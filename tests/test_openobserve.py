"""Tests for the OpenObserve client (mocked HTTP, no network)."""

from __future__ import annotations

import pytest

from lark_agent_bridge.observability.openobserve import (
    MAX_SIZE,
    OpenObserveClient,
    OpenObserveError,
    build_sql,
    parse_datetime_local,
    parse_time_range,
    parse_window,
)
from lark_agent_bridge.observability.registry import ProjectTarget
from lark_agent_bridge.secrets import SecretStore, generate_key


# --------------------------- window parsing --------------------------------- #
@pytest.mark.parametrize("win,secs", [
    ("30s", 30), ("15m", 900), ("1h", 3600), ("2d", 172800), (" 5m ", 300),
])
def test_parse_window_ok(win, secs):
    assert parse_window(win) == secs


@pytest.mark.parametrize("win", ["", "abc", "10x", "-5m", "0h", "m"])
def test_parse_window_invalid(win):
    with pytest.raises(OpenObserveError):
        parse_window(win)


# --------------------------- absolute timestamp parsing --------------------- #
def test_parse_datetime_local_wib_to_utc():
    # 2026-10-19 10:00:00 WIB (UTC+7) == 2026-10-19 03:00:00 UTC
    us = parse_datetime_local("2026-10-19 10:00:00", tz_offset_hours=7)
    # 03:00 UTC epoch
    import datetime as _dt
    expect = int(_dt.datetime(2026, 10, 19, 3, 0, 0,
                              tzinfo=_dt.timezone.utc).timestamp() * 1_000_000)
    assert us == expect


@pytest.mark.parametrize("spec", [
    "2026-10-19 10:00:00",
    "2026-10-19T10:00:00",
    "2026-10-19 10:00",
    "2026-10-19",
])
def test_parse_datetime_local_formats(spec):
    assert isinstance(parse_datetime_local(spec), int)


@pytest.mark.parametrize("spec", ["", "not-a-date", "2026/10/19", "10:00:00"])
def test_parse_datetime_local_invalid(spec):
    with pytest.raises(OpenObserveError):
        parse_datetime_local(spec)


def test_parse_time_range_ok():
    s, e = parse_time_range("2026-10-19 10:00:00", "2026-10-19 11:00:00")
    assert s < e
    assert e - s == 3600 * 1_000_000  # exactly one hour in microseconds


def test_parse_time_range_rejects_reversed():
    with pytest.raises(OpenObserveError):
        parse_time_range("2026-10-19 11:00:00", "2026-10-19 10:00:00")


def test_parse_time_range_rejects_equal():
    with pytest.raises(OpenObserveError):
        parse_time_range("2026-10-19 10:00:00", "2026-10-19 10:00:00")


# --------------------------- SQL builder ------------------------------------ #
def test_build_sql_basic():
    sql = build_sql("my_stream", None, 50)
    assert 'FROM "my_stream"' in sql
    assert "LIMIT 50" in sql
    assert "WHERE" not in sql


def test_build_sql_with_filter_escapes_quotes():
    sql = build_sql("s", "it's bad", 10)
    assert "match_all('it''s bad')" in sql  # single quote doubled


def test_build_sql_escapes_stream_quotes():
    sql = build_sql('weird"name', None, 5)
    assert 'FROM "weird""name"' in sql


# --------------------------- order (ASC/DESC) ------------------------------- #
def test_build_sql_default_order_desc():
    sql = build_sql("s", None, 5)
    assert "ORDER BY _timestamp DESC" in sql


@pytest.mark.parametrize("order,expected", [
    ("asc", "ASC"), ("ASC", "ASC"), ("desc", "DESC"), ("DESC", "DESC"),
    (" asc ", "ASC"), (None, "DESC"),
])
def test_build_sql_order_normalised(order, expected):
    sql = build_sql("s", None, 5, order)
    assert f"ORDER BY _timestamp {expected}" in sql


@pytest.mark.parametrize("bad", ["sideways", "ascending", "up", "1"])
def test_build_sql_invalid_order_raises(bad):
    with pytest.raises(OpenObserveError):
        build_sql("s", None, 5, bad)


def test_search_passes_order_asc_to_sql():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    result = client.search(_target(), order="asc")
    assert "ORDER BY _timestamp ASC" in sess.last["json"]["query"]["sql"]
    assert result.order == "ASC"


def test_search_default_order_is_desc():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    result = client.search(_target())
    assert "ORDER BY _timestamp DESC" in sess.last["json"]["query"]["sql"]
    assert result.order == "DESC"


def test_search_invalid_order_raises():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    with pytest.raises(OpenObserveError):
        client.search(_target(), order="bogus")


# --------------------------- client (mocked) -------------------------------- #
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

    def post(self, url, json=None, auth=None, timeout=None):
        self.last = {"url": url, "json": json, "auth": auth, "timeout": timeout}
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _store():
    s = SecretStore("unused", env_key=generate_key())
    s.set_many({
        "openobserve.MTM-DEV.user": "dev@sam.com",
        "openobserve.MTM-DEV.token": "dev-token",
    })
    return s


def _target(org="MTM-DEV", stream="meta_python_mt4dev_connector"):
    return ProjectTarget(
        name="mt4api-dev", backend="openobserve",
        default_window="15m", org=org, stream=stream,
    )


def test_search_happy_path():
    resp = _FakeResponse(payload={
        "hits": [{"_timestamp": 1, "log": "a"}, {"_timestamp": 2, "log": "b"}],
        "total": 2, "took": 12,
    })
    sess = _FakeSession(resp)
    client = OpenObserveClient("https://oo.example.com", _store(), session=sess)

    result = client.search(_target(), query_text="error", window="1h", limit=50)

    assert result.total == 2
    assert len(result.rows) == 2
    assert result.took_ms == 12
    # request shaped correctly
    assert sess.last["url"] == "https://oo.example.com/api/MTM-DEV/_search"
    assert sess.last["auth"] == ("dev@sam.com", "dev-token")
    body = sess.last["json"]["query"]
    assert "match_all('error')" in body["sql"]
    assert body["start_time"] < body["end_time"]
    assert body["size"] == 50


def test_search_absolute_range_overrides_window():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    start_us, end_us = parse_time_range("2026-10-19 10:00:00", "2026-10-19 11:00:00")
    result = client.search(
        _target(), window="15m", start_us=start_us, end_us=end_us,
    )
    body = sess.last["json"]["query"]
    # exact range sent, relative window ignored
    assert body["start_time"] == start_us
    assert body["end_time"] == end_us
    # label reflects an absolute range, not "15m"
    assert "→" in result.window


def test_search_absolute_range_rejects_reversed():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    with pytest.raises(OpenObserveError):
        client.search(_target(), start_us=200, end_us=100)


def test_search_uses_default_window_when_none():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    result = client.search(_target(), window=None)
    assert result.window == "15m"  # from target.default_window


def test_search_caps_size_at_max():
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    client.search(_target(), limit=99999)
    assert sess.last["json"]["query"]["size"] == MAX_SIZE


def test_search_truncates_rows_to_size():
    hits = [{"i": i} for i in range(100)]
    sess = _FakeSession(_FakeResponse(payload={"hits": hits, "total": 100}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    result = client.search(_target(), limit=10)
    assert len(result.rows) == 10


def test_search_401_is_credential_error():
    sess = _FakeSession(_FakeResponse(status_code=401, text="unauthorized"))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    with pytest.raises(OpenObserveError) as exc:
        client.search(_target())
    assert "credential" in str(exc.value).lower()


def test_search_500_is_error():
    sess = _FakeSession(_FakeResponse(status_code=500, text="boom"))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    with pytest.raises(OpenObserveError):
        client.search(_target())


def test_search_transport_error_wrapped():
    import requests
    sess = _FakeSession(requests.RequestException("conn refused"))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    with pytest.raises(OpenObserveError) as exc:
        client.search(_target())
    assert "failed" in str(exc.value).lower()


def test_search_missing_credentials_fails_closed():
    empty = SecretStore("unused", env_key=generate_key())  # no creds set
    sess = _FakeSession(_FakeResponse(payload={"hits": []}))
    client = OpenObserveClient("https://oo", empty, session=sess)
    with pytest.raises(OpenObserveError) as exc:
        client.search(_target())
    assert "credential" in str(exc.value).lower()


def test_search_rejects_non_openobserve_target():
    sess = _FakeSession(_FakeResponse(payload={"hits": []}))
    client = OpenObserveClient("https://oo", _store(), session=sess)
    grafana_target = ProjectTarget(name="gw", backend="grafana")
    with pytest.raises(OpenObserveError):
        client.search(grafana_target)


def test_search_shared_account_fallback():
    s = SecretStore("unused", env_key=generate_key())
    s.set_many({"openobserve.user": "shared", "openobserve.token": "shared-tok"})
    sess = _FakeSession(_FakeResponse(payload={"hits": [], "total": 0}))
    client = OpenObserveClient("https://oo", s, session=sess)
    client.search(_target())  # no per-org creds -> falls back to shared
    assert sess.last["auth"] == ("shared", "shared-tok")
