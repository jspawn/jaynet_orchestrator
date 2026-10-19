"""Protocol v1 (harness lessons §2): the typed event registry in
runtime/protocol.py and the versioned /api/v1 surface.

The loop-level test is the meta-guard: a real loop run (fake model, stub
registry/trace — same scaffolding as test_loop_regressions.py, copied per
file) under STRICT=True must emit only registered, valid events — an
unregistered emit site anywhere in runtime/ fails here.
"""
import asyncio
import logging

import httpx
import pytest

from runtime import protocol
from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector

# ---------------------------------------------------------------- registry

_NoneT = type(None)


def _value_for(types):
    """A minimally valid value for one spec field's accepted types."""
    for t in types:
        if t is _NoneT:
            continue
        if t is str:
            return "x"
        if t is bool:
            return True
        if t is int:
            return 1
        if t is float:
            return 1.0
        if t is dict:
            return {}
        if t is list:
            return []
    return None


def _minimal_payload(spec):
    return {f: _value_for(types) for f, types in spec.required.items()}


def test_every_registered_type_accepts_its_minimal_payload():
    assert protocol.EVENTS, "registry must not be empty"
    for etype, spec in protocol.EVENTS.items():
        assert protocol.validate_event(etype, _minimal_payload(spec)) == [], etype


def test_unknown_type_rejected():
    assert protocol.validate_event("no_such_event", {})


def test_missing_required_field_rejected():
    problems = protocol.validate_event("run_start", {})
    assert any("message" in p and "missing" in p for p in problems)


def test_wrong_field_type_rejected():
    assert protocol.validate_event("run_start", {"message": 42})
    # bools are not ints (a True where a count belongs IS a schema bug)
    assert protocol.validate_event("compaction", {"compacted": True})


def test_extra_fields_are_additive_not_errors():
    assert protocol.validate_event(
        "run_start", {"message": "hi", "future_field": {"any": 1}}) == []


def test_token_is_trace_free():
    assert protocol.EVENTS["token"].trace is False
    assert all(spec.trace for etype, spec in protocol.EVENTS.items()
               if etype != "token")


def test_strict_raises_and_lenient_logs(monkeypatch, caplog):
    monkeypatch.setattr(protocol, "STRICT", True)
    with pytest.raises(ValueError, match="unknown event type"):
        protocol.check("bogus", {})
    monkeypatch.setattr(protocol, "STRICT", False)
    protocol._warned.clear()
    with caplog.at_level(logging.WARNING, logger="runtime.protocol"):
        problems = protocol.check("bogus", {})
    assert problems and "unknown event type" in caplog.text
    # warn-once per distinct (type, problem): a second identical check is quiet
    caplog.clear()
    protocol.check("bogus", {})
    assert not caplog.text


def test_build_envelope_shape():
    env = protocol.build_envelope("r1", 7, "run_start", 0, {"message": "hi"})
    assert list(env) == ["v", "run_id", "seq", "ts", "type", "iteration", "data"]
    assert env["v"] == protocol.PROTOCOL_VERSION == 1
    assert (env["run_id"], env["seq"], env["type"], env["iteration"]) == \
        ("r1", 7, "run_start", 0)
    assert env["data"] == {"message": "hi"} and isinstance(env["ts"], float)


# ------------------------------------------------------- loop meta-guard

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}


class _StubTool:
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}


class _Registry:
    def __init__(self, names):
        self._tools = {n: _StubTool(n) for n in names}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)

    def openai_schemas(self, allowed=None):
        return [t.to_openai_schema() for n, t in self._tools.items()
                if allowed is None or n in allowed]


class _Trace:
    def start_run(self, *a, **k): pass
    def log(self, *a, **k): pass
    def finish_run(self, *a, **k): pass


def _runtime(script):
    """Real loop, fake model (copied scaffolding — see test_loop_regressions)."""
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = _Registry(["fs.read", "fs.write"])
    rt.selector = ToolSelector(rt.registry, rt.config)
    rt.trace = _Trace()
    rt.system_prompt = "test"
    rt.skill_catalog = ""
    rt.litellm_base = "http://x:4000"
    rt.model = "local-orchestrator"
    rt.cost_table = {}
    rt.brain_info = {}
    rt.vision_enabled = False
    rt._local_concurrency = {}
    rt._local_aliases = frozenset()
    rt._model_sems = {}
    rt._poll_safe = set()
    turns = list(script)

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt


def test_loop_run_under_strict_emits_only_valid_events(monkeypatch):
    """STRICT=True turns any unregistered/mis-shaped emit site into a raised
    ValueError mid-run; a clean finish plus per-envelope validation is the
    regression net for future emit sites."""
    monkeypatch.setattr(protocol, "STRICT", True)
    script = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "fs.read", "arguments": "{}"}}]},
        {"role": "assistant", "content": "done"},
    ]
    rt = _runtime(script)
    envelopes = []

    async def collect(event):
        envelopes.append(event)

    out = asyncio.run(rt.run("do a thing", on_event=collect))
    assert out["status"] == "ok"
    types = {e["type"] for e in envelopes}
    # the run exercised the core seam: lifecycle, model traffic, cost, tools
    assert {"run_start", "tool_selection", "model_start", "model_turn",
            "cost", "tool_result", "run_finish"} <= types
    for e in envelopes:
        assert set(e) == {"v", "run_id", "seq", "ts", "type", "iteration", "data"}
        assert e["v"] == 1
        assert protocol.validate_event(e["type"], e["data"]) == [], e["type"]


# ------------------------------------------------------------- /api/v1 web

def _tracing_fake_run(app):
    """A runtime.run stand-in that takes the real event/trace path: publishes
    run_start + run_finish envelopes through the launcher's on_event and
    records the run in the trace store, so the status/SSE routes have data."""
    async def fake(msg, **kw):
        rid = kw.get("run_id")
        on_event = kw.get("on_event")
        app.state.runtime.trace.start_run(rid, msg, owner=kw.get("owner"))
        if on_event:
            await on_event(protocol.build_envelope(rid, 1, "run_start", 0,
                                                   {"message": msg}))
            await on_event(protocol.build_envelope(
                rid, 2, "run_finish", 0,
                {"status": "ok", "answer": "v1 says hi"}))
        app.state.runtime.trace.finish_run(
            rid, "ok", "v1 says hi", "",
            {"tokens": {"total": 5}, "cost_usd": 0.001})
        return {"status": "ok", "answer": "v1 says hi"}
    app.state.runtime.run = fake


async def _wait_finished(app, run_id, tries=50):
    """Let the launched task land in the trace store."""
    for _ in range(tries):
        row = app.state.runtime.trace._conn.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row and row[0] != "running":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.asyncio
async def test_v1_create_run_returns_run_id(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        body = (await c.post("/api/v1/runs", json={"message": "hi"})).json()
    assert body["run_id"] and body["v"] == 1


@pytest.mark.asyncio
async def test_v1_slash_message_is_422(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/v1/runs", json={"message": "/help"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_v1_auth_required(web_app):
    app = web_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/api/v1/runs", json={"message": "hi"})).status_code == 401
        assert (await c.get("/api/v1/runs/deadbeef")).status_code == 401
        # per-user jn_ token authenticates, same as the unversioned surface
        tok = app.state.users.create_api_token("admin", "v1")["token"]
        r = await c.post("/api/v1/runs", json={"message": "hi"},
                         headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_v1_run_status_owner_sees_answer(web_app, web_client):
    app = web_app(stub_run=False)
    _tracing_fake_run(app)
    async with web_client(app) as c:
        rid = (await c.post("/api/v1/runs", json={"message": "hi"})).json()["run_id"]
        await _wait_finished(app, rid)
        body = (await c.get(f"/api/v1/runs/{rid}")).json()
    assert body["status"] == "ok" and body["run_id"] == rid and body["v"] == 1
    assert body["owner"] == "admin" and body["final_answer"] == "v1 says hi"
    assert body["total_tokens"] == 5 and body["cost_usd"] == 0.001
    assert body["started_at"] and body["finished_at"]


@pytest.mark.asyncio
async def test_v1_run_status_unknown_is_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        assert (await c.get("/api/v1/runs/nope")).status_code == 404


@pytest.mark.asyncio
async def test_v1_run_status_admin_gets_no_answer(web_app):
    """The global admin bearer reads run metadata, but final_answer stays
    owner-only."""
    app = web_app(env={"JAYNET_WEB_TOKEN": "admintok"}, stub_run=False)
    _tracing_fake_run(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        rid = (await c.post("/api/v1/runs", json={"message": "hi"})).json()["run_id"]
        await _wait_finished(app, rid)
    # a fresh client: the login session cookie must not ride along — the
    # bearer-only identity is what proves final_answer stays owner-only
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c2:
        body = (await c2.get(f"/api/v1/runs/{rid}",
                             headers={"Authorization": "Bearer admintok"})).json()
    assert body["status"] == "ok" and "final_answer" not in body


@pytest.mark.asyncio
async def test_v1_events_replays_and_honors_after_seq(web_app, web_client):
    app = web_app(stub_run=False)
    _tracing_fake_run(app)
    async with web_client(app) as c:
        rid = (await c.post("/api/v1/runs", json={"message": "hi"})).json()["run_id"]
        await _wait_finished(app, rid)
        r = await asyncio.wait_for(c.get(f"/api/v1/runs/{rid}/events"), timeout=10)
        assert r.status_code == 200
        assert '"type": "run_start"' in r.text and '"type": "run_finish"' in r.text
        # ?after_seq= skips the already-seen prefix (additive to Last-Event-ID)
        r = await asyncio.wait_for(
            c.get(f"/api/v1/runs/{rid}/events", params={"after_seq": 1}),
            timeout=10)
        assert r.status_code == 200
        assert '"type": "run_start"' not in r.text
        assert '"type": "run_finish"' in r.text


@pytest.mark.asyncio
async def test_v1_events_unknown_run_404(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        assert (await c.get("/api/v1/runs/nope/events")).status_code == 404


@pytest.mark.asyncio
async def test_v1_cancel_finished_run(web_app, web_client):
    app = web_app(stub_run=False)
    _tracing_fake_run(app)
    async with web_client(app) as c:
        rid = (await c.post("/api/v1/runs", json={"message": "hi"})).json()["run_id"]
        await _wait_finished(app, rid)
        body = (await c.post(f"/api/v1/runs/{rid}/cancel")).json()
    assert body == {"ok": True, "cancelled": False}


@pytest.mark.asyncio
async def test_v1_approve_answer_no_pending(web_app, web_client):
    app = web_app(stub_run=False)
    _tracing_fake_run(app)
    async with web_client(app) as c:
        rid = (await c.post("/api/v1/runs", json={"message": "hi"})).json()["run_id"]
        await _wait_finished(app, rid)
        r = await c.post(f"/api/v1/runs/{rid}/approve",
                         json={"confirmation_id": "x", "approved": True})
        assert r.status_code == 404
        r = await c.post(f"/api/v1/runs/{rid}/answer",
                         json={"ask_id": "x", "answers": {}})
        assert r.status_code == 404
