"""Tests for the observability project registry."""

from __future__ import annotations

import json

import pytest

from lark_agent_bridge.observability.registry import (
    Registry,
    RegistryError,
)

SAMPLE = {
    "projects": {
        "mt4api-dev": {
            "backend": "openobserve",
            "org": "MTM-DEV",
            "stream": "meta_python_mt4dev_connector",
            "default_window": "15m",
        },
        "mtm-prd": {
            "backend": "openobserve",
            "org": "MTM-PRD",
            "stream": "mtm_swoole_connector",
        },
        "gateway-dev": {
            "backend": "grafana",
            "dashboard_uid": "abc123",
            "datasource_uid": "xyz789",
            "panel_id": 4,
            "default_window": "1h",
        },
    }
}


def test_resolve_openobserve_project():
    reg = Registry.from_dict(SAMPLE)
    t = reg.resolve("mt4api-dev")
    assert t.backend == "openobserve"
    assert t.org == "MTM-DEV"
    assert t.stream == "meta_python_mt4dev_connector"
    assert t.default_window == "15m"


def test_resolve_is_case_insensitive():
    reg = Registry.from_dict(SAMPLE)
    assert reg.resolve("MT4API-DEV").stream == "meta_python_mt4dev_connector"
    assert reg.resolve("Mt4Api-Dev").org == "MTM-DEV"


def test_default_window_fallback():
    reg = Registry.from_dict(SAMPLE)
    assert reg.resolve("mtm-prd").default_window == "15m"  # not set -> default


def test_resolve_grafana_project():
    reg = Registry.from_dict(SAMPLE)
    t = reg.resolve("gateway-dev")
    assert t.backend == "grafana"
    assert t.dashboard_uid == "abc123"
    assert t.panel_id == 4
    assert t.default_window == "1h"


def test_unknown_project_fails_closed():
    reg = Registry.from_dict(SAMPLE)
    with pytest.raises(RegistryError) as exc:
        reg.resolve("nope")
    # error lists known projects to help the user
    assert "mt4api-dev" in str(exc.value)


def test_names_sorted_original_case():
    reg = Registry.from_dict(SAMPLE)
    assert reg.names() == ["gateway-dev", "mt4api-dev", "mtm-prd"]


def test_empty_projects_fails():
    with pytest.raises(RegistryError):
        Registry.from_dict({"projects": {}})
    with pytest.raises(RegistryError):
        Registry.from_dict({})


def test_invalid_backend_fails():
    with pytest.raises(RegistryError):
        Registry.from_dict({"projects": {"x": {"backend": "elasticsearch"}}})


def test_openobserve_missing_stream_fails():
    with pytest.raises(RegistryError):
        Registry.from_dict({"projects": {"x": {"backend": "openobserve", "org": "O"}}})


def test_from_file_roundtrip(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    reg = Registry.from_file(p)
    assert reg.resolve("mt4api-dev").org == "MTM-DEV"


def test_from_file_missing_fails(tmp_path):
    with pytest.raises(RegistryError):
        Registry.from_file(tmp_path / "nope.json")


def test_from_file_bad_json_fails(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError):
        Registry.from_file(p)


def test_extra_fields_preserved():
    data = {"projects": {"x": {
        "backend": "openobserve", "org": "O", "stream": "S",
        "time_field": "_timestamp", "note": "custom",
    }}}
    reg = Registry.from_dict(data)
    t = reg.resolve("x")
    assert t.extra["time_field"] == "_timestamp"
    assert t.extra["note"] == "custom"


# --- mutation: add / remove / persist -------------------------------------- #
def _load(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(SAMPLE), encoding="utf-8")
    return p, Registry.from_file(p)


def test_add_project_persists(tmp_path):
    p, reg = _load(tmp_path)
    t = reg.add_openobserve_project("mt4api-uat", "MTM-UAT", "s_uat", "1h")
    assert t.backend == "openobserve"
    assert t.default_window == "1h"
    # in-memory reflects it
    assert reg.resolve("mt4api-uat").stream == "s_uat"
    # and so does a fresh reload from the file (actually written)
    reg2 = Registry.from_file(p)
    assert reg2.resolve("mt4api-uat").org == "MTM-UAT"
    assert reg2.resolve("mt4api-uat").default_window == "1h"


def test_add_project_default_window(tmp_path):
    _, reg = _load(tmp_path)
    t = reg.add_openobserve_project("x-uat", "O", "S")
    assert t.default_window == "15m"


def test_add_duplicate_fails_and_does_not_write(tmp_path):
    p, reg = _load(tmp_path)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(RegistryError) as exc:
        reg.add_openobserve_project("mt4api-dev", "O", "S")  # already exists
    assert "already exists" in str(exc.value)
    assert p.read_text(encoding="utf-8") == before  # untouched


def test_add_is_case_insensitive_duplicate(tmp_path):
    _, reg = _load(tmp_path)
    with pytest.raises(RegistryError):
        reg.add_openobserve_project("MT4API-DEV", "O", "S")


def test_add_missing_org_or_stream_fails(tmp_path):
    _, reg = _load(tmp_path)
    with pytest.raises(RegistryError):
        reg.add_openobserve_project("x", "", "S")
    with pytest.raises(RegistryError):
        reg.add_openobserve_project("x", "O", "")


def test_add_invalid_window_fails(tmp_path):
    _, reg = _load(tmp_path)
    with pytest.raises(RegistryError) as exc:
        reg.add_openobserve_project("x", "O", "S", "15minutes")
    assert "invalid window" in str(exc.value).lower()


def test_remove_project_persists(tmp_path):
    p, reg = _load(tmp_path)
    name = reg.remove_project("mt4api-dev")
    assert name == "mt4api-dev"
    with pytest.raises(RegistryError):
        reg.resolve("mt4api-dev")
    # file no longer has it
    reg2 = Registry.from_file(p)
    with pytest.raises(RegistryError):
        reg2.resolve("mt4api-dev")


def test_remove_unknown_fails(tmp_path):
    _, reg = _load(tmp_path)
    with pytest.raises(RegistryError) as exc:
        reg.remove_project("nope")
    assert "unknown project" in str(exc.value).lower()


def test_remove_is_case_insensitive(tmp_path):
    _, reg = _load(tmp_path)
    assert reg.remove_project("MT4API-DEV") == "mt4api-dev"


def test_add_without_backing_file_fails():
    # built from_dict -> no source_path -> cannot persist
    reg = Registry.from_dict(SAMPLE)
    with pytest.raises(RegistryError) as exc:
        reg.add_openobserve_project("x", "O", "S")
    assert "backing file" in str(exc.value).lower()


def test_add_preserves_existing_and_extra_fields(tmp_path):
    # a grafana project with a panel_id + an openobserve one with extras must
    # survive a rewrite triggered by adding a new project.
    data = {"projects": {
        "g": {"backend": "grafana", "dashboard_uid": "d", "panel_id": 7},
        "o": {"backend": "openobserve", "org": "O", "stream": "S", "note": "keep"},
    }}
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    reg = Registry.from_file(p)
    reg.add_openobserve_project("new", "N", "NS")
    reg2 = Registry.from_file(p)
    assert reg2.resolve("g").panel_id == 7
    assert reg2.resolve("o").extra["note"] == "keep"
    assert reg2.resolve("new").stream == "NS"
