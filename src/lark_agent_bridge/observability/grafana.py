"""Read-only Grafana log client (CloudWatch Logs datasource).

Only talks to ``POST /api/ds/query`` and ``GET /api/datasources/uid/<uid>/
resources/log-groups`` for the datasource uid pinned in the registry; it never
writes. Credentials (basic auth) come from the secret store at
``grafana.user`` / ``grafana.password``.

Registry fields for a grafana project: ``datasource_uid`` plus ``extra``
``log_group`` and ``region``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from ..secrets import SecretStore
from .openobserve import LogQueryResult, OpenObserveError, parse_window
from .registry import ProjectTarget

MAX_SIZE = 200
_TIMEOUT = 90
_REGEX_SPECIAL = re.compile(r"([\\^$.|?*+()\[\]{}/])")


def _escape_like(text: str) -> str:
    return _REGEX_SPECIAL.sub(r"\\\1", text)


def build_expression(query_text: str | None, order: str, limit: int) -> str:
    parts = ["fields @timestamp, @message"]
    if query_text:
        parts.append(f"filter @message like /{_escape_like(query_text)}/")
    parts.append(f"sort @timestamp {order}")
    parts.append(f"limit {limit}")
    return " | ".join(parts)


class GrafanaClient:
    def __init__(self, base_url: str, store: SecretStore, session: Any = None):
        self.base_url = base_url.rstrip("/")
        self._store = store
        self._session = session or requests.Session()

    def _auth(self) -> tuple[str, str]:
        return self._store.get("grafana.user"), self._store.get("grafana.password")

    def _log_group(self, ds_uid: str, region: str, name: str) -> dict[str, str]:
        try:
            r = self._session.get(
                f"{self.base_url}/api/datasources/uid/{ds_uid}/resources/log-groups",
                params={"region": region, "logGroupNamePrefix": name},
                auth=self._auth(), timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OpenObserveError(f"Grafana unreachable: {exc}") from None
        self._check(r)
        for item in r.json():
            val = item.get("value", {})
            if val.get("name") == name:
                return {"arn": val["arn"], "name": name}
        raise OpenObserveError(f"log group '{name}' not found in {region}")

    @staticmethod
    def _check(r: Any) -> None:
        if r.status_code in (401, 403):
            raise OpenObserveError("Grafana rejected credentials")
        if r.status_code >= 400:
            raise OpenObserveError(f"Grafana error {r.status_code}: {r.text[:200]}")

    def search(
        self,
        target: ProjectTarget,
        query_text: str | None,
        window: str | None,
        limit: int,
        start_us: int | None = None,
        end_us: int | None = None,
        tz_offset_hours: float = 7.0,
        order: str | None = None,
    ) -> LogQueryResult:
        ds_uid = target.datasource_uid
        log_group = target.extra.get("log_group")
        region = target.extra.get("region")
        if not (ds_uid and log_group and region):
            raise OpenObserveError(
                f"project '{target.name}' needs datasource_uid, log_group and region"
            )
        order_sql = "ASC" if (order or "").upper() == "ASC" else "DESC"
        size = max(1, min(int(limit), MAX_SIZE))

        if start_us is not None and end_us is not None:
            start_ms, end_ms = start_us // 1000, end_us // 1000
            label_tz = tz_offset_hours
            label = (
                f"{_fmt(start_ms, label_tz)} → {_fmt(end_ms, label_tz)}"
            )
        else:
            win = window or target.default_window
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - parse_window(win) * 1000
            label = win

        lg = self._log_group(ds_uid, region, str(log_group))
        body = {
            "queries": [{
                "refId": "A",
                "datasource": {"type": "cloudwatch", "uid": ds_uid},
                "queryMode": "Logs",
                "region": region,
                "id": "",
                "expression": build_expression(query_text, order_sql, size),
                "logGroups": [lg],
                "statsGroups": [],
            }],
            "from": str(start_ms),
            "to": str(end_ms),
        }
        try:
            r = self._session.post(
                f"{self.base_url}/api/ds/query", json=body,
                auth=self._auth(), timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise OpenObserveError(f"Grafana unreachable: {exc}") from None
        self._check(r)
        rows = _frames_to_rows(r.json())
        return LogQueryResult(
            stream=str(log_group), org=f"grafana:{region}", window=label,
            total=len(rows), rows=rows, order=order_sql,
        )


def _fmt(ms: int, tz_offset_hours: float) -> str:
    from datetime import timedelta
    tz = timezone(timedelta(hours=tz_offset_hours))
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S")


def _frames_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("results", {}).get("A", {})
    if result.get("error"):
        raise OpenObserveError(f"CloudWatch query failed: {result['error'][:200]}")
    rows: list[dict[str, Any]] = []
    for frame in result.get("frames", []):
        names = [f["name"] for f in frame["schema"]["fields"]]
        cols = frame.get("data", {}).get("values", [])
        if "@timestamp" not in names or "@message" not in names:
            continue
        ti, mi = names.index("@timestamp"), names.index("@message")
        for ts, msg in zip(cols[ti], cols[mi]):
            rows.append({
                "_timestamp": datetime.fromtimestamp(ts / 1000, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "message": msg,
            })
    return rows


__all__ = ["GrafanaClient", "build_expression"]
