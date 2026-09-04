"""Deliverable check (AgentRuntime._missing_deliverables).

Named-but-missing is the signal: input files a task references already exist,
so an absolute path named in the task or the final answer that does NOT exist
in the workspace is almost always an unwritten deliverable — the dominant
small-brain eval failure (solved the task, never called fs.write).
"""
from runtime.loop import AgentRuntime
from runtime.tool_base import ToolContext


def _ctx(tmp_path):
    return ToolContext(request_id="t", config={}, budget=None,
                       work_root=str(tmp_path))


def test_missing_named_file_is_reported(tmp_path):
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "Write the answer to /app/answer.txt and finish.")
    assert missing == ["/app/answer.txt"]


def test_existing_input_file_is_not_reported(tmp_path):
    (tmp_path / "rules.json").write_text("{}")
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "Read /app/rules.json, then write /app/out.txt.")
    assert missing == ["/app/out.txt"]


def test_paths_outside_workspace_are_skipped(tmp_path):
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "compare with /etc/passwd and /definitely/not/here.txt")
    # /etc/passwd: real host path outside the workspace — not ours to check.
    # /definitely/...: fictional root, rebased into the workspace → missing.
    assert missing == ["/definitely/not/here.txt"]


def test_urls_and_templates_are_not_matched(tmp_path):
    text = ("see http://example.com/spec.txt and https://x.io/a/b.json; "
            "name it incident_<IP>_<timestamp>.txt like the schema says")
    assert AgentRuntime._missing_deliverables(_ctx(tmp_path), text) == []


def test_duplicates_collapsed_and_capped(tmp_path):
    text = " ".join(f"/app/f{i}.txt" for i in range(10)) + " /app/f0.txt"
    missing = AgentRuntime._missing_deliverables(_ctx(tmp_path), text)
    assert len(missing) == 5
    assert len(set(missing)) == 5


def test_no_workspace_returns_nothing():
    ctx = ToolContext(request_id="t", config={"tools": {"fs": {} }},
                      budget=None, work_root=None)
    assert AgentRuntime._missing_deliverables(ctx, "/app/x.txt") == []


# ---- mid-run early warning (agent.deliverable_check.warn_at) ----
# The final-answer check only fires when the model STOPS; a run that burns
# its last iterations still computing never gets to react (live:
# tb-count-dataset-tokens). The warning fires once at ~3/4 of the iteration
# budget while >= 2 turns remain. Driven with the real loop over a fake model.
import asyncio
import json

from tests.test_loop_regressions import _final, _Registry, _runtime, _tc  # noqa: E402

TASK = "Check the inputs, compute the answer, write it to /app/answer.txt."


def _read_reg(tmp_path, names):
    from tools.fs.ops import FsRead
    for n in names:
        (tmp_path / n).write_text(n)
    return _Registry([], real={"fs.read": FsRead()})


def _reminders(seen):
    out = set()
    for msgs in seen:
        for m in msgs:
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                if m["content"].startswith("Deliverable reminder"):
                    out.add(m["content"])
    return out


def _script(names):
    return [_tc("fs.read", json.dumps({"path": n})) for n in names]


def test_warns_midrun_when_named_file_missing(tmp_path):
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    reg = _read_reg(tmp_path, names)
    # 5 reads put pass 6 (of max 8, warn_at .75 -> iteration 6) in front of
    # the first _final; the final-answer check then demands the second.
    script = _script(names) + [_final("done"), _final("done")]
    rt, seen = _runtime(reg, script)
    out = asyncio.run(rt.run(TASK, work_root=str(tmp_path)))
    assert out["status"] == "ok"
    rem = _reminders(seen)
    assert len(rem) == 1
    assert "/app/answer.txt" in rem.pop()


def test_no_warning_when_the_file_exists(tmp_path):
    (tmp_path / "answer.txt").write_text("42")
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    reg = _read_reg(tmp_path, names)
    script = _script(names) + [_final("done")]
    rt, seen = _runtime(reg, script)
    out = asyncio.run(rt.run(TASK, work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert _reminders(seen) == set()


def test_warn_at_zero_disables_the_warning(tmp_path):
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    reg = _read_reg(tmp_path, names)
    script = _script(names) + [_final("done"), _final("done")]
    rt, seen = _runtime(reg, script)
    rt.config["agent"] = {"deliverable_check": {"enabled": True, "warn_at": 0}}
    out = asyncio.run(rt.run(TASK, work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert _reminders(seen) == set()
