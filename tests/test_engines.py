"""Tests for engine adapters: build_cmd and stream-json parsing.

Uses sample stream-json lines matching the real CLI output schemas. The
adapters never touch the binary during build_cmd/parse, so a dummy binary name
is fine for construction.
"""

import json

import pytest

from lark_agent_bridge.config import Settings
from lark_agent_bridge.engines.agy import AgyEngine
from lark_agent_bridge.engines.kiro import KiroEngine


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")

    def _make(**overrides):
        for k, v in overrides.items():
            monkeypatch.setenv(k, v)
        return Settings()

    return _make


# --- kiro ------------------------------------------------------------------ #
def test_kiro_build_cmd_basics(settings):
    eng = KiroEngine("kiro-cli", settings(AGENT_TRUST_TOOLS="read,grep"))
    cmd = eng.build_cmd("hi", None)
    assert cmd[0] == "kiro-cli"
    assert "chat" in cmd
    assert "--trust-tools=read,grep" in cmd
    assert cmd[-1] == "hi"  # prompt is the final positional arg


def test_kiro_build_cmd_trust_all_and_resume(settings):
    eng = KiroEngine("kiro-cli", settings(AGENT_TRUST_TOOLS="all"))
    cmd = eng.build_cmd("hi", "sess-123")
    assert "--trust-all-tools" in cmd
    assert "--resume-id" in cmd and "sess-123" in cmd


def test_kiro_parse_segments_and_session(settings):
    eng = KiroEngine("kiro-cli", settings())
    lines = [
        {"type": "sessionUpdate", "data": {"sessionId": "s1", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hello "}}}},
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "world"}}}},
        {"type": "runFinished", "data": {"status": "success"}},
    ]
    stdout = "\n".join(json.dumps(x) for x in lines)
    segments, sid, ok = eng.parse(stdout)
    assert segments == ["Hello world"]
    assert sid == "s1"
    assert ok is True


def test_kiro_parse_tool_call_splits_segments(settings):
    eng = KiroEngine("kiro-cli", settings())
    lines = [
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "before tool"}}}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call"}}},
        {"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "after tool"}}}},
        {"type": "runFinished", "data": {"status": "success"}},
    ]
    stdout = "\n".join(json.dumps(x) for x in lines)
    segments, _, ok = eng.parse(stdout)
    assert segments == ["before tool", "after tool"]
    assert ok is True


def test_kiro_parse_failure_status(settings):
    eng = KiroEngine("kiro-cli", settings())
    stdout = json.dumps({"type": "runFinished", "data": {"status": "error"}})
    _, _, ok = eng.parse(stdout)
    assert ok is False


# --- agy ------------------------------------------------------------------- #
def test_agy_build_cmd_prompt_attached(settings):
    eng = AgyEngine("agy", settings(AGY_MODEL="gemini-3.8-flash-high", AGY_EFFORT="high"))
    cmd = eng.build_cmd("do it", "conv-9")
    assert "--dangerously-skip-permissions" in cmd
    assert "--model" in cmd and "gemini-3.8-flash-high" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--conversation" in cmd and "conv-9" in cmd
    assert cmd[-1] == "--print=do it"  # prompt MUST be attached to the flag


def test_agy_parse_result_response(settings):
    eng = AgyEngine("agy", settings())
    lines = [
        {"event": "init", "conversation_id": "conv-1"},
        {"event": "step_update", "data": {"x": 1}},
        {"event": "result", "result": {
            "conversation_id": "conv-1", "status": "SUCCESS", "response": "the answer"}},
    ]
    stdout = "\n".join(json.dumps(x) for x in lines)
    segments, sid, ok = eng.parse(stdout)
    assert segments == ["the answer"]
    assert sid == "conv-1"
    assert ok is True


def test_agy_parse_failure(settings):
    eng = AgyEngine("agy", settings())
    stdout = json.dumps({"event": "result", "result": {"status": "ERROR", "response": ""}})
    segments, _, ok = eng.parse(stdout)
    assert ok is False
    assert segments == []
