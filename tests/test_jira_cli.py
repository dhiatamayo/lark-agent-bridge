"""Smoke tests for the Jira CLI (no network, no real .env/secret key)."""

from __future__ import annotations

import json

import pytest

from lark_agent_bridge.config import Settings
from lark_agent_bridge.jira import jira_cli
from lark_agent_bridge.jira.client import JiraClient
from lark_agent_bridge.jira.service import JiraService
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

    def post(self, url, json=None, auth=None, timeout=None, headers=None):
        self.last = {"url": url, "json": json, "auth": auth}
        return self._response


def _issue(key="SIT-1"):
    return {
        "key": key,
        "fields": {
            "summary": "Fix thing",
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Mayo"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Task"},
            "labels": ["backend"],
        },
    }


def _service(payload, default_project="SIT"):
    store = SecretStore("unused", env_key=generate_key())
    store.set_many({"jira.email": "u@x.com", "jira.token": "t"})
    sess = _FakeSession(_FakeResponse(payload=payload))
    client = JiraClient("https://x.atlassian.net", store, session=sess)
    return JiraService(client, default_project=default_project), sess


@pytest.fixture
def patch_build(monkeypatch):
    def _apply(payload, default_project="SIT"):
        svc, sess = _service(payload, default_project)
        settings = object.__new__(Settings)  # skip __init__ (no .env needed)
        settings.jira_default_limit = 30
        monkeypatch.setattr(jira_cli, "_build_service", lambda: (svc, settings))
        return svc, sess
    return _apply


def test_cli_json_output(patch_build, capsys):
    _, sess = patch_build({"issues": [_issue()], "total": 1})
    rc = jira_cli.main(["--jql", "SIT", "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    assert out["issues"][0]["key"] == "SIT-1"
    # bare project key expanded into a scoped JQL
    assert 'project = "SIT"' in sess.last["json"]["jql"]


def test_cli_default_project_when_no_jql(patch_build, capsys):
    _, sess = patch_build({"issues": [], "total": 0}, default_project="SIT")
    rc = jira_cli.main(["--format", "json"])
    assert rc == 0
    assert 'project = "SIT"' in sess.last["json"]["jql"]


def test_cli_text_output(patch_build, capsys):
    patch_build({"issues": [_issue()], "total": 1})
    rc = jira_cli.main(["--jql", "SIT", "--format", "text"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SIT-1" in out
    assert "Fix thing" in out


def test_cli_bad_jql_is_error(patch_build, capsys):
    patch_build({}, default_project="")  # no default -> resolve(None) raises
    rc = jira_cli.main([])  # no --jql, no default project
    assert rc == 2
    assert "error" in capsys.readouterr().err.lower()


def test_cli_disabled_service_exits_3(monkeypatch):
    def _boom():
        print("Jira is disabled", file=__import__("sys").stderr)
        raise SystemExit(3)
    monkeypatch.setattr(jira_cli, "_build_service", _boom)
    with pytest.raises(SystemExit) as exc:
        jira_cli.main(["--jql", "SIT"])
    assert exc.value.code == 3
