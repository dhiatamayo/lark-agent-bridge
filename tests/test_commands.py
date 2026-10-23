"""Tests for slash-command dispatch.

Uses a fake runner so no real CLI binary is required. We only verify the
routing/validation behaviour of commands.dispatch and that it never raises on
engine-specific commands regardless of the active engine (the key safety goal:
calling agy-only commands under kiro must return a friendly message, not error).
"""

import pytest

from lark_agent_bridge import commands
from lark_agent_bridge.config import Settings
from lark_agent_bridge.engines import EngineNotFound
from lark_agent_bridge.transcript import TranscriptStore


class _FakeEngine:
    def __init__(self, name, binary="dummy"):
        self.name = name
        self.binary = binary


class _FakeRunner:
    """Mimics AgentRunner's surface used by commands.dispatch."""

    def __init__(self, engine_name="kiro"):
        self.engine = _FakeEngine(engine_name)
        self.session_ids = {"kiro": None, "agy": None, "copilot": None}
        self.queued = {}

    def reset_session(self):
        self.session_ids[self.engine.name] = None

    def switch_engine(self, name):
        name = name.strip().lower()
        if name not in ("kiro", "agy", "copilot"):
            raise ValueError(f"Unknown engine {name!r}")
        # Simulate agy not installed to exercise the EngineNotFound path.
        if name == "agy" and getattr(self, "agy_missing", False):
            raise EngineNotFound("agy not found")
        self.engine = _FakeEngine(name)
        self.session_ids[name] = None
        return self.engine

    def queue_context(self, engine_name, context):
        self.queued[engine_name] = context


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    return Settings()


@pytest.fixture
def transcript(tmp_path):
    return TranscriptStore(tmp_path)


def test_non_command_returns_none(settings, transcript):
    assert commands.dispatch("hello there", _FakeRunner(), transcript, settings) is None


def test_unknown_command(settings, transcript):
    out = commands.dispatch("/frobnicate", _FakeRunner(), transcript, settings)
    assert "Unknown command" in out


def test_help(settings, transcript):
    out = commands.dispatch("/help", _FakeRunner(), transcript, settings)
    assert "/engine" in out and "/model" in out


def test_new_resets_session(settings, transcript):
    runner = _FakeRunner("kiro")
    runner.session_ids["kiro"] = "old-sid"
    out = commands.dispatch("/new", runner, transcript, settings)
    assert runner.session_ids["kiro"] is None
    assert "new conversation" in out.lower()


def test_engine_show(settings, transcript):
    out = commands.dispatch("/engine", _FakeRunner("kiro"), transcript, settings)
    assert "Active engine" in out and "kiro" in out


def test_engine_switch(settings, transcript):
    runner = _FakeRunner("kiro")
    out = commands.dispatch("/engine agy", runner, transcript, settings)
    assert runner.engine.name == "agy"
    assert "Switched engine" in out


def test_engine_switch_missing_binary(settings, transcript):
    runner = _FakeRunner("kiro")
    runner.agy_missing = True
    out = commands.dispatch("/engine agy", runner, transcript, settings)
    assert "Can't switch" in out
    assert runner.engine.name == "kiro"  # unchanged


def test_model_under_kiro_is_friendly(settings, transcript):
    # The key safety requirement: /model under kiro must not error.
    out = commands.dispatch("/model", _FakeRunner("kiro"), transcript, settings)
    assert "kiro" in out.lower()
    assert "server-side" in out.lower()


def test_model_set_under_kiro_rejected(settings, transcript):
    out = commands.dispatch("/model gemini-x", _FakeRunner("kiro"), transcript, settings)
    assert "agy-only" in out.lower() or "does not support" in out.lower()


def test_model_set_under_agy(settings, transcript):
    runner = _FakeRunner("agy")
    out = commands.dispatch("/model gemini-3.8-flash-high", runner, transcript, settings)
    assert settings.agy_model == "gemini-3.8-flash-high"
    assert "agy model set" in out.lower()


def test_models_under_kiro(settings, transcript):
    out = commands.dispatch("/models", _FakeRunner("kiro"), transcript, settings)
    assert "*agy* and *copilot*" in out


def test_effort_valid_under_agy(settings, transcript):
    runner = _FakeRunner("agy")
    out = commands.dispatch("/effort high", runner, transcript, settings)
    assert settings.agy_effort == "high"
    assert "effort set" in out.lower()


def test_effort_invalid_under_agy(settings, transcript):
    out = commands.dispatch("/effort turbo", _FakeRunner("agy"), transcript, settings)
    assert "invalid effort" in out.lower()


def test_effort_under_kiro(settings, transcript):
    out = commands.dispatch("/effort", _FakeRunner("kiro"), transcript, settings)
    assert "*agy* and *copilot*" in out


# --- copilot engine commands ----------------------------------------------- #
def test_engine_switch_copilot(settings, transcript):
    runner = _FakeRunner("kiro")
    out = commands.dispatch("/engine copilot", runner, transcript, settings)
    assert runner.engine.name == "copilot"
    assert "Switched engine" in out


def test_model_show_under_copilot(settings, transcript):
    out = commands.dispatch("/model", _FakeRunner("copilot"), transcript, settings)
    assert "copilot" in out.lower()


def test_model_set_under_copilot(settings, transcript):
    runner = _FakeRunner("copilot")
    out = commands.dispatch("/model gpt-5.4", runner, transcript, settings)
    assert settings.copilot_model == "gpt-5.4"
    assert "copilot model set" in out.lower()


def test_model_reset_under_copilot(settings, transcript):
    runner = _FakeRunner("copilot")
    settings.copilot_model = "gpt-5.4"
    out = commands.dispatch("/model default", runner, transcript, settings)
    assert settings.copilot_model == ""
    assert "reset" in out.lower()


def test_models_under_copilot(settings, transcript):
    out = commands.dispatch("/models", _FakeRunner("copilot"), transcript, settings)
    assert "copilot" in out.lower()
    assert "auto" in out  # auto is listed


def test_effort_valid_under_copilot(settings, transcript):
    runner = _FakeRunner("copilot")
    out = commands.dispatch("/effort minimal", runner, transcript, settings)
    assert settings.copilot_effort == "minimal"  # copilot-specific level
    assert "effort set" in out.lower()


def test_effort_none_under_copilot(settings, transcript):
    runner = _FakeRunner("copilot")
    out = commands.dispatch("/effort none", runner, transcript, settings)
    assert settings.copilot_effort == "none"


def test_effort_invalid_under_copilot(settings, transcript):
    out = commands.dispatch("/effort turbo", _FakeRunner("copilot"), transcript, settings)
    assert "invalid effort" in out.lower()


def test_timeout_set(settings, transcript):
    out = commands.dispatch("/timeout 300", _FakeRunner(), transcript, settings)
    assert settings.timeout_sec == 300
    assert "300s" in out


def test_timeout_invalid(settings, transcript):
    out = commands.dispatch("/timeout abc", _FakeRunner(), transcript, settings)
    assert "invalid timeout" in out.lower()


def test_reply_mode_toggle(settings, transcript):
    out = commands.dispatch("/reply all", _FakeRunner(), transcript, settings)
    assert settings.reply_mode == "all"
    assert "reply mode set" in out.lower()


def test_reply_mode_invalid(settings, transcript):
    out = commands.dispatch("/reply loud", _FakeRunner(), transcript, settings)
    assert "invalid reply mode" in out.lower()


def test_transcript_empty(settings, transcript):
    out = commands.dispatch("/transcript", _FakeRunner(), transcript, settings)
    assert "no transcript history" in out.lower()


def test_usage_and_status_smoke(settings, transcript):
    assert "Usage statistics" in commands.dispatch("/usage", _FakeRunner(), transcript, settings)
    assert "Bot status" in commands.dispatch("/status", _FakeRunner(), transcript, settings)
