"""CLM plugin: HTTP client contract, route_request hook, clm.decide/rank tools.

No real clm-serve — urlopen is faked at the client module, and the
sys.modules "clm_plugin_client" cache lets tests inject the fake before the
tool/hook loaders run (same cache the production loaders rely on).
"""
import asyncio
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

CLM_DIR = Path(__file__).resolve().parent.parent / "plugins" / "clm"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, CLM_DIR / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client():
    mod = _load("clm_plugin_client", "clm_client.py")   # the cached name
    yield mod
    sys.modules.pop("clm_plugin_client", None)
    sys.modules.pop("clm_test_hooks", None)
    sys.modules.pop("clm_test_tool", None)


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


CFG = {"plugins": {"clm": {"route_threshold": 0.6, "route": True}},
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
        seen["url"] = req.full_url
        return _fake_urlopen(CHOICE_OK)(req, timeout)

    monkeypatch.setattr(client, "urlopen", _open)
    answers = client.decide(CFG, "fix my parser",
                            {"route": {"type": "choice", "instructions": "i",
                                       "criteria": {"coding": "c"}}}, 1.0)
    assert answers["route"]["choice"] == "coding"
    assert seen["body"]["model"] == "clm-latest"
    assert seen["url"] == "http://127.0.0.1:8700/v1/systemone"


def test_decide_question_mismatch_raises(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen({"answers": {"x": {}}}))
    with pytest.raises(client.ClmError):
        client.decide(CFG, "s", {"route": {}}, 1.0)


def test_decide_unreachable_raises_with_setup_hint(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=ConnectionRefusedError("nope")))
    with pytest.raises(client.ClmError, match="clm-serve"):
        client.decide(CFG, "s", {"route": {}}, 1.0)


def test_rank_happy_path(client, monkeypatch):
    payload = {"ranked": [{"rank": 1, "candidate": "b", "prob": 0.8},
                          {"rank": 2, "candidate": "a", "prob": 0.2}]}
    seen = {}

    def _open(req, timeout=None):
        seen["body"] = json.loads(req.data)
        seen["url"] = req.full_url
        return _fake_urlopen(payload)(req, timeout)

    monkeypatch.setattr(client, "urlopen", _open)
    out = client.rank(CFG, "ctx", "which is right?", ["a", "b"], 1.0)
    assert out[0]["candidate"] == "b"
    assert seen["url"] == "http://127.0.0.1:8700/v1/rank"
    assert seen["body"]["answers"] == ["a", "b"]


def test_rank_bad_response_raises(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen({"nope": 1}))
    with pytest.raises(client.ClmError):
        client.rank(CFG, "ctx", "q", ["a", "b"], 1.0)


def _hooks(client_mod):
    return _load("clm_test_hooks", "hooks.py")


def test_route_request_above_threshold(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(CHOICE_OK))
    assert _hooks(client).route_request("debug this traceback", CFG) == {
        "tag": "coding", "confidence": 0.9, "source": "clm"}


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
    assert h.route_request("debug", {"plugins": {"clm": {"route": False}},
                                     "models": CFG["models"]}) is None
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=TimeoutError("slow")))
    assert h.route_request("debug", CFG) is None     # silent keyword fallback


def test_clm_decide_tool(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen",
                        _fake_urlopen(boom=ConnectionRefusedError("nope")))
    tool_mod = _load("clm_test_tool", "tools/clm.py")
    tool = tool_mod.ClmDecide()

    class _Ctx:
        config = CFG

    out = asyncio.run(tool.execute({"state": "s", "questions": {"q": {}}},
                                   _Ctx()))
    # Server down → the tool reports the setup hint, not a crash
    assert out.status == "error" and "clm-serve" in out.error
    out = asyncio.run(tool.execute({"state": "", "questions": {"q": {}}}, _Ctx()))
    assert out.status == "error" and "state" in out.error


def test_clm_decide_tool_happy_path(client, monkeypatch):
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(
        {"answers": {"q": {"type": "noul", "noul": 0.93}}}))
    tool_mod = _load("clm_test_tool", "tools/clm.py")
    tool = tool_mod.ClmDecide()

    class _Ctx:
        config = CFG

    out = asyncio.run(tool.execute(
        {"state": "refund please",
         "questions": {"q": {"type": "noul", "instructions": "angry?"}}},
        _Ctx()))
    assert out.status == "ok" and out.result["answers"]["q"]["noul"] == 0.93


def test_clm_rank_tool(client, monkeypatch):
    payload = {"ranked": [{"rank": 1, "candidate": "b", "prob": 0.8},
                          {"rank": 2, "candidate": "a", "prob": 0.2}]}
    monkeypatch.setattr(client, "urlopen", _fake_urlopen(payload))
    tool_mod = _load("clm_test_tool", "tools/clm.py")
    tool = tool_mod.ClmRank()

    class _Ctx:
        config = CFG

    out = asyncio.run(tool.execute(
        {"context": "the bug", "question": "which fix is correct?",
         "answers": ["a", "b"]}, _Ctx()))
    assert out.status == "ok"
    assert out.result["ranked"][0]["candidate"] == "b"
    # validation: fewer than 2 candidates is an error
    out = asyncio.run(tool.execute(
        {"context": "c", "question": "q", "answers": ["only"]}, _Ctx()))
    assert out.status == "error" and "2+" in out.error


def test_manifest_is_valid():
    import yaml
    m = yaml.safe_load((CLM_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert m["name"] == "clm" and m["version"] and m["requires_jaynet"]
