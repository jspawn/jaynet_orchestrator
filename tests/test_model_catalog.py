"""model.list / model.use with static-port loading (works on a stateless LiteLLM):
serve onto a fixed port + rely on the static alias; detect wrong-model conflicts."""
import asyncio
import tools.model.catalog as M
from tools.model.catalog import ModelList, ModelUse, _served_matches
from runtime.tool_base import ToolResult

CATALOG = {
    "models": {"presets": {
        "brain":  {"preset": "/p/brain.conf", "alias": "local-orchestrator", "port": 8090, "gpu": "0", "served_id": "qwen3-30b-a3b", "vram_gib": 20},
        "brain2": {"preset": "/p/brain.conf", "alias": "local-orchestrator", "port": 8080, "gpu": "1", "served_id": "qwen3-30b-a3b", "vram_gib": 20},
        "specialist":  {"preset": "/p/specialist.conf", "alias": "local-specialist",         "port": 8080, "gpu": "1", "served_id": "ornith-1.0-35b", "vram_gib": 24},
        "vision": {"preset": "/p/vis.conf",   "alias": "local-vision",        "gpu": "1", "vram_gib": 22},  # no port -> dynamic
    }, "gpus": ["0", "1"], "default_posture": "parallel-brain"},
    "orchestrator": {"model": "local-orchestrator"}, "tools": {"serve": {}},
}


class _Ctx:
    def __init__(self): self.config = CATALOG


class _FakeServe:
    calls = []
    async def execute(self, args, ctx):
        _FakeServe.calls.append(args)
        res = {"state": "running", "port": args.get("port") or 8091}
        if args.get("register"):
            res["litellm_alias"] = args.get("alias")
        return ToolResult(status="ok", tool_name="serve.start", result=res)


def _wire(monkeypatch, live, free, servers=None):
    async def qmi(base):
        for port, sid in live.items():
            if f":{port}" in base:
                return sid
        return None
    monkeypatch.setattr(M.S, "query_model_id", qmi)
    monkeypatch.setattr(M.S, "gpu_free_gib", lambda ctx, g: free.get(str(g)))
    monkeypatch.setattr(M, "_cfg", lambda ctx: {"host": "127.0.0.1", "min_free_vram_gib": 1.0, "default_gpu": "1"})
    monkeypatch.setattr(M, "_state_dir", lambda ctx: "/sd")
    monkeypatch.setattr(M.S, "list_servers", lambda sd: servers or [])
    monkeypatch.setattr(M.S, "pid_alive", lambda pid: True)
    monkeypatch.setattr(M.S, "stop_server", lambda e: True)
    monkeypatch.setattr(M.S, "delete_server", lambda sd, n: None)
    monkeypatch.setattr(M, "ServeStart", _FakeServe)
    _FakeServe.calls = []


def _run(t, args=None): return asyncio.run(t.execute(args or {}, _Ctx()))


def test_served_matches():
    assert _served_matches("qwen3-30b-a3b", {"served_id": "qwen3-30b-a3b"})
    assert not _served_matches("ornith-1.0-35b", {"served_id": "qwen3-30b-a3b"})
    assert _served_matches("anything", {})           # no id -> trust static mapping


def test_list_shows_live_and_mismatch(monkeypatch):
    _wire(monkeypatch, live={8090: "qwen3-30b-a3b", 8080: "ornith-1.0-35b"}, free={"0": 12, "1": 8})
    r = _run(ModelList())
    ps = {x["preset"]: x for x in r.result["presets"]}
    assert ps["brain"]["live"] and ps["brain"]["matches"]
    assert ps["specialist"]["live"] and ps["specialist"]["matches"]
    # :8080 is up but runs the specialist, not a brain — port responds, preset doesn't match
    assert ps["brain2"]["port_up"] and ps["brain2"]["live"] is False and ps["brain2"]["matches"] is False


def test_use_already_serving_no_launch(monkeypatch):
    _wire(monkeypatch, live={8090: "qwen3-30b-a3b"}, free={"0": 12, "1": 30})
    r = _run(ModelUse(), {"preset": "brain"})
    assert r.result["status"] == "already serving on :8090" and not _FakeServe.calls


def test_use_slot_busy_reports_conflict(monkeypatch):
    _wire(monkeypatch, live={8080: "qwen3-30b-a3b"}, free={"1": 30})     # brain2 sitting on :8080
    r = _run(ModelUse(), {"preset": "specialist"})
    assert r.result["status"] == "slot busy — different model" and not _FakeServe.calls
    assert "swap:true" in r.result["hint"]


def test_use_swap_stops_then_serves(monkeypatch):
    _wire(monkeypatch, live={8080: "qwen3-30b-a3b"}, free={"1": 30},
          servers=[{"port": 8080, "pid": 1, "name": "brain2", "litellm_alias": "local-orchestrator"}])
    r = _run(ModelUse(), {"preset": "specialist", "swap": True})
    assert len(_FakeServe.calls) == 1
    c = _FakeServe.calls[0]
    assert c["port"] == 8080 and c["register"] is False and c["preset"] == "/p/specialist.conf"


def test_use_serves_on_fixed_port_no_register(monkeypatch):
    _wire(monkeypatch, live={}, free={"1": 30})                          # nothing on :8080
    r = _run(ModelUse(), {"preset": "specialist"})
    c = _FakeServe.calls[0]
    assert c["port"] == 8080 and c["gpu"] == "1" and c["register"] is False
    assert r.result["alias"] == "local-specialist" and "static :8080" in r.result["note"]


def test_use_vram_insufficient_reports(monkeypatch):
    _wire(monkeypatch, live={}, free={"1": 5})                           # can't fit a 24 GiB specialist
    r = _run(ModelUse(), {"preset": "specialist"})
    assert r.result["status"] == "not enough VRAM" and not _FakeServe.calls


def test_use_brain2_serves_parallel_instance(monkeypatch):
    _wire(monkeypatch, live={}, free={"1": 30})
    r = _run(ModelUse(), {"preset": "brain2"})
    c = _FakeServe.calls[0]
    assert c["port"] == 8080 and c["register"] is False and c["preset"] == "/p/brain.conf"


def test_dynamic_preset_registers_at_runtime(monkeypatch):
    _wire(monkeypatch, live={}, free={"1": 30})
    r = _run(ModelUse(), {"preset": "vision"})                  # no port -> dynamic path
    c = _FakeServe.calls[0]
    assert c["register"] is True and c["alias"] == "local-vision"
    assert r.result["alias"] == "local-vision"


def test_dynamic_already_loaded_fastpath_hits(monkeypatch):
    """The stored litellm_alias is the preset's alias (serve.start registers it
    under args['alias']), so a second model.use short-circuits instead of
    re-probing/re-serving."""
    _wire(monkeypatch, live={}, free={"1": 30},
          servers=[{"port": 8091, "pid": 1, "name": "vision", "gpu": "1",
                    "litellm_alias": "local-vision"}])
    r = _run(ModelUse(), {"preset": "vision"})
    assert r.result["status"] == "already loaded"
    assert r.result["alias"] == "local-vision"
    assert r.result["port"] == 8091 and not _FakeServe.calls


# ---- _stop_on_port (async; blocking probes run in threads) -------------------
def _wire_stop(monkeypatch, servers, frees):
    """frees: iterable of VRAM-free readings (first = before, rest = after)."""
    monkeypatch.setattr(M, "_state_dir", lambda ctx: "/sd")
    monkeypatch.setattr(M.S, "list_servers", lambda sd: servers)
    monkeypatch.setattr(M.S, "pid_alive", lambda pid: True)
    monkeypatch.setattr(M, "_port_open", lambda port: False)      # port closes at once
    it = iter(frees)
    monkeypatch.setattr(M.S, "gpu_free_gib", lambda ctx, g: next(it, None))
    return {"stopped": [], "deleted": []}


def test_stop_on_port_stops_managed_and_waits_for_vram(monkeypatch):
    calls = _wire_stop(monkeypatch,
                       [{"port": 8080, "pid": 1, "name": "s1", "gpu": "1"}],
                       frees=[10.0, 20.0])                        # VRAM freed at 1st recheck
    monkeypatch.setattr(M.S, "stop_server",
                        lambda e: calls["stopped"].append(e["name"]) or True)
    monkeypatch.setattr(M.S, "delete_server",
                        lambda sd, n: calls["deleted"].append(n))
    ok = asyncio.run(M._stop_on_port(_Ctx(), 8080))
    assert ok is True
    assert calls == {"stopped": ["s1"], "deleted": ["s1"]}


def test_stop_on_port_ignores_unmanaged_occupant(monkeypatch):
    calls = _wire_stop(monkeypatch, [], frees=[])                 # nothing in the registry
    monkeypatch.setattr(M.S, "stop_server",
                        lambda e: calls["stopped"].append(e["name"]) or True)
    monkeypatch.setattr(M.S, "delete_server",
                        lambda sd, n: calls["deleted"].append(n))
    ok = asyncio.run(M._stop_on_port(_Ctx(), 8080))
    assert ok is False and calls == {"stopped": [], "deleted": []}
