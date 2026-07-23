"""Goal feature (/goal): store roundtrip, command grammar, the supervisor loop
(web/goals.py) with a stubbed run-launcher, the goal.* tool seam, the loop's
declaration-sink wiring, and the web surface (slash replies, auto-pause,
/api/me). No network, no LiteLLM — same harness patterns as
test_web_regressions.py / test_loop_regressions.py (copied, not imported)."""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from web import goals as goals_mod
from web.auth import UserStore
from web.store import ChatStore

ROOT = Path(__file__).resolve().parent.parent


# ---- UserStore goal record ----------------------------------------------------
def _users(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
    return UserStore(str(tmp_path / "users.db"))


def test_goal_store_roundtrip(tmp_path, monkeypatch):
    users = _users(tmp_path, monkeypatch)
    assert users.get_goal("admin") == {}
    ok = users.set_goal("admin", {
        "objective": "build X", "criterion": "tests pass", "status": "active",
        "turn": 2, "tokens_total": 1234, "current_run": "r1",
        "started_at": "2026-07-23T10:00:00", "evil": "dropped",
        "log": [{"turn": i, "status": "ok", "note": f"n{i}"} for i in range(30)]})
    assert ok
    g = users.get_goal("admin")
    assert g["objective"] == "build X" and g["turn"] == 2
    assert g["tokens_total"] == 1234 and "evil" not in g
    assert len(g["log"]) == 20                          # capped
    assert g["log"][-1]["note"] == "n29"
    users.set_goal("admin", None)                       # clear
    assert users.get_goal("admin") == {}


# ---- grammar + status card ------------------------------------------------------
def test_parse_grammar():
    assert goals_mod.parse("/goal")["action"] == "status"
    assert goals_mod.parse("/goal stop")["action"] == "stop"
    assert goals_mod.parse("/goal pause")["action"] == "pause"
    assert goals_mod.parse("/goal resume")["action"] == "resume"
    p = goals_mod.parse("/goal build the thing")
    assert p["action"] == "start" and p["objective"] == "build the thing"
    assert p["criterion"] == "build the thing"          # defaults to objective
    p = goals_mod.parse("/goal build X | done when: tests pass")
    assert p["objective"] == "build X" and p["criterion"] == "tests pass"
    assert goals_mod.parse("/goal | done when: x")["action"] == "error"


def test_format_status():
    assert "no goal set" in goals_mod.format_status({}, 10)
    card = goals_mod.format_status(
        {"objective": "o", "criterion": "c", "status": "paused", "turn": 3,
         "tokens_total": 42, "log": [{"turn": 3, "status": "ok", "note": "tail"}]}, 10)
    assert "paused" in card and "3/10" in card and "42" in card


# ---- the supervisor loop ---------------------------------------------------------
class _JudgeRuntime:
    def __init__(self, cfg, verdicts):
        self.config = {"goal": cfg}
        self._verdicts = list(verdicts)

    async def complete(self, messages, think=False):
        v = self._verdicts.pop(0) if self._verdicts else "YES — verified"
        return {"content": v, "usage": {}}


def _goal_record(status="active", **kw):
    g = {"objective": "o", "criterion": "c", "status": status, "turn": 0,
         "tokens_total": 0,
         "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "log": []}
    g.update(kw)
    return g


def _deps(tmp_path, monkeypatch, turns, cfg=None, verdicts=(), on_launch=None):
    """Supervisor deps with a stubbed launcher. `turns`: list of
    (result_dict, declaration_or_None) played back in order."""
    users = _users(tmp_path, monkeypatch)
    chats = ChatStore(str(tmp_path / "chats.db"))
    chats.set_current("admin", {"id": None, "cid": "c1", "title": "t",
                                "saved": False, "turns": []})
    calls = []
    queue = list(turns)

    async def launch(**kw):
        calls.append(kw)
        sink = kw["run_overrides_extra"]["goal"]["declarations"]
        if on_launch:
            on_launch(users)
        result, decl = queue.pop(0)
        if decl:
            sink.append(decl)

        async def _run():
            return result
        return f"rid{len(calls)}", asyncio.ensure_future(_run())

    deps = SimpleNamespace(
        runtime=_JudgeRuntime(cfg or {}, verdicts), users=users, chats=chats,
        launch=launch)
    return deps, calls


def _ok(answer="progress", tokens=100):
    return {"status": "ok", "answer": answer,
            "budget": {"tokens": {"total": tokens}}}


@pytest.mark.asyncio
async def test_supervise_completes_with_judge(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok("all done", 123), {"status": "complete",
                                                 "text": "evidence"})],
                        cfg={"max_turns": 5, "judge": True}, verdicts=["YES"])
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "done" and g["tokens_total"] == 123
    assert len(calls) == 1
    assert "Goal mode" in calls[0]["extra_system"]      # directive injected
    row = deps.chats.get_current("admin")
    msgs = [t["user_message"] for t in row["chat"]["turns"]]
    assert msgs[0].startswith("🎯 goal turn 1")
    assert any("✅" in m for m in msgs)
    assert row["active_run"] is None                    # cleared after finish


@pytest.mark.asyncio
async def test_supervise_judge_rejection_forces_another_turn(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok(), {"status": "complete", "text": "maybe"}),
                         (_ok(), {"status": "complete", "text": "really"})],
                        cfg={"max_turns": 5, "judge": True},
                        verdicts=["NO — evidence missing", "YES"])
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "done" and len(calls) == 2
    assert any(e["status"] == "judge" and "rejected" in e["note"]
               for e in g["log"])


@pytest.mark.asyncio
async def test_supervise_blocked_declaration(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok(), {"status": "blocked", "text": "need credentials"})])
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "blocked"
    assert "need credentials" in g["log"][-1]["note"]


@pytest.mark.asyncio
async def test_supervise_two_failures_block(tmp_path, monkeypatch):
    err = {"status": "error", "answer": "", "error": "boom", "budget": {}}
    deps, calls = _deps(tmp_path, monkeypatch, [(err, None), (err, None)],
                        cfg={"max_turns": 5})
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "blocked" and len(calls) == 2
    assert "two turns" in g["log"][-1]["note"]


@pytest.mark.asyncio
async def test_supervise_turn_ceiling(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok(), None), (_ok(), None)],
                        cfg={"max_turns": 2})
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "blocked" and len(calls) == 2
    assert "turn ceiling" in g["log"][-1]["note"]


@pytest.mark.asyncio
async def test_supervise_token_ceiling_stops_before_launch(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch, [(_ok(), None)],
                        cfg={"max_turns": 5, "max_total_tokens": 500})
    deps.users.set_goal("admin", _goal_record(tokens_total=600))
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "blocked" and not calls
    assert "token ceiling" in g["log"][-1]["note"]


@pytest.mark.asyncio
async def test_supervise_pause_mid_run_exits(tmp_path, monkeypatch):
    def _pause(users):
        g = users.get_goal("admin")
        g["status"] = "paused"
        users.set_goal("admin", g)

    deps, calls = _deps(tmp_path, monkeypatch, [(_ok(), None)],
                        cfg={"max_turns": 5}, on_launch=_pause)
    deps.users.set_goal("admin", _goal_record())
    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "paused" and len(calls) == 1  # no further turns


@pytest.mark.asyncio
async def test_supervise_project_binding(tmp_path, monkeypatch):
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok(), {"status": "complete", "text": "done"})],
                        verdicts=["YES"])
    deps.users.set_goal("admin", _goal_record(project_id="p1"))
    await goals_mod.supervise(deps, "admin")
    assert calls[0]["project_id"] == "p1"               # rooted in the project
    assert "project 'p1'" in calls[0]["extra_system"]
    assert deps.users.get_goal("admin")["project_id"] == "p1"


# ---- goal.* tools -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_goal_tools_require_the_seam():
    from tools.goal.declare import GoalBlockedTool, GoalCompleteTool
    r = await GoalCompleteTool().execute({"summary": "x"},
                                         SimpleNamespace(goal_declare=None))
    assert r.status == "error" and "No active goal" in r.error
    got = []
    ctx = SimpleNamespace(goal_declare=lambda s, t: got.append((s, t)))
    r = await GoalCompleteTool().execute({"summary": "evidence"}, ctx)
    assert r.status == "ok" and got == [("complete", "evidence")]
    r = await GoalBlockedTool().execute({"reason": "stuck"}, ctx)
    assert r.status == "ok" and got[-1] == ("blocked", "stuck")
    r = await GoalCompleteTool().execute({"summary": "  "}, ctx)
    assert r.status == "error"                          # empty text rejected


# ---- loop wiring: sink + force-included tools (harness copied from
#      test_loop_regressions.py) -------------------------------------------------
from runtime.loop import AgentRuntime              # noqa: E402
from runtime.selector import ToolSelector          # noqa: E402

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
    # auto mode with no keywords -> a short message selects the trivial minimal
    # set, which does NOT contain goal.* — the sink must add them.
    "tool_selection": {"mode": "auto", "core_namespaces": ["llm"],
                       "keyword_namespaces": {}},
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
    def __init__(self, names, real=None):
        self._tools = {n: _StubTool(n) for n in names}
        self._tools.update(real or {})

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


def _tc(name, arguments):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def _final(text="done"):
    return {"role": "assistant", "content": text}


def _runtime(registry, script):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = registry
    rt.selector = ToolSelector(registry, rt.config)
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
    seen = []

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        seen.append(messages)
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt, seen


@pytest.mark.asyncio
async def test_loop_wires_goal_sink_and_force_includes_tools():
    from tools.goal.declare import GoalBlockedTool, GoalCompleteTool
    # llm.call stub: the auto selector's core matches it, so the frozen set is
    # a real list (not None='all') and the goal tools must be appended to it.
    reg = _Registry(["web.search", "llm.call"],
                    real={"goal.complete": GoalCompleteTool(),
                          "goal.blocked": GoalBlockedTool()})
    rt, _ = _runtime(reg, [_tc("goal.complete", '{"summary": "evidence"}'),
                           _final("wrapped up")])
    events = []

    async def on_event(ev):
        events.append(ev)

    sink = []
    out = await rt.run("continue the goal",
                       run_overrides={"goal": {"declarations": sink}},
                       on_event=on_event)
    assert out["status"] == "ok"
    assert sink == [{"status": "complete", "text": "evidence"}]
    sel = next(e for e in events if e["type"] == "tool_selection")
    assert "goal.complete" in sel["data"]["selected"]
    assert "goal.blocked" in sel["data"]["selected"]


@pytest.mark.asyncio
async def test_loop_without_sink_goal_tools_error_cleanly():
    from tools.goal.declare import GoalBlockedTool, GoalCompleteTool
    reg = _Registry(["web.search"], real={"goal.complete": GoalCompleteTool(),
                                          "goal.blocked": GoalBlockedTool()})
    rt, _ = _runtime(reg, [_tc("goal.complete", '{"summary": "x"}'),
                           _final("ok")])
    events = []

    async def on_event(ev):
        events.append(ev)

    # selector mode "all" (no tool_selection config) exposes every tool; the
    # declaration seam is absent, so the tool must refuse, not crash the run.
    rt.config["tool_selection"] = {"mode": "all"}
    rt.selector = ToolSelector(reg, rt.config)
    out = await rt.run("declare done", on_event=on_event)
    assert out["status"] == "ok"
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["data"]["status"] == "error"
    assert "No active goal" in tr["data"]["error"]


# ---- web surface (harness copied from test_web_regressions.py) -----------------
def _app(tmp_path, monkeypatch):
    base = tmp_path
    (base / "config").mkdir()
    (base / "prompts").mkdir()
    cfg = yaml.safe_load(open(ROOT / "config/runtime.yaml"))
    cfg["trace"]["db_path"] = str(base / "trace.db")
    cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
    cfg["web"] = {"chats_db": str(base / "chats.db"),
                  "users_db": str(base / "users.db"),
                  "outputs_dir": str(base / "outputs"),
                  "projects_dir": str(base / "projects")}
    (base / "prompts" / "orchestrator.md").write_text("P")
    yaml.safe_dump(cfg, open(base / "config" / "runtime.yaml", "w"))
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
    from web.server import create_app
    app = create_app(str(base / "config" / "runtime.yaml"))

    async def fake_run(msg, **kw):      # mock the model — no LiteLLM needed
        return {}
    app.state.runtime.run = fake_run
    return app


@asynccontextmanager
async def _client(app, username="admin", password="pw"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": username,
                                             "password": password})
        assert r.status_code == 200
        yield c


def _record_run(app):
    """Capture the kwargs runtime.run is called with (the run itself is faked)."""
    seen = {}

    async def rec(msg, **kw):
        seen.update(kw)
        return {}

    app.state.runtime.run = rec
    return seen


@pytest.mark.asyncio
async def test_goal_slash_start_runs_to_done(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)

    async def fake_run(msg, **kw):      # the brain declares completion at once
        sink = ((kw.get("run_overrides") or {}).get("goal") or {}).get("declarations")
        if sink is not None:
            sink.append({"status": "complete", "text": "evidence"})
        return {"status": "ok", "answer": "finished",
                "budget": {"tokens": {"total": 10}}}
    app.state.runtime.run = fake_run

    async def judge(messages, think=False):
        return {"content": "YES", "usage": {}}
    app.state.runtime.complete = judge

    async with _client(app) as c:
        r = await c.post("/api/chat",
                         json={"message": "/goal test objective | done when: tested"})
        assert r.status_code == 200
        g = {}
        for _ in range(200):
            g = app.state.users.get_goal("admin")
            if g.get("status") == "done":
                break
            await asyncio.sleep(0.05)
        assert g["status"] == "done"
        assert g["objective"] == "test objective" and g["criterion"] == "tested"
        # No chat existed -> one was auto-created and now holds the goal turns.
        row = app.state.chats.get_current("admin")
        assert row and any("🎯" in (t.get("user_message") or "")
                           for t in row["chat"]["turns"])
        me = (await c.get("/api/me")).json()
        assert me["goal"]["status"] == "done"
        assert me["goal"]["max_turns"] == 10


@pytest.mark.asyncio
async def test_goal_status_and_stop_slash(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.users.set_goal("admin", {"objective": "o", "criterion": "c",
                                       "status": "paused", "turn": 2,
                                       "tokens_total": 5})
    async with _client(app) as c:
        rid = (await c.post("/api/chat", json={"message": "/goal"})).json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "2/" in r.text and "paused" in r.text
        rid = (await c.post("/api/chat",
                            json={"message": "/goal stop"})).json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "stopped" in r.text
        assert app.state.users.get_goal("admin") == {}


@pytest.mark.asyncio
async def test_goal_slash_start_with_project(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)

    async def fake_run(msg, **kw):
        sink = ((kw.get("run_overrides") or {}).get("goal") or {}).get("declarations")
        if sink is not None:
            sink.append({"status": "complete", "text": "evidence"})
        return {"status": "ok", "answer": "finished",
                "budget": {"tokens": {"total": 10}},
                "work_root_seen": kw.get("work_root")}
    app.state.runtime.run = fake_run

    async def judge(messages, think=False):
        return {"content": "YES", "usage": {}}
    app.state.runtime.complete = judge

    async with _client(app) as c:
        r = await c.post("/api/chat", json={
            "message": "/goal build the thing", "project_id": "proj1"})
        assert r.status_code == 200
        g = {}
        for _ in range(200):
            g = app.state.users.get_goal("admin")
            if g.get("status") == "done":
                break
            await asyncio.sleep(0.05)
        assert g["status"] == "done"
        assert g["project_id"] == "proj1"               # bound at /goal time


@pytest.mark.asyncio
async def test_goal_auto_pause_on_user_message(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = _app(tmp_path, monkeypatch)
    seen = _record_run(app)
    app.state.users.set_goal("admin", {"objective": "o", "criterion": "c",
                                       "status": "active", "turn": 1})
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "a real question"})
        assert r.status_code == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
    assert app.state.users.get_goal("admin")["status"] == "paused"


@pytest.mark.asyncio
async def test_goal_token_session_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_WEB_TOKEN", "tok")   # before create_app reads it
    app = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/chat", json={"message": "/goal x"},
                         headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(
            f"/api/stream/{rid}", headers={"Authorization": "Bearer tok"}),
            timeout=10)
        assert "user-bound" in r.text
