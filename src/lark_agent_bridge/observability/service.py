"""Observability service: ties the registry, secret store, and backend clients
together behind two chat-friendly entry points used by the ``/logs`` and
``/projects`` commands.

Built lazily from :class:`Settings`. If observability is disabled or
misconfigured, :func:`build_observability` returns ``None`` and the commands
report a friendly message instead of crashing the bot.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..secrets import SecretError, SecretStore
from .openobserve import LogQueryResult, OpenObserveClient, OpenObserveError, parse_time_range
from .registry import Registry, RegistryError

# Keys worth surfacing first when rendering a log row (if present).
_PREFERRED_FIELDS = ["_timestamp", "level", "severity", "log", "message", "msg"]
_MAX_ROWS_RENDERED = 30  # keep a Lark message readable even if more were fetched
_MAX_FIELD_LEN = 300


class ObservabilityService:
    """Facade over the registry + backend clients for the chat commands."""

    def __init__(self, registry: Registry, openobserve: OpenObserveClient | None):
        self.registry = registry
        self.openobserve = openobserve

    # -- /projects --------------------------------------------------------- #
    def list_projects(self) -> str:
        targets = self.registry.list_targets()
        if not targets:
            return "No projects registered."
        lines = ["📁 **Registered projects:**", ""]
        for t in targets:
            if t.backend == "openobserve":
                where = f"openobserve · org `{t.org}` · stream `{t.stream}`"
            else:
                where = f"grafana · dashboard `{t.dashboard_uid}`"
            lines.append(f"• `{t.name}` — {where} (default {t.default_window})")
        lines.append("")
        lines.append("Query with `/logs <project> [window] [filter]`.")
        return "\n".join(lines)

    # -- /project-add ------------------------------------------------------ #
    def add_project(
        self, name: str, org: str, stream: str, window: str | None = None
    ) -> str:
        """Register a new OpenObserve project and persist it. Returns a reply."""
        try:
            target = self.registry.add_openobserve_project(name, org, stream, window)
        except RegistryError as exc:
            return f"⚠️ {exc}"
        return (
            f"✅ Added project `{target.name}` "
            f"(openobserve · org `{target.org}` · stream `{target.stream}` "
            f"· default {target.default_window}).\n"
            f"Try it: `/logs {target.name}`"
        )

    # -- /project-remove --------------------------------------------------- #
    def remove_project(self, name: str) -> str:
        """Remove a project from the registry and persist. Returns a reply."""
        try:
            removed = self.registry.remove_project(name)
        except RegistryError as exc:
            return f"⚠️ {exc}"
        return f"🗑️ Removed project `{removed}`."

    # -- /logs ------------------------------------------------------------- #
    def query_logs(
        self,
        project: str,
        window: str | None,
        query_text: str | None,
        limit: int,
        start: str | None = None,
        end: str | None = None,
        tz_offset_hours: float = 7.0,
        order: str | None = None,
    ) -> str:
        try:
            target = self.registry.resolve(project)
        except RegistryError as exc:
            return f"⚠️ {exc}"

        # Resolve an absolute range if the user supplied one.
        start_us: int | None = None
        end_us: int | None = None
        if start is not None or end is not None:
            if start is None or end is None:
                return ("⚠️ Absolute range needs both a start and an end, e.g. "
                        "`/logs <project> from 2026-10-19 10:00:00 to 2026-10-19 11:00:00`.")
            try:
                start_us, end_us = parse_time_range(start, end, tz_offset_hours)
            except OpenObserveError as exc:
                return f"⚠️ {exc}"

        if target.backend == "openobserve":
            if self.openobserve is None:
                return ("⚠️ OpenObserve is not configured "
                        "(set OPENOBSERVE_BASE_URL and credentials).")
            try:
                result = self.openobserve.search(
                    target, query_text, window, limit,
                    start_us=start_us, end_us=end_us,
                    tz_offset_hours=tz_offset_hours,
                    order=order,
                )
            except (OpenObserveError, SecretError) as exc:
                return f"⚠️ OpenObserve query failed: {exc}"
            return self._format_result(target.name, result, query_text)

        if target.backend == "grafana":
            return ("⚠️ Grafana log access isn't wired yet — OpenObserve only for now. "
                    f"(project '{target.name}' is a Grafana project.)")

        return f"⚠️ Unsupported backend '{target.backend}' for project '{project}'."

    # -- /logs (raw, for the CLI / agent insight path) -------------------- #
    def query_logs_raw(
        self,
        project: str,
        window: str | None,
        query_text: str | None,
        limit: int,
        start: str | None = None,
        end: str | None = None,
        tz_offset_hours: float = 7.0,
        order: str | None = None,
    ) -> LogQueryResult:
        """Resolve + search and return the structured :class:`LogQueryResult`.

        Same resolve/search path as :meth:`query_logs` but WITHOUT rendering to a
        chat string — used by the CLI so the agent can read raw rows and produce
        an insight. Raises :class:`OpenObserveError`/:class:`RegistryError` (or
        :class:`SecretError`) on failure rather than returning a friendly string.
        """
        target = self.registry.resolve(project)  # may raise RegistryError

        start_us: int | None = None
        end_us: int | None = None
        if start is not None or end is not None:
            if start is None or end is None:
                raise OpenObserveError(
                    "absolute range needs both a start and an end"
                )
            start_us, end_us = parse_time_range(start, end, tz_offset_hours)

        if target.backend != "openobserve":
            raise OpenObserveError(
                f"project '{project}' backend '{target.backend}' "
                "is not supported by the raw CLI path (openobserve only)"
            )
        if self.openobserve is None:
            raise OpenObserveError(
                "OpenObserve is not configured (base URL / credentials missing)"
            )
        return self.openobserve.search(
            target, query_text, window, limit,
            start_us=start_us, end_us=end_us,
            tz_offset_hours=tz_offset_hours, order=order,
        )

    # -- rendering --------------------------------------------------------- #
    @staticmethod
    def _format_result(project: str, result: LogQueryResult, query_text: str | None) -> str:
        # Absolute ranges come back with a "→" in the label; relative windows
        # are short tokens like "15m". Phrase the header accordingly.
        is_range = "→" in result.window
        when = f"range `{result.window}`" if is_range else f"last `{result.window}`"
        head = (
            f"📑 **{project}** · stream `{result.stream}` · org `{result.org}` "
            f"· {when}"
        )
        if query_text:
            head += f" · filter `{query_text}`"
        if not result.rows:
            empty_what = ("this range" if is_range
                          else f"the last {result.window}")
            return head + f"\n\n_No log entries in {empty_what}._"

        shown = result.rows[:_MAX_ROWS_RENDERED]
        blocks = [head, f"_{result.total} hit(s); showing {len(shown)}._", ""]
        for i, row in enumerate(shown, 1):
            blocks.append(f"**{i}.** {_render_row(row)}")
        if result.total > len(shown):
            blocks.append(f"\n_… {result.total - len(shown)} more not shown "
                          "(narrow the window or add a filter)._")
        return "\n".join(blocks)


def _render_row(row: dict[str, Any]) -> str:
    """Render one log row compactly, preferred fields first."""
    if not isinstance(row, dict):
        return _truncate(str(row))
    parts: list[str] = []
    used = set()
    for key in _PREFERRED_FIELDS:
        if key in row:
            parts.append(f"`{key}`={_truncate(_stringify(row[key]))}")
            used.add(key)
    # a couple of remaining fields for context, deterministic order
    for key in sorted(row.keys()):
        if key in used:
            continue
        parts.append(f"`{key}`={_truncate(_stringify(row[key]))}")
        if len(parts) >= 8:
            break
    return " · ".join(parts)


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _truncate(text: str, limit: int = _MAX_FIELD_LEN) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_observability(settings: Settings) -> ObservabilityService | None:
    """Construct the service from settings, or return ``None`` if unavailable.

    Never raises: any misconfiguration logs nothing and yields ``None`` so the
    bot keeps running; the commands then report a friendly message.
    """
    if not settings.enable_observability:
        return None
    if not settings.observability_registry:
        return None
    try:
        registry = Registry.from_file(settings.observability_registry)
    except RegistryError:
        return None

    openobserve: OpenObserveClient | None = None
    if settings.openobserve_base_url and settings.secrets_path:
        try:
            store = SecretStore(
                settings.secrets_path,
                key_file=settings.secret_key_file or None,
            )
            openobserve = OpenObserveClient(settings.openobserve_base_url, store)
        except SecretError:
            openobserve = None

    return ObservabilityService(registry, openobserve)


__all__ = ["ObservabilityService", "build_observability"]
