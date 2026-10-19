"""Tests for the /logs and /projects observability commands."""

import pytest

from lark_agent_bridge import commands
from lark_agent_bridge.config import Settings
from lark_agent_bridge.transcript import TranscriptStore


class _FakeEngine:
    def __init__(self, name="kiro", binary="dummy"):
        self.name = name
        self.binary = binary


class _FakeRunner:
    def __init__(self, engine_name="kiro"):
        self.engine = _FakeEngine(engine_name)
        self.session_ids = {"kiro": None, "agy": None}


class _FakeObservability:
    """Records calls so we can assert parsing without any network."""

    def __init__(self):
        self.calls = []

    def list_projects(self):
        return "📁 **Registered projects:**\n• `mt4api-dev` — openobserve"

    def query_logs(self, project, window, query_text, limit,
                   start=None, end=None, tz_offset_hours=7.0):
        self.calls.append(
            (project, window, query_text, limit, start, end, tz_offset_hours)
        )
        if start or end:
            return f"LOGS[{project}|{start}..{end}|{query_text}|{limit}]"
        return f"LOGS[{project}|{window}|{query_text}|{limit}]"

    def add_project(self, name, org, stream, window):
        self.calls.append(("add", name, org, stream, window))
        return f"ADD[{name}|{org}|{stream}|{window}]"

    def remove_project(self, name):
        self.calls.append(("remove", name))
        return f"REMOVE[{name}]"


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    return Settings()


@pytest.fixture
def transcript(tmp_path):
    return TranscriptStore(tmp_path)


# ---- disabled path (no service attached) ----------------------------------- #
def test_projects_when_disabled(settings, transcript):
    out = commands.dispatch("/projects", _FakeRunner(), transcript, settings)
    assert "not enabled" in out.lower()


def test_logs_when_disabled(settings, transcript):
    out = commands.dispatch("/logs mt4api-dev", _FakeRunner(), transcript, settings)
    assert "not enabled" in out.lower()


# ---- enabled path (fake service) ------------------------------------------- #
def test_projects_lists(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch("/projects", _FakeRunner(), transcript, settings)
    assert "mt4api-dev" in out


def test_logs_requires_project(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch("/logs", _FakeRunner(), transcript, settings)
    assert "usage" in out.lower()


def test_logs_project_only_uses_defaults(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch("/logs mt4api-dev", _FakeRunner(), transcript, settings)
    # window None (default), no filter, default limit; no absolute range
    assert svc.calls[-1][:4] == ("mt4api-dev", None, None, settings.logs_default_limit)
    assert svc.calls[-1][4:6] == (None, None)


def test_logs_with_window(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch("/logs mt4api-dev 1h", _FakeRunner(), transcript, settings)
    assert svc.calls[-1][:4] == ("mt4api-dev", "1h", None, settings.logs_default_limit)


def test_logs_with_window_and_filter(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch("/logs mt4api-dev 30m status=500 error", _FakeRunner(), transcript, settings)
    assert svc.calls[-1][:4] == ("mt4api-dev", "30m", "status=500 error", settings.logs_default_limit)


def test_logs_filter_without_window(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    # 2nd token is not a window -> treated as filter, window stays default (None)
    commands.dispatch("/logs mt4api-dev error", _FakeRunner(), transcript, settings)
    assert svc.calls[-1][:4] == ("mt4api-dev", None, "error", settings.logs_default_limit)


# ---- /logs absolute range (from ... to ...) -------------------------------- #
def test_logs_absolute_range_full_datetime(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch(
        "/logs mt4api-dev from 2026-10-19 10:00:00 to 2026-10-19 11:00:00",
        _FakeRunner(), transcript, settings,
    )
    project, window, query_text, limit, start, end, tz = svc.calls[-1]
    assert project == "mt4api-dev"
    assert window is None
    assert query_text is None
    assert start == "2026-10-19 10:00:00"
    assert end == "2026-10-19 11:00:00"
    assert tz == settings.logs_tz_offset_hours


def test_logs_absolute_range_with_filter(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch(
        "/logs mt4api-dev from 2026-10-19 10:00:00 to 2026-10-19 11:00:00 timeout error",
        _FakeRunner(), transcript, settings,
    )
    _, _, query_text, _, start, end, _ = svc.calls[-1]
    assert start == "2026-10-19 10:00:00"
    assert end == "2026-10-19 11:00:00"
    assert query_text == "timeout error"


def test_logs_absolute_range_bare_dates(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch(
        "/logs mt4api-dev from 2026-10-19 to 2026-10-20 error",
        _FakeRunner(), transcript, settings,
    )
    _, _, query_text, _, start, end, _ = svc.calls[-1]
    assert start == "2026-10-19"
    assert end == "2026-10-20"
    assert query_text == "error"


def test_logs_absolute_range_missing_to(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch(
        "/logs mt4api-dev from 2026-10-19 10:00:00",
        _FakeRunner(), transcript, settings,
    )
    assert "to" in out.lower()


def test_logs_absolute_range_hh_mm_only(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch(
        "/logs mt4api-dev from 2026-10-19 10:00 to 2026-10-19 11:30 boom",
        _FakeRunner(), transcript, settings,
    )
    _, _, query_text, _, start, end, _ = svc.calls[-1]
    assert start == "2026-10-19 10:00"
    assert end == "2026-10-19 11:30"
    assert query_text == "boom"


def test_logs_returns_service_output(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch("/logs mt4api-dev 15m boom", _FakeRunner(), transcript, settings)
    assert out.startswith("LOGS[mt4api-dev|15m|boom|")


def test_help_includes_logs(settings, transcript):
    out = commands.dispatch("/help", _FakeRunner(), transcript, settings)
    assert "/logs" in out and "/projects" in out


def test_help_includes_project_add(settings, transcript):
    out = commands.dispatch("/help", _FakeRunner(), transcript, settings)
    assert "/project-add" in out and "/project-remove" in out


# ---- /project-add ---------------------------------------------------------- #
def test_project_add_disabled(settings, transcript):
    out = commands.dispatch("/project-add x O S", _FakeRunner(), transcript, settings)
    assert "not enabled" in out.lower()


def test_project_add_usage_when_too_few_args(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch("/project-add x O", _FakeRunner(), transcript, settings)
    assert "usage" in out.lower()


def test_project_add_without_window(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    out = commands.dispatch(
        "/project-add mt4api-uat MTM-UAT s_uat", _FakeRunner(), transcript, settings
    )
    assert svc.calls[-1] == ("add", "mt4api-uat", "MTM-UAT", "s_uat", None)
    assert out.startswith("ADD[")


def test_project_add_with_window(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    commands.dispatch(
        "/project-add mt4api-uat MTM-UAT s_uat 1h", _FakeRunner(), transcript, settings
    )
    assert svc.calls[-1] == ("add", "mt4api-uat", "MTM-UAT", "s_uat", "1h")


# ---- /project-remove ------------------------------------------------------- #
def test_project_remove_disabled(settings, transcript):
    out = commands.dispatch("/project-remove x", _FakeRunner(), transcript, settings)
    assert "not enabled" in out.lower()


def test_project_remove_usage(settings, transcript):
    settings.observability = _FakeObservability()
    out = commands.dispatch("/project-remove", _FakeRunner(), transcript, settings)
    assert "usage" in out.lower()


def test_project_remove_calls_service(settings, transcript):
    svc = _FakeObservability()
    settings.observability = svc
    out = commands.dispatch("/project-remove mt4api-dev", _FakeRunner(), transcript, settings)
    assert svc.calls[-1] == ("remove", "mt4api-dev")
    assert out == "REMOVE[mt4api-dev]"
