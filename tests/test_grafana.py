"""Tests for the Grafana (CloudWatch) log client (mocked HTTP, no network)."""

from __future__ import annotations

import pytest

from lark_agent_bridge.observability.grafana import (
    GrafanaClient,
    build_expression,
)
from lark_agent_bridge.observability.openobserve import OpenObserveError
from lark_agent_bridge.observability.registry import ProjectTarget
from lark_agent_bridge.secrets import SecretStore, generate_key

TARGET = ProjectTarget(
    name="gw", backend="grafana", datasource_uid="ds1",
    extra={"log_group": "/ecs/G", "region": "r1"},
)


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class _Session:
    def __init__(self):
        self.posts = []

    def get(self, url, **kw):
        return _Resp([{"value": {"arn": "arn:x", "name": "/ecs/G"}}])

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        frame = {
            "schema": {"fields": [{"name": "@timestamp"}, {"name": "@message"}]},
            "data": {"values": [[1000], ["hello"]]},
        }
        return _Resp({"results": {"A": {"frames": [frame]}}})


def _client(tmp_path, session):
    key = generate_key()
    store = SecretStore(tmp_path / "s.enc", env_key=key)
    store.set_many({"grafana.user": "u", "grafana.password": "p"})
    return GrafanaClient("https://g.example", store, session=session)


def test_expression_escapes_filter():
    expr = build_expression('a/b"(1', "ASC", 5)
    assert "like /a\\/b\"\\(1/" in expr and "sort @timestamp ASC" in expr


def test_search_maps_rows_and_only_hits_ds_query(tmp_path):
    s = _Session()
    res = _client(tmp_path, s).search(TARGET, "x", "15m", 10, order="asc")
    assert res.rows == [{"_timestamp": "1970-01-01T00:00:01.000Z", "message": "hello"}]
    assert [u for u, _ in s.posts] == ["https://g.example/api/ds/query"]
    assert s.posts[0][1]["queries"][0]["datasource"]["uid"] == "ds1"


def test_missing_config_rejected(tmp_path):
    bad = ProjectTarget(name="gw", backend="grafana", datasource_uid="ds1")
    with pytest.raises(OpenObserveError):
        _client(tmp_path, _Session()).search(bad, None, "15m", 5)
