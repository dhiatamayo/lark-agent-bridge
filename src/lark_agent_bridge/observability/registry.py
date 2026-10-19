"""Project -> log-location registry.

Each SAM project logs to a different place with a different shape (mt4api lives
in OpenObserve under a per-metaserver stream; gateway lives in Grafana under a
dashboard, etc). This registry is the single source of truth mapping a short
project name the user types in chat to the concrete backend + pointer needed to
query it.

The registry is a JSON file kept OUTSIDE version control (it describes internal
infrastructure). JSON is used instead of YAML so there is no extra dependency.

Example ``observability_registry.json``::

    {
      "projects": {
        "mt4api-dev": {
          "backend": "openobserve",
          "org": "MTM-DEV",
          "stream": "meta_python_mt4dev_connector",
          "default_window": "15m"
        },
        "mtm-prd": {
          "backend": "openobserve",
          "org": "MTM-PRD",
          "stream": "mtm_swoole_connector",
          "default_window": "15m"
        },
        "gateway-dev": {
          "backend": "grafana",
          "dashboard_uid": "abc123",
          "datasource_uid": "xyz789",
          "default_window": "1h"
        }
      }
    }

A project name is matched case-insensitively. ``resolve`` fails closed: an
unknown project raises :class:`RegistryError` listing the known projects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_BACKENDS = {"openobserve", "grafana"}
DEFAULT_WINDOW = "15m"
# window token: <positive int><unit s|m|h|d>, matches openobserve.parse_window
_WINDOW_RE = re.compile(r"^\d+[smhd]$", re.IGNORECASE)


class RegistryError(Exception):
    """Raised for missing/invalid registry or unknown project (fail-closed)."""


@dataclass(frozen=True)
class ProjectTarget:
    """Resolved pointer to where a project's logs live."""

    name: str
    backend: str
    default_window: str = DEFAULT_WINDOW
    # backend-specific fields (only the relevant ones are populated)
    org: str | None = None            # openobserve
    stream: str | None = None         # openobserve
    dashboard_uid: str | None = None  # grafana
    datasource_uid: str | None = None  # grafana
    panel_id: int | None = None       # grafana
    extra: dict[str, Any] = field(default_factory=dict)


class Registry:
    """In-memory view of the project registry, loaded from a JSON file."""

    def __init__(self, projects: dict[str, ProjectTarget],
                 source_path: str | Path | None = None):
        # keyed by lowercased name for case-insensitive lookup
        self._projects = projects
        # file this registry was loaded from; required for mutations (add/remove)
        self._source_path: Path | None = (
            Path(source_path).expanduser() if source_path else None
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Registry":
        p = Path(path).expanduser()
        if not p.exists():
            raise RegistryError(f"registry file not found: {p}")
        try:
            # utf-8-sig tolerates a UTF-8 BOM, which Windows editors / PowerShell
            # redirects often prepend and which plain json.loads would reject.
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            raise RegistryError(f"registry is not valid JSON: {exc}") from None
        reg = cls.from_dict(data)
        reg._source_path = p
        return reg

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Registry":
        raw = data.get("projects")
        if not isinstance(raw, dict) or not raw:
            raise RegistryError("registry has no 'projects' map")
        projects: dict[str, ProjectTarget] = {}
        for name, cfg in raw.items():
            projects[name.lower()] = cls._parse_project(name, cfg)
        return cls(projects)

    @staticmethod
    def _parse_project(name: str, cfg: Any) -> ProjectTarget:
        if not isinstance(cfg, dict):
            raise RegistryError(f"project '{name}' config must be an object")
        backend = str(cfg.get("backend", "")).strip().lower()
        if backend not in VALID_BACKENDS:
            raise RegistryError(
                f"project '{name}': backend must be one of {sorted(VALID_BACKENDS)}, "
                f"got '{backend}'"
            )
        window = str(cfg.get("default_window") or DEFAULT_WINDOW)

        known = {"backend", "default_window", "org", "stream",
                 "dashboard_uid", "datasource_uid", "panel_id"}
        extra = {k: v for k, v in cfg.items() if k not in known}

        if backend == "openobserve":
            org = cfg.get("org")
            stream = cfg.get("stream")
            if not org or not stream:
                raise RegistryError(
                    f"project '{name}': openobserve needs 'org' and 'stream'"
                )
            return ProjectTarget(
                name=name, backend=backend, default_window=window,
                org=str(org), stream=str(stream), extra=extra,
            )
        # grafana
        panel = cfg.get("panel_id")
        return ProjectTarget(
            name=name, backend=backend, default_window=window,
            dashboard_uid=cfg.get("dashboard_uid"),
            datasource_uid=cfg.get("datasource_uid"),
            panel_id=int(panel) if panel is not None else None,
            extra=extra,
        )

    def resolve(self, project: str) -> ProjectTarget:
        """Return the target for *project* (case-insensitive). Fail-closed."""
        key = (project or "").strip().lower()
        if key not in self._projects:
            raise RegistryError(
                f"unknown project '{project}'. Known: {', '.join(self.names()) or '(none)'}"
            )
        return self._projects[key]

    def names(self) -> list[str]:
        """Original project names, sorted."""
        return sorted(t.name for t in self._projects.values())

    def list_targets(self) -> list[ProjectTarget]:
        return [self._projects[k] for k in sorted(self._projects)]

    # -- mutation (used by /project-add and /project-remove) --------------- #
    def add_openobserve_project(
        self, name: str, org: str, stream: str, window: str | None = None
    ) -> ProjectTarget:
        """Add (or raise if exists) an OpenObserve project and persist to disk.

        Validates inputs the same way loading does, refuses duplicates, writes
        the whole registry back atomically, and only mutates the in-memory map
        after the file write succeeds. Fail-closed on any problem.
        """
        name = (name or "").strip()
        org = (org or "").strip()
        stream = (stream or "").strip()
        if not name:
            raise RegistryError("project name is required")
        if not org or not stream:
            raise RegistryError("openobserve project needs both org and stream")
        key = name.lower()
        if key in self._projects:
            raise RegistryError(
                f"project '{name}' already exists (remove it first to redefine)"
            )
        window = _validate_window(window) if window else DEFAULT_WINDOW

        target = ProjectTarget(
            name=name, backend="openobserve", default_window=window,
            org=org, stream=stream,
        )
        # Build the new map, persist, then commit in-memory (write-ahead order).
        new_map = dict(self._projects)
        new_map[key] = target
        self._save(new_map)
        self._projects = new_map
        return target

    def remove_project(self, name: str) -> str:
        """Remove a project by name (case-insensitive) and persist. Fail-closed."""
        key = (name or "").strip().lower()
        if key not in self._projects:
            raise RegistryError(
                f"unknown project '{name}'. Known: {', '.join(self.names()) or '(none)'}"
            )
        original = self._projects[key].name
        new_map = {k: v for k, v in self._projects.items() if k != key}
        self._save(new_map)
        self._projects = new_map
        return original

    def _save(self, projects: dict[str, ProjectTarget]) -> None:
        """Serialise *projects* back to the source JSON file atomically."""
        if self._source_path is None:
            raise RegistryError(
                "registry has no backing file; cannot persist changes"
            )
        data = {"projects": {t.name: _target_to_cfg(t)
                             for t in projects.values()}}
        text = json.dumps(data, indent=2, ensure_ascii=False)
        path = self._source_path
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)  # atomic on the same filesystem
        except OSError as exc:
            # best-effort cleanup of the temp file
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RegistryError(f"failed to write registry: {exc}") from None


def _validate_window(window: str) -> str:
    """Return the trimmed window if it matches ``<int><s|m|h|d>``, else raise."""
    w = (window or "").strip()
    if not _WINDOW_RE.match(w):
        raise RegistryError(
            f"invalid window '{window}' (use e.g. 30s, 15m, 1h, 2d)"
        )
    return w.lower()


def _target_to_cfg(t: ProjectTarget) -> dict[str, Any]:
    """Serialise a ProjectTarget back to its registry-file representation."""
    cfg: dict[str, Any] = {"backend": t.backend, "default_window": t.default_window}
    if t.backend == "openobserve":
        cfg["org"] = t.org
        cfg["stream"] = t.stream
    else:  # grafana
        if t.dashboard_uid is not None:
            cfg["dashboard_uid"] = t.dashboard_uid
        if t.datasource_uid is not None:
            cfg["datasource_uid"] = t.datasource_uid
        if t.panel_id is not None:
            cfg["panel_id"] = t.panel_id
    cfg.update(t.extra)  # preserve any custom fields we loaded verbatim
    return cfg


__all__ = ["Registry", "ProjectTarget", "RegistryError", "DEFAULT_WINDOW"]
