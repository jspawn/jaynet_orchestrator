"""Model impersonator (/imp): command grammar (runtime/imp.py), the per-user
brain-override store (web/auth.py), the slash endpoint, and the /api/chat run
wiring — model= routing, tighten-only budget layer, ctx guard, and the
dead-slot auto-clear (a local override whose GPU slot was swapped away is
cleared instead of dying on a LiteLLM error, with an in-stream notice).

Endpoint tests drive FastAPI in-process (docs/testing-harness.md) via the
shared conftest web_app/web_client fixtures.
"""
import asyncio
from types import SimpleNamespace

import pytest

import web
import web.server
from runtime import imp as imp_mod
from web.auth import UserStore


# ---- grammar (pure) -----------------------------------------------------------
def test_parse_list_and_stop():
    assert imp_mod.parse("/imp")["action"] == "list"
    assert imp_mod.parse("/imp list")["action"] == "list"
    assert imp_mod.parse("/impstop")["action"] == "stop"
    assert imp_mod.parse("/imp off")["action"] == "stop"
    assert imp_mod.parse("/imp stop")["action"] == "stop"
    assert imp_mod.parse("/impersonate")["action"] == "list"


def test_parse_set_with_options():
    p = imp_mod.parse("/imp tess")
    assert p == {"action": "set", "target": "tess", "budget": None,
                 "ctxguard": None, "confirm": False}
    p = imp_mod.parse("/imp glm-5.2 budget=0.5 ctxguard=200000 confirm")
    assert p["action"] == "set" and p["target"] == "glm-5.2"
    assert p["budget"] == 0.5 and p["ctxguard"] == 200000 and p["confirm"] is True


def test_parse_rejects_bad_options():
    assert imp_mod.parse("/imp tess budget=abc")["action"] == "error"
    assert imp_mod.parse("/imp tess budget=-1")["action"] == "error"
    assert imp_mod.parse("/imp tess ctxguard=0")["action"] == "error"
    assert imp_mod.parse("/imp tess bogus")["action"] == "error"
    assert imp_mod.parse("/imp tess top_p=0.9")["action"] == "error"


def test_is_imp_ignores_lookalikes():
    assert imp_mod.is_imp("/imp")
    assert imp_mod.is_imp("/imp tess")
    assert imp_mod.is_imp("/impersonate kimi-k3")
    assert imp_mod.is_imp("/impstop")
    assert not imp_mod.is_imp("/important question")      # no false positive
    assert not imp_mod.is_imp("/improve this")
    assert not imp_mod.is_imp("hello /imp")


# ---- store --------------------------------------------------------------------
def test_brain_override_store_roundtrip(tmp_path):
    s = UserStore(str(tmp_path / "users.db"))
    s.create("alice", "pw")
    assert s.get_brain_override("alice") == {}
    s.set_brain_override("alice", {"alias": "kimi-k3", "kind": "cloud",
                                   "label": "kimi-k3", "budget": 0.5,
                                   "ctxguard": 200000, "evil_key": "drop me"})
    ov = s.get_brain_override("alice")
    assert ov == {"alias": "kimi-k3", "kind": "cloud", "label": "kimi-k3",
                  "budget": 0.5, "ctxguard": 200000}     # unknown keys dropped
    s.set_brain_override("alice", None)                    # clear
    assert s.get_brain_override("alice") == {}
    assert s.get_brain_override("nobody") == {}


# ---- endpoint helpers (local: this file's _record_run fires a run_start event --
#      unlike the plainer conftest record_run) -----------------------------------
async def _chat_reply(c, message):
    """POST /api/chat and read the whole SSE replay (slash replies are
    model-less canned runs, same shape as the fast-path)."""
    r = await c.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    rid = r.json()["run_id"]
    r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert r.status_code == 200
    return r.text


def _record_run(app):
    """Capture the kwargs runtime.run is called with (the run itself is faked)."""
    seen = {}

    async def rec(msg, **kw):
        seen.update(kw)
        if kw.get("on_event"):      # minimal envelope: lets the imp notice fire
            await kw["on_event"]({"type": "run_start", "seq": 1})
        return {}

    app.state.runtime.run = rec
    return seen


async def _chat_run(c, seen, payload):
    r = await c.post("/api/chat", json=payload)
    assert r.status_code == 200
    for _ in range(100):
        if seen:
            return r.json()["run_id"]
        await asyncio.sleep(0.02)
    raise AssertionError("run was never invoked")


# ---- /imp endpoint -------------------------------------------------------------
@pytest.mark.asyncio
async def test_imp_list_shows_presets_and_cloud(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

    class _FakeList:
        async def execute(self, args, ctx):
            return SimpleNamespace(status="ok", result={"presets": [
                {"preset": "coder", "role": "allround specialist - x",
                 "alias": "local-coder", "gpu": "1", "port": 8080,
                 "vram_gib": 30, "live": True},
                {"preset": "tess", "role": "coding - alt",
                 "alias": "local-coder", "gpu": "1", "port": 8080,
                 "vram_gib": 24, "live": False}]})
    monkeypatch.setattr(web.server, "ModelList", _FakeList)
    monkeypatch.setattr(web.server, "_litellm_model_ids",
                        lambda rt: _async({"local-orchestrator", "local-coder",
                                           "kimi-k3", "glm-5.2"}))
    async with web_client(app) as c:
        text = await _chat_reply(c, "/imp list")
    assert "coder" in text and "tess" in text          # local presets
    assert "kimi-k3" in text and "glm-5.2" in text     # cloud aliases
    assert "local-orchestrator" in text                # named as the default brain
    assert "budget=" in text and "ctxguard=" in text   # option docs


def _async(val):
    async def inner():
        return val
    return inner()


@pytest.mark.asyncio
async def test_imp_cloud_requires_explicit_confirm(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    monkeypatch.setattr(web.server, "_litellm_model_ids",
                        lambda rt: _async({"kimi-k3", "local-orchestrator"}))
    async with web_client(app) as c:
        text = await _chat_reply(c, "/imp kimi-k3")
        assert "leaves this box" in text               # the privacy gate
        assert app.state.users.get_brain_override("admin") == {}   # not stored
        text = await _chat_reply(c, "/imp kimi-k3 confirm")
        assert "impersonating" in text
        ov = app.state.users.get_brain_override("admin")
        assert ov["alias"] == "kimi-k3" and ov["kind"] == "cloud"
        me = (await c.get("/api/me")).json()           # the badge reads /api/me
        assert me["brain_override"]["alias"] == "kimi-k3"


@pytest.mark.asyncio
async def test_imp_unknown_model_and_default_brain(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    monkeypatch.setattr(web.server, "_litellm_model_ids",
                        lambda rt: _async({"kimi-k3"}))
    async with web_client(app) as c:
        text = await _chat_reply(c, "/imp nosuchmodel")
        assert "unknown model" in text
        text = await _chat_reply(c, "/imp kimi-k3 budget=0.25 confirm")
        ov = app.state.users.get_brain_override("admin")
        assert ov["budget"] == 0.25
        # impersonating the default brain is a no-op with an explanation
        monkeypatch.setattr(web.server, "_litellm_model_ids",
                            lambda rt: _async({"local-orchestrator"}))
        text = await _chat_reply(c, "/imp local-orchestrator")
        assert "already IS the default brain" in text


@pytest.mark.asyncio
async def test_imp_local_set_uses_model_use_and_impstop_clears(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    calls = []

    class _FakeUse:
        async def execute(self, args, ctx):
            calls.append(args)
            return SimpleNamespace(status="ok", result={
                "alias": "local-coder", "status": "already serving on :8080"})
    monkeypatch.setattr(web.server, "ModelUse", _FakeUse)
    async with web_client(app) as c:
        text = await _chat_reply(c, "/imp tess")
        assert "impersonating" in text and "local-coder" in text
        # typing the command IS the swap decision: model.use(swap:true), no gate
        assert calls == [{"preset": "tess", "swap": True}]
        ov = app.state.users.get_brain_override("admin")
        assert ov == {"alias": "local-coder", "label": "tess", "kind": "local",
                      "preset": "tess"}
        text = await _chat_reply(c, "/impstop")
        assert "impersonation stopped" in text
        assert app.state.users.get_brain_override("admin") == {}


@pytest.mark.asyncio
async def test_imp_local_slot_busy_reports_hint(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

    class _BusyUse:
        async def execute(self, args, ctx):
            return SimpleNamespace(status="ok", result={
                "alias": "local-coder", "status": "slot busy — different model",
                "hint": "port 8080 is serving 'other', not 'tess'"})
    monkeypatch.setattr(web.server, "ModelUse", _BusyUse)
    async with web_client(app) as c:
        text = await _chat_reply(c, "/imp tess")
        assert "slot busy" in text
        assert app.state.users.get_brain_override("admin") == {}   # not stored


# ---- run wiring -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_run_uses_override_model_budget_ctxguard(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    app.state.runtime.config["budgets"]["max_cost_usd"] = 1.0
    app.state.users.set_brain_override("admin", {
        "alias": "kimi-k3", "kind": "cloud", "budget": 0.5, "ctxguard": 200000})
    seen = _record_run(app)
    async with web_client(app) as c:
        await _chat_run(c, seen, {"message": "work"})
    assert seen["model"] == "kimi-k3"
    assert seen["budget_overrides"]["max_cost_usd"] == 0.5   # tightened 1.0 -> 0.5
    assert seen["run_overrides"]["context_tokens"] == 200000


@pytest.mark.asyncio
async def test_dead_slot_auto_clears_with_notice(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    app.state.users.set_brain_override("admin", {
        "alias": "local-coder", "label": "tess", "kind": "local", "preset": "tess"})
    monkeypatch.setattr(web.server, "_imp_local_alive",
                        lambda rt, imp: _async(False))   # GPU slot was swapped away
    seen = _record_run(app)
    async with web_client(app) as c:
        rid = await _chat_run(c, seen, {"message": "work"})
        for _ in range(100):                             # let the run task publish
            buf = app.state.bus._buffer.get(rid) or []
            if any(e.get("type") == "model_turn" for e in buf):
                break
            await asyncio.sleep(0.02)
    assert seen.get("model") is None                     # default brain instead
    assert app.state.users.get_brain_override("admin") == {}   # override cleared
    buf = app.state.bus._buffer.get(rid) or []
    notice = [e for e in buf if e.get("type") == "model_turn"
              and (e.get("data") or {}).get("model") == "imp"]
    assert notice and "no longer live on its slot" in notice[0]["data"]["content"]


@pytest.mark.asyncio
async def test_me_exposes_imp_models_for_slash_completion(web_app, web_client):
    """`/imp ` completion in the composer reads /api/me.imp_models: local
    preset names (minus the default brain) + cloud aliases from the cost
    table (static, no proxy probe)."""
    app = web_app()
    async with web_client(app) as c:
        me = (await c.get("/api/me")).json()
    im = me["imp_models"]
    assert "tess" in im["local"] and "coder" in im["local"]
    assert "brain" not in im["local"]               # the default brain is no /imp target
    assert "embed" not in im["local"] and "rerank" not in im["local"]  # no chat alias
    assert "kimi-k3" in im["cloud"] and "glm-5.2" in im["cloud"]
    assert not any(a.startswith("local-") for a in im["cloud"])


@pytest.mark.asyncio
async def test_alive_local_override_routes_to_alias(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    app.state.users.set_brain_override("admin", {
        "alias": "local-coder", "label": "tess", "kind": "local", "preset": "tess"})
    monkeypatch.setattr(web.server, "_imp_local_alive",
                        lambda rt, imp: _async(True))
    seen = _record_run(app)
    async with web_client(app) as c:
        await _chat_run(c, seen, {"message": "work"})
    assert seen["model"] == "local-coder"
    assert app.state.users.get_brain_override("admin")["alias"] == "local-coder"
