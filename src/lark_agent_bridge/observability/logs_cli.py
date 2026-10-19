"""Command-line entrypoint for log access — the agent's "fetch then insight" path.

This is the out-of-band way to run the exact same log query the Lark ``/logs``
command runs, but from a terminal so an agent (or a human) can capture the
result and reason over it. It reuses :func:`build_observability` so the
registry, encrypted secret store, and OpenObserve client are wired identically
to the bot — there is no second code path for querying logs.

Usage::

    python -m lark_agent_bridge.observability.logs_cli \\
        --project mt4api-mt4in2 \\
        --from "2026-10-19 10:00:00" --to "2026-10-19 11:00:00" \\
        --limit 10 --order asc --filter "(110" --format json

    # relative window instead of an absolute range:
    python -m lark_agent_bridge.observability.logs_cli \\
        --project mt4api-dev --window 15m --filter error --format text

    # list registered projects:
    python -m lark_agent_bridge.observability.logs_cli --list

Design / safety:
    - **Read-only.** Only ever calls ``search`` (SELECT); never writes.
    - **No secrets echoed.** Credentials come from the encrypted store via
      ``build_observability``; this CLI prints log rows only, never the key or
      token. (Keep the key file OUTSIDE the repo, per setup.)
    - **Data boundary.** Using this CLI implies the caller WANTS the raw rows in
      hand (e.g. an agent that will summarise them). The Lark ``/logs`` command
      remains the RAW-straight-to-chat path that never passes through an LLM.
    - **Exit codes:** 0 = success (even if zero hits), 2 = user/query error
      (bad project, bad timestamp, creds), 3 = observability disabled/unconfigured.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from ..config import Settings, default_env_path
from ..secrets import SecretError
from .openobserve import OpenObserveError
from .registry import RegistryError
from .service import ObservabilityService, build_observability


def _build_service() -> tuple[ObservabilityService, Settings]:
    """Construct the observability service + settings from the repo ``.env``.

    Raises :class:`SystemExit` (code 3) if observability is disabled or the
    service cannot be built — the same "friendly off" condition the bot handles,
    surfaced here as a clear CLI error.
    """
    settings = Settings(default_env_path())
    svc = build_observability(settings)
    if svc is None:
        print(
            "observability is disabled or misconfigured "
            "(check ENABLE_OBSERVABILITY, OBSERVABILITY_REGISTRY, creds)",
            file=sys.stderr,
        )
        raise SystemExit(3)
    return svc, settings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="logs_cli",
        description="Read-only log query (reuses the bot's observability stack).",
    )
    p.add_argument("--project", help="registered project name (see --list)")
    p.add_argument("--list", action="store_true",
                   help="list registered projects and exit")
    # time range: either a relative window OR an absolute from/to pair
    p.add_argument("--window", help="relative window, e.g. 15m / 1h / 2d")
    p.add_argument("--from", dest="start",
                   help="absolute start, e.g. '2026-10-19 10:00:00' (local/WIB)")
    p.add_argument("--to", dest="end",
                   help="absolute end, e.g. '2026-10-19 11:00:00' (local/WIB)")
    p.add_argument("--filter", dest="filter_text",
                   help="free-text match_all filter, e.g. '(110'")
    p.add_argument("--limit", type=int, default=None,
                   help="max rows (defaults to LOGS_DEFAULT_LIMIT, hard cap 200)")
    p.add_argument("--order", choices=["asc", "desc", "ASC", "DESC"],
                   default="desc",
                   help="timestamp sort: desc=newest-first (default), asc=oldest-first")
    p.add_argument("--format", choices=["json", "text"], default="json",
                   help="json (structured, for agent insight) or text (chat-style)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    svc, settings = _build_service()

    if args.list:
        print(svc.list_projects())
        return 0

    if not args.project:
        print("error: --project is required (or use --list)", file=sys.stderr)
        return 2

    limit = args.limit if args.limit is not None else settings.logs_default_limit

    try:
        result = svc.query_logs_raw(
            project=args.project,
            window=args.window,
            query_text=args.filter_text,
            limit=limit,
            start=args.start,
            end=args.end,
            tz_offset_hours=settings.logs_tz_offset_hours,
            order=args.order,
        )
    except (RegistryError, OpenObserveError, SecretError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = asdict(result)
        payload["project"] = args.project
        payload["filter"] = args.filter_text
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        # Reuse the same renderer the Lark command uses, for parity.
        print(svc._format_result(args.project, result, args.filter_text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
