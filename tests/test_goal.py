"""Goal feature (/goal): store roundtrip, command grammar, the supervisor loop
(web/goals.py) with a stubbed run-launcher, the goal.* tool seam, the loop's
declaration-sink wiring, and the web surface (slash replies, auto-pause,
/api/me). No network, no LiteLLM — the web surface drives the app in-process
via the shared conftest web_app/web_client/record_run fixtures."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from web import goals as goals_mod
from web.auth import UserStore
from web.store import ChatStore


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


def test_parse_loop_marks_fresh():
    p = goals_mod.parse("/loop refactor the module | done when: tests green")
    assert p["action"] == "start" and p["fresh"] is True
    assert p["objective"] == "refactor the module"
    assert p["criterion"] == "tests green"
    p = goals_mod.parse("/goal build X")
    assert p["action"] == "start" and p["fresh"] is False
    assert goals_mod.parse("/loop")["action"] == "status"
    assert goals_mod.parse("/loop stop")["action"] == "stop"


def test_parse_check_command():
    p = goals_mod.parse("/loop build X | done when: tests pass | check: pytest -q")
    assert p["check"] == "pytest -q"
    assert p["objective"] == "build X" and p["criterion"] == "tests pass"
    # order-independent, and /goal can carry one too
    p = goals_mod.parse("/loop build X | check: make test | done when: green")
    assert p["check"] == "make test" and p["criterion"] == "green"
    p = goals_mod.parse("/goal build X | done when: y | check: ./verify.sh")
    assert p["check"] == "./verify.sh" and p["fresh"] is False
    assert goals_mod.parse("/goal build X")["check"] == ""


def test_loop_directive_and_continuation():
    g = _goal_record(fresh=True, state="plan: 1) read 2) write")
    d = goals_mod.directive(g, 1, 10)
    assert "Loop mode" in d and "FRESH-CONTEXT" in d
    assert "STATE.md" in d and "no memory" in d
    c = goals_mod._continuation(g, 2, 10)
    assert "fresh context" in c and "plan: 1) read 2) write" in c
    assert "STATE.md" in c
    # without captured state the continuation says so instead of fabricating
    g2 = _goal_record(fresh=True)
    assert "no STATE.md yet" in goals_mod._continuation(g2, 2, 10)
    # non-fresh goals keep the classic continuation
    g3 = _goal_record()
    assert "Goal turn 2/10" in goals_mod._continuation(g3, 2, 10)


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
async def test_supervise_check_gate_replaces_judge(tmp_path, monkeypatch):
    """A `| check:` command decides completion deterministically: failing
    check → another iteration (failure logged + fed forward), passing check
    → done WITHOUT any judge call."""
    ws = tmp_path / "ws"
    ws.mkdir()
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok("claimed done"), {"status": "complete", "text": "e"}),
                         (_ok("really done"), {"status": "complete", "text": "e"})],
                        cfg={"max_turns": 5, "judge": True},
                        verdicts=["YES", "YES"])
    deps.state_root = lambda owner, goal: ws
    deps.users.set_goal("admin", _goal_record(
        fresh=True, check="test -f done.txt"))
    judged = []
    orig_judge = goals_mod._judge
    async def spy(*a, **kw):
        judged.append(1)
        return await orig_judge(*a, **kw)
    monkeypatch.setattr(goals_mod, "_judge", spy)
    # iteration 1's claim is premature — the artifact doesn't exist yet;
    # iteration 2 creates it before declaring complete
    orig_launch = deps.launch
    async def launch_then_act(**kw):
        rid, task = await orig_launch(**kw)
        if len(calls) == 2:
            (ws / "done.txt").write_text("x")
        return rid, task
    deps.launch = launch_then_act

    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "done" and len(calls) == 2
    assert judged == []                          # deterministic gate, no judge
    assert any(e["status"] == "check" and "failed" in e["note"]
               for e in g["log"])
    # the failed check rode into iteration 2's fresh-context message
    assert "completion check FAILED" in calls[1]["message"]
    # the directive told the agent the exact deterministic bar
    assert "test -f done.txt" in calls[0]["extra_system"]


@pytest.mark.asyncio
async def test_supervise_loop_fresh_context_and_state_spine(tmp_path, monkeypatch):
    """The Ralph trait: every /loop iteration launches with EMPTY history;
    STATE.md written by iteration 1 is captured and injected into iteration
    2's continuation — the workspace, not the context window, carries memory."""
    ws = tmp_path / "ws"
    ws.mkdir()
    deps, calls = _deps(tmp_path, monkeypatch,
                        [(_ok("did unit 1"), None),
                         (_ok("all done"), {"status": "complete",
                                            "text": "evidence"})],
                        cfg={"max_turns": 5, "judge": True}, verdicts=["YES"])
    deps.state_root = lambda owner, goal: ws
    deps.users.set_goal("admin", _goal_record(fresh=True))

    # iteration 1 "leaves" its state spine behind when it finishes
    orig_launch = deps.launch
    async def launch_then_write_state(**kw):
        rid, task = await orig_launch(**kw)
        if len(calls) == 1:
            (ws / "STATE.md").write_text("plan: A B C — A done, next: B")
        return rid, task
    deps.launch = launch_then_write_state

    await goals_mod.supervise(deps, "admin")
    g = deps.users.get_goal("admin")
    assert g["status"] == "done" and len(calls) == 2
    # THE fresh-context guarantee: no history on ANY iteration
    assert calls[0]["history"] == [] and calls[1]["history"] == []
    assert "Loop mode" in calls[0]["extra_system"]
    # the captured STATE.md rode into iteration 2's message
    assert "plan: A B C — A done, next: B" in calls[1]["message"]
    assert g["state"] == "plan: A B C — A done, next: B"
    row = deps.chats.get_current("admin")
    msgs = [t["user_message"] for t in row["chat"]["turns"]]
    assert msgs[0].startswith("🔄 loop turn 1")


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
from runtime.loop import AgentRuntime  # noqa: E402
from runtime.selector import ToolSelector  # noqa: E402

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


# ---- web surface (conftest web_app/web_client/record_run fixtures) --------------
@pytest.mark.asyncio
async def test_goal_slash_start_runs_to_done(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

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

    async with web_client(app) as c:
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
async def test_goal_status_and_stop_slash(web_app, web_client):
    app = web_app()
    app.state.users.set_goal("admin", {"objective": "o", "criterion": "c",
                                       "status": "paused", "turn": 2,
                                       "tokens_total": 5})
    async with web_client(app) as c:
        rid = (await c.post("/api/chat", json={"message": "/goal"})).json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "2/" in r.text and "paused" in r.text
        rid = (await c.post("/api/chat",
                            json={"message": "/goal stop"})).json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        assert "stopped" in r.text
        assert app.state.users.get_goal("admin") == {}


@pytest.mark.asyncio
async def test_goal_slash_start_with_project(web_app, web_client, monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()

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

    async with web_client(app) as c:
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
async def test_goal_auto_pause_on_user_message(web_app, web_client, record_run,
                                               monkeypatch):
    monkeypatch.setattr("runtime.quick_reply.QuickReply.match",
                        lambda self, msg, username="": None)
    app = web_app()
    seen = record_run(app)
    app.state.users.set_goal("admin", {"objective": "o", "criterion": "c",
                                       "status": "active", "turn": 1})
    async with web_client(app) as c:
        r = await c.post("/api/chat", json={"message": "a real question"})
        assert r.status_code == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
    assert app.state.users.get_goal("admin")["status"] == "paused"


@pytest.mark.asyncio
async def test_goal_token_session_rejected(web_app):
    app = web_app(env={"ORCH_WEB_TOKEN": "tok"})   # before create_app reads it
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


def test_config_check_timeout_never_unbounded():
    """0/unset floors to the default — no unbounded check subprocess
    (audit D1)."""
    cfg = goals_mod.config(_JudgeRuntime({}, []))
    assert cfg["check_timeout_s"] == 120.0
    cfg = goals_mod.config(_JudgeRuntime({"check_timeout_s": 0}, []))
    assert cfg["check_timeout_s"] == 120.0
    cfg = goals_mod.config(_JudgeRuntime({"check_timeout_s": 30}, []))
    assert cfg["check_timeout_s"] == 30.0
