"""Jira service: builds a read-only :class:`JiraClient` from settings and offers
chat/CLI-friendly helpers for ticket triage.

Built lazily from :class:`Settings`. If Jira is disabled or misconfigured,
:func:`build_jira` returns ``None`` so the bot keeps running and commands report
a friendly message instead of crashing.

Configurable, not hard-coded: the base URL, default project, and default JQL
come from env (see :class:`Settings`), so moving boards / projects needs no code
change — just config.
"""

from __future__ import annotations

from ..config import Settings
from ..secrets import SecretError, SecretStore
from .client import JiraClient, JiraError, JiraSearchResult

_MAX_ISSUES_RENDERED = 30


class JiraService:
    """Facade over the Jira client for chat commands and the CLI."""

    def __init__(
        self,
        client: JiraClient,
        default_project: str = "",
        default_jql: str = "",
        team_members: list[str] | None = None,
        aliases: dict[str, str] | None = None,
    ):
        self.client = client
        self.default_project = default_project.strip()
        self.default_jql = default_jql.strip()
        # Jira displayNames of the team (for the `team` / `@name` shortcuts).
        self.team_members = [m.strip() for m in (team_members or []) if m.strip()]
        # Optional nickname -> displayName map (e.g. "billy" -> "Billisany
        # Akhyar") so `@billy` resolves even when the Jira name differs. Keys
        # are lowercased for case-insensitive lookup.
        self.aliases: dict[str, str] = {
            k.strip().lower(): v.strip()
            for k, v in (aliases or {}).items()
            if k.strip() and v.strip()
        }

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _quote(value: str) -> str:
        r"""Quote a JQL string value, escaping ``\`` and ``"``.

        JQL string literals are double-quoted; backslash and double-quote must
        be backslash-escaped. Keeps display names with spaces/quotes safe.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _match_member(self, needle: str) -> str | None:
        """Resolve a loose ``@name`` token to a roster displayName.

        Matches case-insensitively: exact first, then substring. Returns the
        canonical roster name, or ``None`` if nothing matches.
        """
        q = needle.strip().lower()
        if not q:
            return None
        # 1) explicit nickname alias wins (e.g. "billy" -> "Billisany Akhyar").
        if q in self.aliases:
            return self.aliases[q]
        # 2) exact displayName match.
        for m in self.team_members:
            if m.lower() == q:
                return m
        # 3) unambiguous substring match.
        hits = [m for m in self.team_members if q in m.lower()]
        return hits[0] if len(hits) == 1 else None

    def _project_clause(self) -> str:
        """``project = "X" AND `` prefix if a default project is set, else ``''``."""
        return (
            f"project = {self._quote(self.default_project)} AND "
            if self.default_project
            else ""
        )

    # -- JQL resolution ---------------------------------------------------- #
    def resolve_jql(self, jql: str | None) -> str:
        """Pick the JQL to run: explicit > configured default > project-scoped.

        Recognised shortcuts (when ``jql`` is a lone token):
            * a project key (e.g. ``SIT``)  -> open issues in that project.
            * ``team``                      -> open issues assigned to any
              roster member (``JIRA_TEAM_MEMBERS``).
            * ``@name`` / ``name``          -> open issues for one roster member
              (loose, case-insensitive match).
        All shortcuts are stalest-first (``ORDER BY updated ASC``).
        """
        if jql and jql.strip():
            text = jql.strip()

            # Shortcut: whole team.
            if text.lower() == "team":
                if not self.team_members:
                    raise JiraError(
                        "no team roster configured (set JIRA_TEAM_MEMBERS in .env)"
                    )
                names = ", ".join(self._quote(m) for m in self.team_members)
                return (
                    f"{self._project_clause()}assignee IN ({names}) "
                    "AND statusCategory != Done ORDER BY updated ASC"
                )

            # Shortcut: a single roster member via @name (or a bare name that
            # resolves to exactly one member).
            member: str | None = None
            if text.startswith("@"):
                member = self._match_member(text[1:])
                if member is None:
                    raise JiraError(
                        f"no team member matches '{text[1:]}' "
                        "(check JIRA_TEAM_MEMBERS)"
                    )
            elif " " not in text and not text.replace("-", "").isalnum():
                # e.g. a name token with a dot; alnum tokens fall through to the
                # project-key shortcut below.
                member = self._match_member(text)

            if member is not None:
                return (
                    f"{self._project_clause()}assignee = {self._quote(member)} "
                    "AND statusCategory != Done ORDER BY updated ASC"
                )

            # Shortcut: a lone token that looks like a project key.
            if text.replace("-", "").isalnum() and " " not in text:
                return (
                    f'project = "{text}" AND statusCategory != Done '
                    "ORDER BY updated ASC"
                )
            return text
        if self.default_jql:
            return self.default_jql
        if self.default_project:
            return (
                f'project = "{self.default_project}" AND statusCategory != Done '
                "ORDER BY updated ASC"
            )
        raise JiraError(
            "no JQL given and no default project/JQL configured "
            "(set JIRA_DEFAULT_PROJECT or JIRA_DEFAULT_JQL)"
        )

    # -- search (raw, for the CLI / agent insight path) -------------------- #
    def search_raw(self, jql: str | None, limit: int) -> JiraSearchResult:
        """Resolve JQL + search, returning the structured result (no rendering).

        Raises :class:`JiraError` on failure (used by the CLI so the agent reads
        raw issues and produces an insight).
        """
        resolved = self.resolve_jql(jql)
        return self.client.search(resolved, max_results=limit)

    # -- search (rendered, for the Lark command) --------------------------- #
    def search(self, jql: str | None, limit: int) -> str:
        """Resolve + search + render a chat-friendly string. Never raises."""
        try:
            result = self.search_raw(jql, limit)
        except (JiraError, SecretError) as exc:
            return f"⚠️ Jira query failed: {exc}"
        return self._format_result(result)

    # -- rendering --------------------------------------------------------- #
    @staticmethod
    def _format_result(result: JiraSearchResult) -> str:
        head = f"🎫 **Jira** · `{result.jql}`"
        if not result.issues:
            return head + "\n\n_No issues matched._"
        shown = result.issues[:_MAX_ISSUES_RENDERED]
        blocks = [head, f"_{result.total} issue(s); showing {len(shown)}._", ""]
        for i, it in enumerate(shown, 1):
            assignee = it.assignee or "Unassigned"
            line = (
                f"**{i}.** `{it.key}` — {it.summary}\n"
                f"    _{it.status} · {it.issuetype} · {it.priority or 'No priority'} "
                f"· {assignee}_"
            )
            if it.labels:
                line += f" · labels: {', '.join(it.labels)}"
            blocks.append(line)
        if result.total > len(shown):
            blocks.append(
                f"\n_… {result.total - len(shown)} more not shown "
                "(narrow the JQL or add a filter)._"
            )
        return "\n".join(blocks)


def build_jira(settings: Settings) -> JiraService | None:
    """Construct the Jira service from settings, or ``None`` if unavailable.

    Never raises: any misconfiguration yields ``None`` so the bot keeps running.
    """
    if not settings.enable_jira:
        return None
    if not settings.jira_base_url or not settings.secrets_path:
        return None
    try:
        store = SecretStore(
            settings.secrets_path,
            key_file=settings.secret_key_file or None,
        )
    except SecretError:
        return None
    client = JiraClient(settings.jira_base_url, store)
    return JiraService(
        client,
        default_project=settings.jira_default_project,
        default_jql=settings.jira_default_jql,
        team_members=settings.jira_team_members,
        aliases=settings.jira_team_aliases,
    )


__all__ = ["JiraService", "build_jira"]
