"""Command-line entrypoint for Jira — the agent's "fetch then insight" path.

Runs the exact same read-only JQL search the Lark ``/jira`` command runs, but
from a terminal so an agent (or a human) can capture the result and reason over
it (triage, find stale/unassigned/duplicate tickets, suggest priorities). It
reuses :func:`build_jira` so the config and encrypted secret store are wired
identically to the bot — there is no second code path.

Usage::

    # default project / default JQL (from .env):
    python -m lark_agent_bridge.jira.jira_cli --format json

    # a bare project key -> open issues, stalest first:
    python -m lark_agent_bridge.jira.jira_cli --jql SIT --format json

    # an explicit JQL:
    python -m lark_agent_bridge.jira.jira_cli \\
        --jql "assignee = currentUser() AND statusCategory != Done ORDER BY updated ASC" \\
        --limit 50 --format json

Design / safety:
    - **Read-only.** Only ever calls the search endpoint; never writes.
    - **No secrets echoed.** Credentials come from the encrypted store via
      ``build_jira``; this CLI prints issue fields only, never the key or token.
    - **Data boundary.** Using this CLI implies the caller WANTS the issues in
      hand (e.g. an agent that will summarise them). The Lark ``/jira`` command
      is the RAW-straight-to-chat path that never passes through an LLM.
    - **Exit codes:** 0 = success (even if zero issues), 2 = user/query error
      (bad JQL, creds), 3 = Jira disabled/unconfigured.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from ..config import Settings, default_env_path
from ..secrets import SecretError
from .client import JiraError
from .service import JiraService, build_jira


def _build_service() -> tuple[JiraService, Settings]:
    """Construct the Jira service + settings from the repo ``.env``.

    Raises :class:`SystemExit` (code 3) if Jira is disabled or the service
    cannot be built.
    """
    settings = Settings(default_env_path())
    svc = build_jira(settings)
    if svc is None:
        print(
            "Jira is disabled or misconfigured "
            "(check ENABLE_JIRA, JIRA_BASE_URL, LAB_SECRETS_PATH, creds)",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return svc, settings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="jira_cli",
        description="Read-only Jira JQL search (reuses the bot's Jira stack).",
    )
    p.add_argument(
        "--jql",
        help="JQL query, or a bare project key (e.g. SIT). "
        "Omit to use the configured default project/JQL.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="max issues (defaults to JIRA_DEFAULT_LIMIT, hard cap 100)",
    )
    p.add_argument(
        "--format", choices=["json", "text"], default="json",
        help="json (structured, for agent insight) or text (chat-style)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    svc, settings = _build_service()

    limit = args.limit if args.limit is not None else settings.jira_default_limit

    try:
        result = svc.search_raw(args.jql, limit)
    except (JiraError, SecretError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = {
            "jql": result.jql,
            "total": result.total,
            "issues": [asdict(it) for it in result.issues],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(svc._format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
