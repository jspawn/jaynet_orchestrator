"""j-space badge+plan gate (loop_guard.jspace_badge_gate, default on): the
j-space skill's protocol order is classify → badge → plan → work. While
j-space is loaded and either opener is missing (run.badge not called, or
no todos plan set), fs.write/fs.edit outside .jspace/ AND
specialist.delegate/agent.spawn calls are REJECTED at dispatch (not
executed) with only the missing opener(s) named. The badge step was
chronically skipped (j-space-loop eval 3/3); once the badge was enforced
the plan step failed the same way — in a dispatch-mode run the brain
badged, hit the dispatch gate, and delegated the rename with no todos
plan, moving the first edit into an invisible child. Both openers in
place latches the gate open for the rest of the run. Loop-level tests
use the fake-model harness (convention: the scaffolding helpers are
copied from test_loop_regressions, not shared)."""
import asyncio

from runtime.loop import AgentRuntime, _jspace_ledger_target
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult
from tools.agent.todos import TodosTool

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 14, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}

GATE_MARK = "BLOCKED (j-space badge gate)"


class _RecTool:
    """Stub tool that records every EXECUTED call — a dispatch-gate
    rejection must never reach execute()."""
    private = False

    def __init__(self, name, log):
        self.name = name
        self._log = log

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        self._log.append(self.name)
        return ToolResult(status="ok", result={"ok": True})


class _Registry:
    """Recording stubs for every tool but `todos` — the REAL TodosTool, so
    a `set` call lands in rs.todo_list through the loop's own ctx wiring
    (the gate reads the harness list, not the tool's self-report)."""
    def __init__(self, log):
        self._tools = {n: _RecTool(n, log) for n in
                       ("skill.load", "run.badge", "fs.write", "fs.edit",
                        "specialist.delegate", "agent.spawn")}
        self._tools["todos"] = TodosTool()

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
    """One assistant message carrying a single tool call."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def _final(text="done"):
    return {"role": "assistant", "content": text}


_LOAD_JSPACE = _tc("skill.load", '{"name": "j-space"}')
_EDIT_PROJECT = _tc("fs.edit", '{"path": "settings.py", "old": "a", "new": "b"}')
_BADGE = _tc("run.badge", '{"label": "j-space: loop"}')
_PLAN = _tc("todos", '{"action": "set", "items": [{"title": "rename"},'
                     ' {"title": "test"}]}')
_DELEGATE = _tc("specialist.delegate", '{"task": "rename TIMEOUT"}')
_SPAWN = _tc("agent.spawn", '{"task": "rename TIMEOUT"}')


def _runtime(registry, script):
    """A drivable AgentRuntime: real loop, fake model. `script` is the list of
    assistant messages the fake _model_turn returns in order. Returns
    (runtime, seen) — seen collects the message lists each model turn got."""
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


def _gate_messages(call):
    """The gate rejections visible in one model turn's message list."""
    return [m["content"] for m in call
            if isinstance(m.get("content"), str) and GATE_MARK in m["content"]]


# ---- path classification (unit level) ----

def test_ledger_target_paths():
    assert _jspace_ledger_target('{"path": ".jspace/WORKSPACE.md"}')
    assert _jspace_ledger_target('{"path": "/srv/proj/.jspace/WORKSPACE.md"}')
    assert _jspace_ledger_target({"path": "sub/dir/.jspace/notes.md"})
    assert not _jspace_ledger_target('{"path": "settings.py"}')
    assert not _jspace_ledger_target('{"path": ".jspacing/x.md"}')
    assert not _jspace_ledger_target('not json')
    assert not _jspace_ledger_target({"no_path": 1})


# ---- loop-level behavior ----

def test_full_protocol_ladder_each_rejection_names_only_what_is_missing(tmp_path):
    """The eval failure mode, whole ladder: load → edit (both openers
    missing) → badge → edit (plan still missing) → plan → edit succeeds.
    Neither rejected edit executes; each rejection names ONLY the missing
    opener(s)."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _BADGE,
                         _EDIT_PROJECT, _PLAN, _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    # fs.edit executed exactly once — after BOTH openers, never before.
    assert log == ["skill.load", "run.badge", "fs.edit"]
    # First rejection (neither opener): names both.
    both = _gate_messages(seen[2])
    assert len(both) == 1
    assert "`run.badge`" in both[0] and "todos" in both[0]
    assert "fast / full / loop" in both[0] and "NOT executed" in both[0]
    # Second rejection (badged, no plan): names ONLY the plan — no run.badge
    # instruction (the badge is already in place). The transcript
    # accumulates, so the turn's LATEST gate message is the new one.
    plan_only = _gate_messages(seen[4])[-1]
    assert "set a plan with the todos tool" in plan_only
    assert "`run.badge`" not in plan_only


def test_todos_before_badge_edit_rejected_naming_only_badge(tmp_path):
    """Plan first, badge second: the rejection names ONLY the badge — no
    todos instruction (the plan is already in place)."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _PLAN, _EDIT_PROJECT, _BADGE,
                         _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "fs.edit"]
    badge_only = _gate_messages(seen[3])[-1]
    assert "`run.badge`" in badge_only
    assert "todos" not in badge_only


def test_unplanned_delegate_rejected_then_flows(tmp_path):
    """The live bypass: badged but no plan, the brain hands the
    implementation to specialist.delegate — in a j-space run delegation IS
    the implementation lane, so the gate rejects it with the plan message;
    after the plan lands the same delegate call flows."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _BADGE, _DELEGATE, _PLAN,
                         # extra final: the verify-after-delegate bounce
                         # costs one turn once a delegation executed
                         _DELEGATE, _final("done"), _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "specialist.delegate"]
    plan_only = _gate_messages(seen[3])[-1]
    assert "set a plan with the todos tool" in plan_only
    assert "`run.badge`" not in plan_only


def test_agent_spawn_blocked_under_same_condition(tmp_path):
    """agent.spawn is a real implementation lane in this harness (the fresh
    -retry/dispatch bookkeeping treats it alongside the delegate verbs), so
    the gate covers it under the same condition and message."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _SPAWN, _BADGE, _PLAN, _SPAWN,
                         _final("done"), _final("done")])   # see above
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "agent.spawn"]
    both = _gate_messages(seen[2])[-1]
    assert "`run.badge`" in both and "todos" in both


def test_ledger_file_writable_without_badge(tmp_path):
    """The legit flow the gate must not break: the skill maintains
    .jspace/WORKSPACE.md with the fs.* tools — ledger writes pass while
    project edits stay gated."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE,
                         _tc("fs.write",
                             '{"path": ".jspace/WORKSPACE.md", "content": "..."}'),
                         _final("done")])
    out = asyncio.run(rt.run("plan the work", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "fs.write"]


def test_protocol_order_badge_plan_no_rejection(tmp_path):
    """Protocol-compliant run: badge AND plan before any edit or
    delegation — the gate never fires."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _BADGE, _PLAN, _EDIT_PROJECT,
                         _DELEGATE, _final("done"), _final("done")])   # see above
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "fs.edit",
                   "specialist.delegate"]
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


def test_gate_off_edits_and_delegates_allowed(tmp_path):
    """loop_guard.jspace_badge_gate: false = zero behavior change (the
    one-shot badge-watch nudge stays the only reminder)."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _DELEGATE,
                         _final("done"), _final("done")])   # see above
    rt.config["loop_guard"] = {"jspace_badge_gate": False}
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "fs.edit", "specialist.delegate"]
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


def test_non_jspace_runs_unaffected(tmp_path):
    """No j-space load (or a different, non-badge skill): the gate never
    engages — only j-space carries the protocol this enforces."""
    log = []
    rt, seen = _runtime(_Registry(log), [_EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok" and log == ["fs.edit"]

    log2 = []
    rt2, seen2 = _runtime(_Registry(log2),
                          [_tc("skill.load", '{"name": "coding"}'),
                           _EDIT_PROJECT, _final("done")])
    out2 = asyncio.run(rt2.run("rename the setting", work_root=str(tmp_path)))
    assert out2["status"] == "ok" and log2 == ["skill.load", "fs.edit"]
    flat2 = [m.get("content") or "" for call in seen2 for m in call]
    assert all(GATE_MARK not in c for c in flat2)
