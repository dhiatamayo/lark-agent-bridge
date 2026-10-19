"""Read-only OpenObserve log client.

Queries the OpenObserve Search API for a resolved :class:`ProjectTarget`
(backend ``openobserve``). Read-only: it only issues ``_search`` (SELECT); it
never writes.

Search API (per OpenObserve docs)::

    POST {base_url}/api/{org}/_search
    Authorization: Basic <base64(user:token)>
    {
      "query": {
        "sql": "SELECT * FROM \"stream\"",
        "start_time": <epoch microseconds>,
        "end_time":   <epoch microseconds>,
        "size": <int>
      }
    }

Credentials come from the :class:`SecretStore` at ``openobserve.<org>.user`` and
``openobserve.<org>.token`` (per-org, so DEV/PRD/UAT keep separate creds), with
a fallback to ``openobserve.user`` / ``openobserve.token`` for a shared account.

Safety:
    - Free-text filter is passed through ``match_all('...')`` with single quotes
      escaped, so a user's filter cannot break out of the SQL string.
    - The stream name comes from the registry (not user input) but is still
      quote-escaped defensively.
    - Hard ``size`` cap and request timeout; results truncated before returning.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..secrets import SecretError, SecretStore
from .registry import ProjectTarget

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Accepted absolute timestamp formats (date, with/without seconds, 'T' or space).
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)

MAX_SIZE = 200  # hard cap on rows fetched regardless of what the caller asks


class OpenObserveError(Exception):
    """Raised for query/config/transport failures (message is user-safe)."""


def parse_window(window: str) -> int:
    """Parse a window like ``15m`` / ``1h`` / ``30s`` / ``2d`` into seconds."""
    m = _WINDOW_RE.match(window or "")
    if not m:
        raise OpenObserveError(
            f"invalid window '{window}' (use e.g. 30s, 15m, 1h, 2d)"
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    if value <= 0:
        raise OpenObserveError(f"window must be positive, got '{window}'")
    return value * _UNIT_SECONDS[unit]


def parse_datetime_local(text: str, tz_offset_hours: float = 7.0) -> int:
    """Parse an absolute timestamp *text* into epoch microseconds (UTC).

    The timestamp is interpreted in the caller's local timezone (``tz_offset_hours``,
    default WIB = UTC+7) because people type wall-clock time, while OpenObserve
    wants UTC epoch. Accepted shapes (see ``_DATETIME_FORMATS``) include
    ``2026-10-19 10:00:00``, ``2026-10-19 10:00``, ``2026-10-19T10:00:00`` and a
    bare date ``2026-10-19`` (midnight).
    """
    s = (text or "").strip()
    parsed: datetime | None = None
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise OpenObserveError(
            f"invalid timestamp '{text}' "
            "(use e.g. '2026-10-19 10:00:00' or '2026-10-19')"
        )
    tz = timezone(timedelta(hours=tz_offset_hours))
    aware = parsed.replace(tzinfo=tz)
    return int(aware.timestamp() * 1_000_000)


def parse_time_range(
    start: str, end: str, tz_offset_hours: float = 7.0
) -> tuple[int, int]:
    """Parse an absolute ``start``/``end`` pair into (start_us, end_us) UTC.

    Both are interpreted in local time (``tz_offset_hours``). Raises
    :class:`OpenObserveError` if start is not strictly before end.
    """
    start_us = parse_datetime_local(start, tz_offset_hours)
    end_us = parse_datetime_local(end, tz_offset_hours)
    if start_us >= end_us:
        raise OpenObserveError(
            f"start '{start}' must be before end '{end}'"
        )
    return start_us, end_us


def _escape_sql_literal(text: str) -> str:
    """Escape single quotes for safe embedding in a SQL string literal."""
    return text.replace("'", "''")


def _normalize_order(order: str | None) -> str:
    """Return a validated SQL sort direction (``ASC``/``DESC``, default DESC).

    ``DESC`` = newest-first (default, "latest N logs"); ``ASC`` = oldest-first
    ("first N logs in this range"). Anything unrecognised raises so a typo can't
    silently flip the result ordering.
    """
    value = (order or "DESC").strip().upper()
    if value in ("ASC", "DESC"):
        return value
    raise OpenObserveError(f"invalid order '{order}' (use ASC or DESC)")


def build_sql(
    stream: str, query_text: str | None, size: int, order: str | None = None
) -> str:
    """Build a read-only SELECT for *stream*, optional full-text *query_text*.

    *order* sorts by ``_timestamp``: ``DESC`` (default, newest-first) or ``ASC``
    (oldest-first, i.e. the "first" rows in a range).
    """
    stream_safe = stream.replace('"', '""')
    direction = _normalize_order(order)
    sql = f'SELECT * FROM "{stream_safe}"'
    if query_text:
        sql += f" WHERE match_all('{_escape_sql_literal(query_text)}')"
    sql += f" ORDER BY _timestamp {direction} LIMIT {int(size)}"
    return sql


@dataclass
class LogQueryResult:
    stream: str
    org: str
    window: str
    total: int          # how many rows OpenObserve reported / returned
    rows: list[dict[str, Any]]
    took_ms: int | None = None
    order: str = "DESC"  # _timestamp sort direction used for this query


class OpenObserveClient:
    """Thin read-only wrapper over the OpenObserve Search API."""

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

    def _auth(self, org: str) -> tuple[str, str]:
        """Resolve (user, token) for *org*, falling back to a shared account."""
        try:
            user = self.secrets.get_default(
                f"openobserve.{org}.user"
            ) or self.secrets.get("openobserve.user")
            token = self.secrets.get_default(
                f"openobserve.{org}.token"
            ) or self.secrets.get("openobserve.token")
        except SecretError as exc:
            raise OpenObserveError(
                f"no OpenObserve credentials for org '{org}': {exc}"
            ) from None
        return str(user), str(token)

    def search(
        self,
        target: ProjectTarget,
        query_text: str | None = None,
        window: str | None = None,
        limit: int = 50,
        start_us: int | None = None,
        end_us: int | None = None,
        tz_offset_hours: float = 7.0,
        order: str | None = None,
    ) -> LogQueryResult:
        """Run a read-only search for *target*.

        Time range is either relative (*window*, e.g. ``1h``) or absolute
        (*start_us*/*end_us*, epoch microseconds). When both ``start_us`` and
        ``end_us`` are given they take precedence over *window*.

        *order* controls ``_timestamp`` sort: ``DESC`` (default, newest-first)
        or ``ASC`` (oldest-first — the "first" rows in the range).

        Raises :class:`OpenObserveError` on any failure (never a raw requests
        exception), with a message safe to show in chat.
        """
        if target.backend != "openobserve":
            raise OpenObserveError(
                f"project '{target.name}' is not an OpenObserve project"
            )
        org = target.org or ""
        stream = target.stream or ""
        if not org or not stream:
            raise OpenObserveError(f"project '{target.name}' missing org/stream")

        size = max(1, min(int(limit), MAX_SIZE))
        direction = _normalize_order(order)

        if start_us is not None and end_us is not None:
            # Absolute range wins; label it human-readably for the reply.
            if start_us >= end_us:
                raise OpenObserveError("start time must be before end time")
            range_start, range_end = start_us, end_us
            win_label = _format_range_label(start_us, end_us, tz_offset_hours)
        else:
            win = window or target.default_window
            window_seconds = parse_window(win)
            range_end = int(time.time() * 1_000_000)
            range_start = range_end - window_seconds * 1_000_000
            win_label = win

        sql = build_sql(stream, query_text, size, direction)

        user, token = self._auth(org)
        url = f"{self.base_url}/api/{org}/_search"
        payload = {
            "query": {
                "sql": sql,
                "start_time": range_start,
                "end_time": range_end,
                "size": size,
            }
        }
        try:
            resp = self._session.post(
                url, json=payload, auth=(user, token), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise OpenObserveError(f"request to OpenObserve failed: {exc}") from None

        if resp.status_code == 401 or resp.status_code == 403:
            raise OpenObserveError(
                f"OpenObserve rejected credentials for org '{org}' (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise OpenObserveError(
                f"OpenObserve returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError:
            raise OpenObserveError("OpenObserve returned a non-JSON response") from None

        hits = data.get("hits", [])
        if not isinstance(hits, list):
            hits = []
        rows = hits[:size]
        return LogQueryResult(
            stream=stream,
            org=org,
            window=win_label,
            total=int(data.get("total", len(rows))),
            rows=rows,
            took_ms=data.get("took"),
            order=direction,
        )


def _format_range_label(start_us: int, end_us: int, tz_offset_hours: float = 7.0) -> str:
    """Render an absolute range back into readable local wall-clock time."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    s = datetime.fromtimestamp(start_us / 1_000_000, tz)
    e = datetime.fromtimestamp(end_us / 1_000_000, tz)
    sign = "+" if tz_offset_hours >= 0 else "-"
    off = f"UTC{sign}{abs(tz_offset_hours):g}"
    if s.date() == e.date():
        return f"{s:%Y-%m-%d %H:%M:%S} → {e:%H:%M:%S} ({off})"
    return f"{s:%Y-%m-%d %H:%M:%S} → {e:%Y-%m-%d %H:%M:%S} ({off})"


__all__ = [
    "OpenObserveClient",
    "OpenObserveError",
    "LogQueryResult",
    "parse_window",
    "parse_datetime_local",
    "parse_time_range",
    "build_sql",
    "MAX_SIZE",
]
