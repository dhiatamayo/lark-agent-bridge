"""Tests for AgentRunner.run_streaming.

Uses a fake engine whose ``build_cmd`` launches a real ``python -c`` subprocess
that emits kiro-style stream-json lines to stdout. This exercises the real
Popen/line-reading/progress-callback path (not a mock) while reusing
KiroEngine's parse/progress_label logic.
"""

import json
import sys

import pytest

from lark_agent_bridge.config import Settings
from lark_agent_bridge.engines.kiro import KiroEngine
from lark_agent_bridge.runner import AgentRunner


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    return Settings()


def _emitter_script(events: list[dict], sleep: float = 0.0) -> str:
    """Return a python -c program that prints each event as a JSON line."""
    payload = json.dumps(events)
    return (
        "import json,sys,time\n"
        f"evs=json.loads(r'''{payload}''')\n"
        "for e in evs:\n"
        "    print(json.dumps(e), flush=True)\n"
        f"    time.sleep({sleep})\n"
    )


class FakeStreamEngine(KiroEngine):
    """Kiro parse/progress, but build_cmd runs a local emitter subprocess."""

    name = "kiro"  # reuse the kiro session slot

    def __init__(self, settings, events, sleep=0.0):
        super().__init__("python", settings)
        self._script = _emitter_script(events, sleep)

    def build_cmd(self, prompt, session_id):
        return [sys.executable, "-c", self._script]


def test_run_streaming_collects_progress_and_final(settings):
    events = [
        {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {
            "sessionUpdate": "tool_call", "title": "fs_read"}}},
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hello world"}}}},
        {"type": "runFinished", "data": {"status": "success"}},
    ]
    runner = AgentRunner(settings)
    runner.engine = FakeStreamEngine(settings, events)

    labels = []
    answer = runner.run_streaming("hi", labels.append)

    assert answer == "Hello world"
    # progress surfaced the tool call + writing + finishing (deduped, in order)
    assert "🔧 fs_read" in labels
    assert "📝 writing answer…" in labels
    assert "✅ finishing up…" in labels
    # session id captured from the stream
    assert runner.session_ids["kiro"] == "s1"


def test_run_streaming_dedupes_consecutive_labels(settings):
    events = [
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a"}}}},
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "b"}}}},
        {"type": "runFinished", "data": {"status": "success"}},
    ]
    runner = AgentRunner(settings)
    runner.engine = FakeStreamEngine(settings, events)
    labels = []
    runner.run_streaming("hi", labels.append)
    # two consecutive writing chunks must not emit the label twice in a row
    assert labels.count("📝 writing answer…") == 1


def test_run_streaming_progress_callback_errors_are_swallowed(settings):
    events = [
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}}}},
        {"type": "runFinished", "data": {"status": "success"}},
    ]
    runner = AgentRunner(settings)
    runner.engine = FakeStreamEngine(settings, events)

    def boom(_label):
        raise RuntimeError("lark down")

    # A failing progress callback must not break the run.
    answer = runner.run_streaming("hi", boom)
    assert answer == "ok"


def test_run_streaming_timeout(settings, monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    monkeypatch.setenv("AGENT_TIMEOUT_SEC", "1")
    s = Settings()
    events = [{"type": "sessionUpdate", "data": {"update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": str(i)}}}}
        for i in range(50)]
    runner = AgentRunner(s)
    runner.engine = FakeStreamEngine(s, events, sleep=0.2)  # 50*0.2=10s > 1s timeout
    answer = runner.run_streaming("hi", lambda _l: None)
    assert "timed out" in answer
