"""Tests for declarative API connectors (tools/connector).

YAML files build ConnectorTool instances; execution goes through a fake
httpx.AsyncClient (monkeypatched — no network).
"""
from __future__ import annotations

import pytest
import yaml

from runtime.tool_base import ToolContext
from tools.connector import load_connectors

from conftest import run

GOOD = {
    "name": "custom.meteo",
    "description": "current weather",
    "base_url": "https://api.example.ch",
    "auth": {"env": "METEO_KEY", "header": "Authorization: Bearer {value}"},
    "request": {"method": "GET", "path": "/v1/forecast"},
    "params": {"lat": {"type": "number", "required": True},
               "lon": {"type": "number", "required": True},
               "days": {"type": "integer", "default": 3}},
}


def _ctx():
    return ToolContext(request_id="t", config={}, budget=None)


def _write(d, name, doc):
    (d / f"{name}.yaml").write_text(yaml.safe_dump(doc))


class _FakeResponse:
    def __init__(self, status=200, text="BODY"):
        self.status_code = status
        self.text = text


class _FakeClient:
    calls: list = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"method": "GET", "url": url, "params": params,
                           "headers": headers})
        return _FakeResponse()

    async def request(self, method, url, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers})
        return _FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr("tools.connector.httpx.AsyncClient", _FakeClient)
    return _FakeClient


# ---- YAML → tool --------------------------------------------------------------

def test_yaml_builds_tool(tmp_path):
    _write(tmp_path, "meteo", GOOD)
    tools = load_connectors(tmp_path)
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "custom.meteo"
    assert t.description == "current weather"
    props = t.parameters["properties"]
    assert props["lat"]["type"] == "number"
    assert props["days"]["default"] == 3
    assert sorted(t.parameters["required"]) == ["lat", "lon"]
    schema = t.to_openai_schema()
    assert schema["function"]["name"] == "custom.meteo"


def test_defaults_private_get_open(tmp_path):
    _write(tmp_path, "meteo", GOOD)
    t = load_connectors(tmp_path)[0]
    assert t.private is True                    # default true
    assert t.requires_confirmation is False     # auto: GET is open
    assert t.read_only is True


def test_auto_confirm_gates_non_get(tmp_path):
    doc = dict(GOOD, request={"method": "POST", "path": "/v1/report"})
    _write(tmp_path, "report", doc)
    t = load_connectors(tmp_path)[0]
    assert t.requires_confirmation is True
    assert t.read_only is False


def test_explicit_confirm_overrides_auto(tmp_path):
    _write(tmp_path, "open", dict(GOOD, confirm=False,
                                  request={"method": "DELETE", "path": "/x"}))
    _write(tmp_path, "gated", dict(GOOD, name="custom.gated", confirm=True))
    by_name = {t.name: t for t in load_connectors(tmp_path)}
    assert by_name["custom.meteo"].requires_confirmation is False
    assert by_name["custom.gated"].requires_confirmation is True


def test_explicit_private_false(tmp_path):
    _write(tmp_path, "meteo", dict(GOOD, private=False))
    assert load_connectors(tmp_path)[0].private is False


@pytest.mark.parametrize("doc", [
    dict(GOOD, name="nodot"),
    dict(GOOD, base_url="ftp://x"),
    dict(GOOD, request={"method": "YEET", "path": "/x"}),
    dict(GOOD, request={"method": "GET"}),          # no path
])
def test_bad_files_are_skipped(tmp_path, doc):
    _write(tmp_path, "bad", doc)
    _write(tmp_path, "meteo", GOOD)
    tools = load_connectors(tmp_path)
    assert [t.name for t in tools] == ["custom.meteo"]


def test_missing_dir_is_empty(tmp_path):
    assert load_connectors(tmp_path / "nope") == []


# ---- execution (fake httpx) -----------------------------------------------------

def test_get_sends_query_params_and_auth(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("METEO_KEY", "s3cret")
    _write(tmp_path, "meteo", GOOD)
    t = load_connectors(tmp_path)[0]
    res = run(t.execute({"lat": 47.3, "lon": 8.5}, _ctx()))
    assert res.status == "ok"
    assert res.result == "BODY"
    call = fake_http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.example.ch/v1/forecast"
    assert call["params"] == {"lat": 47.3, "lon": 8.5, "days": 3}  # default filled
    assert call["headers"] == {"Authorization": "Bearer s3cret"}


def test_path_param_interpolation(tmp_path, fake_http):
    doc = dict(GOOD, auth=None,
               request={"method": "GET", "path": "/v1/city/{city}"},
               params={"city": {"type": "string", "required": True},
                       "units": {"type": "string", "default": "metric"}})
    _write(tmp_path, "geo", dict(doc, name="custom.geo"))
    t = load_connectors(tmp_path)[0]
    res = run(t.execute({"city": "Zurich"}, _ctx()))
    assert res.status == "ok"
    call = fake_http.calls[0]
    assert call["url"] == "https://api.example.ch/v1/city/Zurich"
    assert call["params"] == {"units": "metric"}     # path param not in query


def test_post_sends_json_body(tmp_path, fake_http):
    doc = dict(GOOD, auth=None,
               request={"method": "POST", "path": "/v1/report"})
    _write(tmp_path, "report", dict(doc, name="custom.report"))
    t = load_connectors(tmp_path)[0]
    res = run(t.execute({"lat": 1.0}, _ctx()))
    assert res.status == "ok"
    call = fake_http.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"lat": 1.0, "days": 3}


def test_missing_env_var_is_clear_error(tmp_path, fake_http, monkeypatch):
    monkeypatch.delenv("METEO_KEY", raising=False)
    _write(tmp_path, "meteo", GOOD)
    t = load_connectors(tmp_path)[0]
    res = run(t.execute({"lat": 1, "lon": 2}, _ctx()))
    assert res.status == "error"
    assert "METEO_KEY" in res.error
    assert fake_http.calls == []                     # no request attempted


def test_non_2xx_is_error(tmp_path, fake_http, monkeypatch):
    class FailClient(_FakeClient):
        async def get(self, url, params=None, headers=None):
            return _FakeResponse(status=503, text="upstream broke")
    monkeypatch.setattr("tools.connector.httpx.AsyncClient", FailClient)
    _write(tmp_path, "meteo", dict(GOOD, auth=None))
    t = load_connectors(tmp_path)[0]
    res = run(t.execute({"lat": 1, "lon": 2}, _ctx()))
    assert res.status == "error"
    assert "503" in res.error
