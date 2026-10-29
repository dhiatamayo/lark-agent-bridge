"""Read-only Jira Cloud client (REST API v3).

Searches issues with JQL for triage / cleanup. Read-only: the only HTTP verbs
used are GET/POST against the *search* endpoint — it never transitions, edits,
assigns, or creates issues.

Search API (Jira Cloud platform REST v3)::

    POST {base_url}/rest/api/3/search/jql
    Authorization: Basic <base64(email:api_token)>
    Content-Type: application/json
    {
      "jql": "project = SIT ORDER BY updated DESC",
      "maxResults": 50,
      "fields": ["summary","status","assignee","priority","updated","issuetype","labels"]
    }

Credentials come from the :class:`SecretStore`:
    - ``jira.email``  (the Atlassian account email)
    - ``jira.token``  (an API token from id.atlassian.com)

Only the token is a secret; the email is low-sensitivity but kept alongside it
so auth is resolved from one place. The base URL and default project/JQL are
plain config (see :mod:`.config`) so switching boards needs no code change.

Safety:
    - Read-only (search endpoint only).
    - Hard ``maxResults`` cap and a request timeout.
    - All failures become :class:`JiraError` with a user-safe message (never a
      raw requests exception, never a leaked credential).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from ..secrets import SecretError, SecretStore

MAX_RESULTS = 100  # hard cap regardless of what the caller asks for

# Fields we ask Jira for (keeps payloads small and predictable for triage).
DEFAULT_FIELDS = [
    "summary",
    "status",
    "assignee",
    "reporter",
    "priority",
    "issuetype",
    "labels",
    "created",
    "updated",
    "duedate",
]


class JiraError(Exception):
    """Raised for query/config/transport failures (message is user-safe)."""


@dataclass
class JiraIssue:
    """A flattened, triage-friendly view of one Jira issue."""

    key: str
    summary: str = ""
    status: str = ""
    assignee: str = ""
    reporter: str = ""
    priority: str = ""
    issuetype: str = ""
    labels: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    duedate: str = ""
    url: str = ""


@dataclass
class JiraSearchResult:
    jql: str
    total: int
    issues: list[JiraIssue]


def _field_name(obj: Any, key: str = "name") -> str:
    """Safely pull a display string out of a nested Jira field object."""
    if isinstance(obj, dict):
        for k in (key, "displayName", "value"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def _flatten_issue(raw: dict[str, Any], base_url: str) -> JiraIssue:
    """Turn a raw Jira issue JSON into a :class:`JiraIssue`."""
    fields = raw.get("fields") or {}
    key = str(raw.get("key", ""))
    labels = fields.get("labels") or []
    if not isinstance(labels, list):
        labels = []
    return JiraIssue(
        key=key,
        summary=str(fields.get("summary") or ""),
        status=_field_name(fields.get("status")),
        assignee=_field_name(fields.get("assignee"), "displayName"),
        reporter=_field_name(fields.get("reporter"), "displayName"),
        priority=_field_name(fields.get("priority")),
        issuetype=_field_name(fields.get("issuetype")),
        labels=[str(x) for x in labels],
        created=str(fields.get("created") or ""),
        updated=str(fields.get("updated") or ""),
        duedate=str(fields.get("duedate") or ""),
        url=f"{base_url}/browse/{key}" if key else "",
    )


class JiraClient:
    """Thin read-only wrapper over the Jira Cloud search endpoint."""

    def __init__(
        self,
        base_url: str,
        secrets: SecretStore,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.secrets = secrets
        self.timeout = timeout
        self._session = session or requests.Session()

    def _auth(self) -> tuple[str, str]:
        """Resolve (email, token) from the secret store. Fail-closed."""
        try:
            email = self.secrets.get("jira.email")
            token = self.secrets.get("jira.token")
        except SecretError as exc:
            raise JiraError(f"no Jira credentials configured: {exc}") from None
        return str(email), str(token)

    def search(
        self,
        jql: str,
        max_results: int = 50,
        fields: list[str] | None = None,
    ) -> JiraSearchResult:
        """Run a read-only JQL search.

        Raises :class:`JiraError` on any failure (never a raw requests
        exception), with a message safe to show in chat.
        """
        if not self.base_url:
            raise JiraError("Jira base URL is not configured")
        jql = (jql or "").strip()
        if not jql:
            raise JiraError("JQL query is empty")

        size = max(1, min(int(max_results), MAX_RESULTS))
        email, token = self._auth()
        url = f"{self.base_url}/rest/api/3/search/jql"
        payload = {
            "jql": jql,
            "maxResults": size,
            "fields": fields or DEFAULT_FIELDS,
        }
        try:
            resp = self._session.post(
                url, json=payload, auth=(email, token),
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise JiraError(f"request to Jira failed: {exc}") from None

        if resp.status_code in (401, 403):
            raise JiraError(
                f"Jira rejected credentials (HTTP {resp.status_code}); "
                "check the email/API token and that the account can see this project"
            )
        if resp.status_code == 400:
            # Jira returns 400 for a bad JQL — surface its message (trimmed).
            raise JiraError(f"invalid JQL or request: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise JiraError(f"Jira returned HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError:
            raise JiraError("Jira returned a non-JSON response") from None

        raw_issues = data.get("issues", [])
        if not isinstance(raw_issues, list):
            raw_issues = []
        issues = [_flatten_issue(x, self.base_url) for x in raw_issues[:size]]
        total = int(data.get("total", len(issues)))
        return JiraSearchResult(jql=jql, total=total, issues=issues)


__all__ = [
    "JiraClient",
    "JiraError",
    "JiraIssue",
    "JiraSearchResult",
    "MAX_RESULTS",
    "DEFAULT_FIELDS",
]
