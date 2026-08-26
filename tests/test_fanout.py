"""agent.fanout: parallel map/merge over ctx.spawn — task validation,
per-child isolation, partial-failure semantics, model/strength routing."""
import asyncio

from runtime.tool_base import ToolContext
from tools.agent.fanout import _MAX_TASKS, AgentFanout

CFG = {"orchestrator": {"model": "local-orchestrator",
                        "litellm_base": "http://x:4000"}}


def _ctx(spawn):
    return ToolContext(request_id="t", config=dict(CFG), budget=None,
                       spawn=spawn)


def _run(args, spawn):
    return asyncio.run(AgentFanout().execute(args, _ctx(spawn)))


def test_fanout_runs_children_concurrently_and_merges():
    calls = []

    async def fake_spawn(task, **kw):
        calls.append({"task": task, **kw})
        return {"status": "ok", "answer": f"did: {task}",
                "files_changed": [], "run_id": f"r-{len(calls)}"}

    r = _run({"tasks": ["sum file a", "sum file b", "sum file c"]},
             fake_spawn)
    assert r.status == "ok"
    assert r.result["succeeded"] == 3 and r.result["failed"] == 0
    assert [c["answer"] for c in r.result["children"]] == [
        "did: sum file a", "did: sum file b", "did: sum file c"]
    # children are named for traces/UI, tools/model/budget pass through
    assert [c["name"] for c in calls] == ["fanout-1", "fanout-2", "fanout-3"]


def test_fanout_partial_failure_is_signal_not_tool_error():
    async def fake_spawn(task, **kw):
        if "bad" in task:
            return {"status": "error", "error": "child blew up",
                    "answer": None}
        return {"status": "ok", "answer": "fine"}

    r = _run({"tasks": ["good one", "bad one", "good two"]}, fake_spawn)
    assert r.status == "ok"                      # merge what succeeded
    assert r.result["succeeded"] == 2 and r.result["failed"] == 1
    bad = [c for c in r.result["children"] if c.get("error")][0]
    assert "child blew up" in bad["error"]


def test_fanout_all_failed_is_tool_error():
    async def fake_spawn(task, **kw):
        return {"status": "error", "error": "nope"}

    r = _run({"tasks": ["a", "b"]}, fake_spawn)
    assert r.status == "error" and "every child failed" in r.error
    assert len(r.result["children"]) == 2        # details survive


def test_fanout_spawn_exception_becomes_child_error():
    async def fake_spawn(task, **kw):
        raise RuntimeError("depth cap")

    r = _run({"tasks": ["a", "b"]}, fake_spawn)
    assert r.status == "error"
    assert all("depth cap" in c["error"] for c in r.result["children"])


def test_fanout_validates_tasks():
    async def fake_spawn(task, **kw):  # pragma: no cover - never reached
        return {"status": "ok"}

    assert _run({"tasks": []}, fake_spawn).status == "error"
    assert _run({"tasks": ["", "  "]}, fake_spawn).status == "error"
    assert _run({"tasks": "not-a-list"}, fake_spawn).status == "error"
    too_many = [f"t{i}" for i in range(_MAX_TASKS + 1)]
    r = _run({"tasks": too_many}, fake_spawn)
    assert r.status == "error" and "too many" in r.error


def test_fanout_unknown_model_hard_errors_before_spawning(tmp_path):
    spawned = []

    async def fake_spawn(task, **kw):
        spawned.append(task)
        return {"status": "ok"}

    ctx = ToolContext(request_id="t",
                      config={**CFG, "tools": {"serve": {
                          "state_dir": str(tmp_path)}}},
                      budget=None, spawn=fake_spawn)
    r = asyncio.run(AgentFanout().execute(
        {"tasks": ["a"], "model": "not-a-model"}, ctx))
    assert r.status == "error" and "unknown model" in r.error
    assert not spawned


def test_fanout_no_spawn_runtime():
    r = asyncio.run(AgentFanout().execute(
        {"tasks": ["a"]},
        ToolContext(request_id="t", config=dict(CFG), budget=None,
                    spawn=None)))
    assert r.status == "error" and "not available" in r.error
