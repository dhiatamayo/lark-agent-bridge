"""Smoke tests for the logs CLI (no network, no real .env/secret key).

We monkeypatch ``_build_service`` so the CLI runs against an in-memory
:class:`ObservabilityService` whose OpenObserve client uses a fake HTTP session.
This exercises argument parsing, the raw-query path, order handling, and both
output formats without hitting OpenObserve or reading real credentials.
"""

from __future__ import annotations

import json

import pytest

from lark_agent_bridge.config import Settings
from lark_agent_bridge.observability import logs_cli
from lark_agent_bridge.observability.openobserve import OpenObserveClient
from lark_agent_bridge.observability.registry import ProjectTarget, Registry
from lark_agent_bridge.observability.service import ObservabilityService
from lark_agent_bridge.secrets import SecretStore, generate_key


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.last = None

    def post(self, url, json=None, auth=None, timeout=None):
        self.last = {"url": url, "json": json, "auth": auth, "timeout": timeout}
        return self._response


def _service(payload):
    reg = Registry({
        "mt4api-dev": ProjectTarget(
            name="mt4api-dev", backend="openobserve",
            default_window="15m", org="MTM-DEV",
            stream="meta_python_mt4dev_connector",
        ),
    })
    store = SecretStore("unused", env_key=generate_key())
    store.set_many({
        "openobserve.MTM-DEV.user": "u", "openobserve.MTM-DEV.token": "t",
    })
    sess = _FakeSession(_FakeResponse(payload=payload))
    client = OpenObserveClient("https://oo", store, session=sess)
    return ObservabilityService(reg, client), sess


@pytest.fixture
def patch_build(monkeypatch):
    """Return a helper that wires a fake service + minimal Settings into the CLI."""
    def _apply(payload):
        svc, sess = _service(payload)
        settings = object.__new__(Settings)       # skip __init__ (no .env needed)
        settings.logs_default_limit = 50
        settings.logs_tz_offset_hours = 7.0
        monkeypatch.setattr(logs_cli, "_build_service",
                            lambda: (svc, settings))
        return svc, sess
    return _apply


def test_cli_requires_project(patch_build, capsys):
    patch_build({"hits": [], "total": 0})
    rc = logs_cli.main([])
    assert rc == 2
    assert "project" in capsys.readouterr().err.lower()


def test_cli_list(patch_build, capsys):
    patch_build({"hits": [], "total": 0})
    rc = logs_cli.main(["--list"])
    assert rc == 0
    assert "mt4api-dev" in capsys.readouterr().out


def test_cli_json_output(patch_build, capsys):
    _, sess = patch_build({
        "hits": [{"_timestamp": 1, "log": "hello"}], "total": 1, "took": 5,
    })
    rc = logs_cli.main(["--project", "mt4api-dev", "--window", "15m",
                        "--filter", "hello", "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] == "mt4api-dev"
    assert out["filter"] == "hello"
    assert out["total"] == 1
    assert out["rows"] == [{"_timestamp": 1, "log": "hello"}]
    assert "match_all('hello')" in sess.last["json"]["query"]["sql"]


def test_cli_order_asc_absolute_range(patch_build, capsys):
    _, sess = patch_build({"hits": [], "total": 0})
    rc = logs_cli.main([
        "--project", "mt4api-dev",
        "--from", "2026-10-19 10:00:00", "--to", "2026-10-19 11:00:00",
        "--limit", "10", "--order", "asc", "--filter", "(110",
        "--format", "json",
    ])
    assert rc == 0
    body = sess.last["json"]["query"]
    assert "ORDER BY _timestamp ASC" in body["sql"]
    assert body["size"] == 10
    # single quote escaping keeps the '(110' filter safe
    assert "match_all('(110')" in body["sql"]
    assert body["start_time"] < body["end_time"]


def test_cli_text_output(patch_build, capsys):
    patch_build({"hits": [{"_timestamp": 1, "log": "x"}], "total": 1})
    rc = logs_cli.main(["--project", "mt4api-dev", "--window", "15m",
                        "--format", "text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mt4api-dev" in out
    assert "meta_python_mt4dev_connector" in out


def test_cli_unknown_project_is_error(patch_build, capsys):
    patch_build({"hits": [], "total": 0})
    rc = logs_cli.main(["--project", "does-not-exist"])
    assert rc == 2
    assert "unknown project" in capsys.readouterr().err.lower()


def test_cli_disabled_service_exits_3(monkeypatch, capsys):
    def _boom():
        print("observability is disabled", file=__import__("sys").stderr)
        raise SystemExit(3)
    monkeypatch.setattr(logs_cli, "_build_service", _boom)
    with pytest.raises(SystemExit) as exc:
        logs_cli.main(["--project", "mt4api-dev"])
    assert exc.value.code == 3
