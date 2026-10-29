"""Tests for the Jira client + service (mocked HTTP, no network)."""

from __future__ import annotations

import pytest

from lark_agent_bridge.jira.client import (
    MAX_RESULTS,
    JiraClient,
    JiraError,
    _flatten_issue,
)
from lark_agent_bridge.jira.service import JiraService
from lark_agent_bridge.secrets import SecretStore, generate_key


# --------------------------- fakes ------------------------------------------ #
class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    """Captures the last request and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.last = None

    def post(self, url, json=None, auth=None, timeout=None, headers=None):
        self.last = {"url": url, "json": json, "auth": auth,
                     "timeout": timeout, "headers": headers}
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _store(email="me@sam.com", token="tok"):
    s = SecretStore("unused", env_key=generate_key())
    s.set_many({"jira.email": email, "jira.token": token})
    return s


def _issue_payload(key="SIT-1", summary="Fix thing", status="In Progress",
                   assignee="Mayo"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "assignee": {"displayName": assignee} if assignee else None,
            "reporter": {"displayName": "Dyeva"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Task"},
            "labels": ["backend", "mt4"],
            "created": "2026-10-01T10:00:00.000+0700",
            "updated": "2026-10-20T10:00:00.000+0700",
            "duedate": None,
        },
    }


# --------------------------- flatten ---------------------------------------- #
def test_flatten_issue_builds_url_and_fields():
    issue = _flatten_issue(_issue_payload(), "https://x.atlassian.net")
    assert issue.key == "SIT-1"
    assert issue.summary == "Fix thing"
    assert issue.status == "In Progress"
    assert issue.assignee == "Mayo"
    assert issue.reporter == "Dyeva"
    assert issue.priority == "High"
    assert issue.issuetype == "Task"
    assert issue.labels == ["backend", "mt4"]
    assert issue.url == "https://x.atlassian.net/browse/SIT-1"


def test_flatten_issue_unassigned_safe():
    issue = _flatten_issue(_issue_payload(assignee=None), "https://x")
    assert issue.assignee == ""  # no crash on null assignee


def test_flatten_issue_handles_missing_fields():
    issue = _flatten_issue({"key": "SIT-9"}, "https://x")
    assert issue.key == "SIT-9"
    assert issue.summary == ""
    assert issue.labels == []


# --------------------------- client (mocked) -------------------------------- #
def test_search_happy_path():
    resp = _FakeResponse(payload={"issues": [_issue_payload()], "total": 1})
    sess = _FakeSession(resp)
    client = JiraClient("https://x.atlassian.net", _store(), session=sess)

    result = client.search("project = SIT", max_results=50)

    assert result.total == 1
    assert len(result.issues) == 1
    assert result.issues[0].key == "SIT-1"
    # request shaped correctly: v3 search/jql endpoint + basic auth
    assert sess.last["url"] == "https://x.atlassian.net/rest/api/3/search/jql"
    assert sess.last["auth"] == ("me@sam.com", "tok")
    assert sess.last["json"]["jql"] == "project = SIT"
    assert sess.last["json"]["maxResults"] == 50


def test_search_empty_jql_raises():
    sess = _FakeSession(_FakeResponse(payload={"issues": []}))
    client = JiraClient("https://x", _store(), session=sess)
    with pytest.raises(JiraError):
        client.search("   ")


def test_search_caps_max_results():
    sess = _FakeSession(_FakeResponse(payload={"issues": [], "total": 0}))
    client = JiraClient("https://x", _store(), session=sess)
    client.search("project = SIT", max_results=99999)
    assert sess.last["json"]["maxResults"] == MAX_RESULTS


def test_search_truncates_issues_to_size():
    issues = [_issue_payload(key=f"SIT-{i}") for i in range(50)]
    sess = _FakeSession(_FakeResponse(payload={"issues": issues, "total": 50}))
    client = JiraClient("https://x", _store(), session=sess)
    result = client.search("project = SIT", max_results=10)
    assert len(result.issues) == 10


def test_search_401_is_credential_error():
    sess = _FakeSession(_FakeResponse(status_code=401, text="nope"))
    client = JiraClient("https://x", _store(), session=sess)
    with pytest.raises(JiraError) as exc:
        client.search("project = SIT")
    assert "credential" in str(exc.value).lower()


def test_search_400_is_jql_error():
    sess = _FakeSession(_FakeResponse(status_code=400, text="bad jql"))
    client = JiraClient("https://x", _store(), session=sess)
    with pytest.raises(JiraError) as exc:
        client.search("garbage ===")
    assert "jql" in str(exc.value).lower()


def test_search_transport_error_wrapped():
    import requests
    sess = _FakeSession(requests.RequestException("conn refused"))
    client = JiraClient("https://x", _store(), session=sess)
    with pytest.raises(JiraError) as exc:
        client.search("project = SIT")
    assert "failed" in str(exc.value).lower()


def test_search_missing_credentials_fails_closed():
    empty = SecretStore("unused", env_key=generate_key())  # no jira creds
    sess = _FakeSession(_FakeResponse(payload={"issues": []}))
    client = JiraClient("https://x", empty, session=sess)
    with pytest.raises(JiraError) as exc:
        client.search("project = SIT")
    assert "credential" in str(exc.value).lower()


# --------------------------- service / JQL resolution ----------------------- #
def _service(payload, default_project="", default_jql=""):
    sess = _FakeSession(_FakeResponse(payload=payload))
    client = JiraClient("https://x.atlassian.net", _store(), session=sess)
    return JiraService(client, default_project, default_jql), sess


def test_resolve_jql_explicit_full_jql_passthrough():
    svc, _ = _service({"issues": []})
    jql = svc.resolve_jql("project = SIT AND status = Done")
    assert jql == "project = SIT AND status = Done"


def test_resolve_jql_bare_project_shortcut():
    svc, _ = _service({"issues": []})
    jql = svc.resolve_jql("SIT")
    assert 'project = "SIT"' in jql
    assert "statusCategory != Done" in jql
    assert "ORDER BY updated ASC" in jql


def test_resolve_jql_default_project_when_empty():
    svc, _ = _service({"issues": []}, default_project="SIT")
    jql = svc.resolve_jql(None)
    assert 'project = "SIT"' in jql


def test_resolve_jql_default_jql_wins_over_project():
    svc, _ = _service({"issues": []}, default_project="SIT",
                      default_jql="assignee = currentUser()")
    assert svc.resolve_jql(None) == "assignee = currentUser()"


def test_resolve_jql_no_default_raises():
    svc, _ = _service({"issues": []})
    with pytest.raises(JiraError):
        svc.resolve_jql(None)


def test_service_search_renders_issues():
    svc, sess = _service({"issues": [_issue_payload()], "total": 1},
                         default_project="SIT")
    out = svc.search(None, 30)
    assert "SIT-1" in out
    assert "Fix thing" in out
    # default project shortcut used
    assert 'project = "SIT"' in sess.last["json"]["jql"]


def test_service_search_empty_message():
    svc, _ = _service({"issues": [], "total": 0}, default_project="SIT")
    out = svc.search(None, 30)
    assert "No issues matched" in out


def test_service_search_error_is_friendly_string():
    sess = _FakeSession(_FakeResponse(status_code=401, text="nope"))
    client = JiraClient("https://x", _store(), session=sess)
    svc = JiraService(client, default_project="SIT")
    out = svc.search(None, 30)
    assert out.startswith("⚠️")  # never raises to the caller


# --------------------------- team roster / shortcuts ------------------------ #
_ROSTER = ["Dhiatamayo Fadhlika", "Asep Wahyudin", "Dyeva Mahaputra",
           "tri wibowo", "Aldo Karendra"]


def _team_service(default_project="SIT", roster=None):
    sess = _FakeSession(_FakeResponse(payload={"issues": [], "total": 0}))
    client = JiraClient("https://x.atlassian.net", _store(), session=sess)
    svc = JiraService(
        client, default_project=default_project,
        team_members=_ROSTER if roster is None else roster,
    )
    return svc, sess


def test_quote_escapes_backslash_and_quote():
    assert JiraService._quote('a"b') == '"a\\"b"'
    assert JiraService._quote("a\\b") == '"a\\\\b"'


def test_resolve_jql_team_shortcut_lists_all_members():
    svc, _ = _team_service()
    jql = svc.resolve_jql("team")
    # scoped to the default project
    assert 'project = "SIT" AND' in jql
    # every roster member appears, quoted, inside assignee IN (...)
    assert "assignee IN (" in jql
    for name in _ROSTER:
        assert f'"{name}"' in jql
    assert "statusCategory != Done" in jql
    assert "ORDER BY updated ASC" in jql


def test_resolve_jql_team_case_insensitive():
    svc, _ = _team_service()
    assert "assignee IN (" in svc.resolve_jql("TEAM")


def test_resolve_jql_team_without_roster_raises():
    svc, _ = _team_service(roster=[])
    with pytest.raises(JiraError) as exc:
        svc.resolve_jql("team")
    assert "roster" in str(exc.value).lower()


def test_resolve_jql_team_without_project_has_no_project_clause():
    svc, _ = _team_service(default_project="")
    jql = svc.resolve_jql("team")
    assert "project =" not in jql
    assert jql.startswith("assignee IN (")


def test_resolve_jql_at_name_exact_member():
    svc, _ = _team_service()
    jql = svc.resolve_jql("@Asep Wahyudin")
    assert 'assignee = "Asep Wahyudin"' in jql
    assert 'project = "SIT" AND' in jql
    assert "statusCategory != Done" in jql


def test_resolve_jql_at_name_substring_match():
    svc, _ = _team_service()
    jql = svc.resolve_jql("@dyeva")  # case-insensitive substring
    assert 'assignee = "Dyeva Mahaputra"' in jql


def test_resolve_jql_at_name_no_match_raises():
    svc, _ = _team_service()
    with pytest.raises(JiraError) as exc:
        svc.resolve_jql("@nobody")
    assert "no team member" in str(exc.value).lower()


def test_resolve_jql_at_name_ambiguous_raises():
    # two members share the substring "a" -> ambiguous -> no single match
    svc, _ = _team_service(roster=["Asep Wahyudin", "Aldo Karendra"])
    with pytest.raises(JiraError):
        svc.resolve_jql("@a")


def test_resolve_jql_bare_project_key_still_wins_over_member():
    # a bare alnum token is a PROJECT key, never a member (members need @)
    svc, _ = _team_service()
    jql = svc.resolve_jql("SIT")
    assert 'project = "SIT"' in jql
    assert "assignee" not in jql


def test_resolve_jql_alias_resolves_to_displayname():
    sess = _FakeSession(_FakeResponse(payload={"issues": [], "total": 0}))
    client = JiraClient("https://x.atlassian.net", _store(), session=sess)
    svc = JiraService(
        client, default_project="SIT",
        team_members=["Billisany Akhyar", "tri wibowo"],
        aliases={"billy": "Billisany Akhyar", "bowo": "tri wibowo"},
    )
    # alias that is NOT a substring of the displayName still resolves
    jql = svc.resolve_jql("@billy")
    assert 'assignee = "Billisany Akhyar"' in jql
    # alias is case-insensitive
    assert 'assignee = "Billisany Akhyar"' in svc.resolve_jql("@BILLY")


def test_resolve_jql_alias_wins_over_substring():
    # 'bowo' is a substring of 'tri wibowo' AND an alias -> alias path is fine
    sess = _FakeSession(_FakeResponse(payload={"issues": [], "total": 0}))
    client = JiraClient("https://x", _store(), session=sess)
    svc = JiraService(
        client, default_project="SIT",
        team_members=["tri wibowo"],
        aliases={"bowo": "tri wibowo"},
    )
    assert 'assignee = "tri wibowo"' in svc.resolve_jql("@bowo")
