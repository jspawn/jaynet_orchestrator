"""Jev plugin: HTTP client contract, route_request hook, jev.decide tool.

No real Open-Jev server — urlopen is faked at the client module, and the
sys.modules "jev_plugin_client" cache lets tests inject the fake before the
tool/hook loaders run (same cache the production loaders rely on).
"""
import asyncio
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

JEV_DIR = Path(__file__).resolve().parent.parent / "plugins" / "jev"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, JEV_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client():
    mod = _load("jev_plugin_client", "jev_client.py")   # the cached name
    yield mod
    sys.modules.pop("jev_plugin_client", None)
    sys.modules.pop("jev_test_hooks", None)
    sys.modules.pop("jev_test_tool", None)


def _fake_urlopen(payload=None, boom=None):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        if boom:
            raise boom
        return _Resp(json.dumps(payload).encode())

    return _open


CFG = {"plugins": {"jev": {"route_threshold": 0.6}},
       "models": {"strengths": {"coding": "code synthesis, debugging",
                                "research": "web research, analysis",
                                "allround": "catch-all"}}}

CHOICE_OK = {"answers": {"route": {
    "type": "choice", "choice": "coding",
    "probabilities": {"coding": 0.9, "research": 0.08, "general": 0.02},
    "confidence": 0.8}}}


def test_decide_happy_path(client, monkeypatch):
    seen = {}

    def _open(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _fake_urlopen(CHOICE_OK)(req, timeout)

    monkeypatch.setattr(client, "urlopen", _open)
    answers = client.decide(CFG, "fix my parser",
                            {"route": {"type": "choice", "instructions": "i",
                                       "criteria": {"coding": "c"}}}, 1.0)
    assert answers["route"]["choice"] == "coding"
    assert seen["body"]["model"] == "open-jev"


def test_decide_question_mismatch_raises(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen({"answers": {"x": {}}}))
    with pytest.raises(client.JevError):
        client.decide(CFG, "s", {"route": {}}, 1.0)


def test_decide_unreachable_raises_with_setup_hint(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=ConnectionRefusedError("nope")))
    with pytest.raises(client.JevError, match="jev.server"):
        client.decide(CFG, "s", {"route": {}}, 1.0)


def _hooks(client_mod):
    return _load("jev_test_hooks", "hooks.py")


def test_route_request_above_threshold(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(CHOICE_OK))
    assert _hooks(client).route_request("debug this traceback", CFG) == "coding"


def test_route_request_general_and_low_confidence(client, monkeypatch):
    general = {"answers": {"route": {
        "type": "choice", "choice": "general",
        "probabilities": {"coding": 0.2, "research": 0.1, "general": 0.7}}}}
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(general))
    h = _hooks(client)
    assert h.route_request("what is the capital of France?", CFG) is None

    weak = {"answers": {"route": {
        "type": "choice", "choice": "coding",
        "probabilities": {"coding": 0.4, "research": 0.35, "general": 0.25}}}}
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(weak))
    assert h.route_request("maybe fix this?", CFG) is None


def test_route_request_allround_not_a_candidate(client, monkeypatch):
    seen = {}

    def _open(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _fake_urlopen(CHOICE_OK)(req, timeout)

    monkeypatch.setattr(client, "urlopen", _open)
    _hooks(client).route_request("debug this", CFG)
    criteria = seen["body"]["questions"]["route"]["criteria"]
    assert "allround" not in criteria and "general" in criteria


def test_route_request_disabled_and_server_down(client, monkeypatch):
    h = _hooks(client)
    assert h.route_request("debug", {"plugins": {"jev": {"route": False}},
                                     "models": CFG["models"]}) is None
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=TimeoutError("slow")))
    assert h.route_request("debug", CFG) is None     # silent keyword fallback


def test_jev_decide_tool(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=ConnectionRefusedError("nope")))
    tool_mod = _load("jev_test_tool", "tools/jev.py")
    tool = tool_mod.JevDecide()

    class _Ctx:
        config = CFG

    out = asyncio.run(tool.execute({"state": "s", "questions": {"q": {}}},
                                   _Ctx()))
    # Server down → the tool reports the setup hint, not a crash
    assert out.status == "error" and "jev.server" in out.error
    out = asyncio.run(tool.execute({"state": "", "questions": {"q": {}}}, _Ctx()))
    assert out.status == "error" and "state" in out.error


def test_jev_decide_tool_happy_path(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(
        {"answers": {"q": {"type": "noul", "noul": 0.93}}}))
    tool_mod = _load("jev_test_tool", "tools/jev.py")
    tool = tool_mod.JevDecide()

    class _Ctx:
        config = CFG

    out = asyncio.run(tool.execute(
        {"state": "refund please",
         "questions": {"q": {"type": "noul", "instructions": "angry?"}}},
        _Ctx()))
    assert out.status == "ok" and out.result["answers"]["q"]["noul"] == 0.93


def test_manifest_is_valid():
    import yaml
    m = yaml.safe_load((JEV_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert m["name"] == "jev" and m["version"] and m["requires_jaynet"]
