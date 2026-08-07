"""Harness todo list: the TodoList state manager, the todos tool, loop wiring
(todos SSE event + per-turn re-injection when the working anchor is off), the
child-run snapshot forward, and the architect's UNITS parser."""
import asyncio

from runtime.loop import AgentRuntime, _child_progress_fwd
from runtime.todos import MAX_ITEMS, TodoList
from runtime.tool_base import ToolContext
from tools.agent.architect import _parse_units
from tools.agent.todos import TodosTool

from test_loop_regressions import _Registry, _Trace, _final, _runtime, _tc


# ---- TodoList state manager ----

def test_set_assigns_ids_and_pending():
    tl = TodoList()
    res = tl.apply({"action": "set",
                    "items": [{"title": "a", "desc": "x"}, {"title": "b"}]})
    assert res["status"] == "ok"
    assert [(i["id"], i["title"], i["status"]) for i in res["items"]] == \
        [(1, "a", "pending"), (2, "b", "pending")]
    assert res["items"][0]["desc"] == "x"


def test_set_accepts_plain_strings_and_rejects_empty():
    tl = TodoList()
    assert tl.apply({"action": "set", "items": ["do a", "do b"]})["status"] == "ok"
    assert tl.apply({"action": "set", "items": []})["status"] == "error"
    assert tl.apply({"action": "set"})["status"] == "error"


def test_working_is_exclusive():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a"}, {"title": "b"}]})
    tl.apply({"action": "update", "id": 1, "status": "working"})
    res = tl.apply({"action": "update", "id": 2, "status": "working"})
    by_id = {i["id"]: i["status"] for i in res["items"]}
    assert by_id == {1: "pending", 2: "working"}


def test_update_appends_notes_and_edits_desc():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a"}]})
    tl.apply({"action": "update", "id": 1, "note": "started"})
    res = tl.apply({"action": "update", "id": 1, "status": "skipped",
                    "note": "not needed", "desc": "why"})
    it = res["items"][0]
    assert it["status"] == "skipped" and it["info"] == ["started", "not needed"]
    assert it["desc"] == "why"


def test_update_rejects_noop_and_bad_references():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a"}]})
    assert tl.apply({"action": "update", "id": 1})["status"] == "error"
    assert tl.apply({"action": "update", "id": 99, "status": "done"})["status"] == "error"
    assert tl.apply({"action": "update", "id": 1, "status": "nope"})["status"] == "error"
    assert tl.apply({"action": "bogus"})["status"] == "error"


def test_add_remove_clear_and_caps():
    tl = TodoList()
    tl.apply({"action": "set",
              "items": [{"title": f"t{i}"} for i in range(MAX_ITEMS + 5)]})
    assert len(tl.items) == MAX_ITEMS
    assert tl.apply({"action": "add", "title": "one more"})["status"] == "error"
    res = tl.apply({"action": "remove", "id": 3})
    assert res["status"] == "ok"
    assert [i["id"] for i in res["items"]] == list(range(1, MAX_ITEMS))  # re-numbered
    assert tl.apply({"action": "add", "title": "one more"})["status"] == "ok"
    res = tl.apply({"action": "clear"})
    assert res["status"] == "ok" and res["items"] == []
    assert tl.render() == ""


def test_render_compact_with_progress():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a"}, {"title": "b", "desc": "d"}]})
    tl.apply({"action": "update", "id": 1, "status": "done"})
    tl.apply({"action": "update", "id": 2, "status": "working"})
    out = tl.render()
    assert "1/2 done" in out and "1 [done] a" in out
    assert "2 [working] b — d" in out     # the working item's desc rides along


# ---- todos tool ----

def test_tool_errors_without_seam():
    ctx = ToolContext(request_id="r", config={}, budget=None)
    res = asyncio.run(TodosTool().execute({"action": "clear"}, ctx))
    assert res.status == "error"


def test_tool_forwards_and_propagates_errors():
    ctx = ToolContext(request_id="r", config={}, budget=None)
    tl = TodoList()

    async def seam(payload):
        return tl.apply(payload)
    ctx.todos_update = seam
    res = asyncio.run(TodosTool().execute(
        {"action": "set", "items": [{"title": "a"}]}, ctx))
    assert res.status == "ok" and res.result["items"][0]["title"] == "a"
    res = asyncio.run(TodosTool().execute({"action": "nope"}, ctx))
    assert res.status == "error" and "unknown action" in (res.error or "")


# ---- loop wiring ----

def _todos_rt(script):
    rt, seen = _runtime(_Registry(["todos"], real={"todos": TodosTool()}), script)
    return rt, seen


def test_loop_emits_todos_snapshot_events():
    rt, _ = _todos_rt([
        _tc("todos", '{"action":"set","items":[{"title":"a"},{"title":"b"}]}'),
        _tc("todos", '{"action":"update","id":1,"status":"working"}'),
        _final("done")])
    events = []
    rt.trace = _Trace()

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("do a thing", tools=["todos"], on_event=on_event))
    assert out["status"] == "ok"
    snaps = [e["data"]["items"] for e in events if e["type"] == "todos"]
    assert len(snaps) == 2
    assert [i["title"] for i in snaps[0]] == ["a", "b"]
    assert snaps[1][0]["status"] == "working"


def test_loop_reinjects_todos_as_trailing_system_when_anchor_off():
    # Default anchor.mode is off: the live todo list still rides every model
    # turn as a trailing system message so compaction can't take it away.
    rt, seen = _todos_rt([
        _tc("todos", '{"action":"set","items":[{"title":"write tests"}]}'),
        _final("done")])
    asyncio.run(rt.run("do a thing", tools=["todos"]))
    assert len(seen) == 2
    tail = seen[1][-1]
    assert tail["role"] == "system" and "TODO LIST" in tail["content"]
    assert "1 [pending] write tests" in tail["content"]
    # The anchor is per-call only — never persisted into the transcript.
    assert seen[0] is not seen[1]


def test_build_anchor_includes_todos_section():
    m = AgentRuntime._build_anchor("Goal X", "", "TODO LIST (0/1 done):\n1 [pending] a")
    assert "TODO LIST" in m["content"]
    m2 = AgentRuntime._build_todos_anchor("TODO LIST (0/1 done):\n1 [pending] a")
    assert m2["role"] == "system" and "TODO LIST" in m2["content"]
    assert AgentRuntime._build_todos_anchor("") is None


# ---- child-run forwarding (the architect's executor drives the same panel) ----

def test_child_todos_events_forward_and_sync_parent():
    sent = []
    synced = []

    async def emit(t, d):
        sent.append((t, d))
    fwd = _child_progress_fwd(emit, on_todos=synced.append)
    asyncio.run(fwd({"type": "todos",
                     "data": {"items": [{"id": 1, "title": "a",
                                         "status": "working"}]}}))
    assert sent == [("todos", {"items": [{"id": 1, "title": "a",
                                          "status": "working"}]})]
    assert synced == [[{"id": 1, "title": "a", "status": "working"}]]
    # Other event types are untouched by the todos path.
    asyncio.run(fwd({"type": "model_start", "data": {}}))
    assert sent[-1][0] == "progress"


# ---- architect UNITS → todo list ----

def test_parse_units_bullets_and_numbers():
    plan = ("GOAL: x\nUNITS:\n- read the config\n1. patch the loop\n"
            "* add tests\nRISKS:\n- none\n")
    assert _parse_units(plan) == ["read the config", "patch the loop", "add tests"]


def test_parse_units_caps_and_ignores_noise():
    plan = "UNITS:\n" + "\n".join(f"- step {i}" for i in range(20)) + \
           "\nnot a bullet\nGOAL: later"
    units = _parse_units(plan)
    assert len(units) == 12 and units[0] == "step 0"
    assert _parse_units("no units here") == []
