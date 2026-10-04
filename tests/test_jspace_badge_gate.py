"""j-space badge gate (loop_guard.jspace_badge_gate, default on): the
j-space skill's protocol badges the pass (run.badge with "j-space: full" /
"j-space: loop") right after classifying, BEFORE any file work — and the
badge step was chronically skipped even when everything else went right
(j-space-loop eval failed 3/3 on exactly that one deterministic check).
Prompt nudges don't move small brains, so the harness gates it: while
j-space is loaded and unbadged, fs.write/fs.edit on anything but the
.jspace/ ledger itself is REJECTED at dispatch (not executed) with the
protocol step named. One successful run.badge opens the gate for the rest
of the run. Loop-level tests use the fake-model harness (convention: the
scaffolding helpers are copied from test_loop_regressions, not shared)."""
import asyncio

from runtime.loop import AgentRuntime, _jspace_ledger_target
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 12, "max_wall_clock_s": 60.0,
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
    def __init__(self, log, names=("skill.load", "run.badge",
                                   "fs.write", "fs.edit")):
        self._tools = {n: _RecTool(n, log) for n in names}

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

def test_unbadged_edit_rejected_not_executed_then_recovers(tmp_path):
    """The eval failure mode: skill.load(j-space) lands, the agent goes
    straight for the project edit — the gate rejects it at dispatch with
    the protocol step named, and the rejection feeds back as a tool error
    the model reacts to: badge, then the same edit succeeds."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _BADGE,
                         _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    # fs.edit executed exactly once — AFTER the badge, never before.
    assert log == ["skill.load", "run.badge", "fs.edit"]
    # The rejection reached the model as a normal tool result naming the
    # exact recovery step (classify → run.badge → re-issue).
    flat = [m.get("content") or "" for m in seen[2]]
    assert any(GATE_MARK in c for c in flat)
    rejected = next(c for c in flat if GATE_MARK in c)
    assert "run.badge" in rejected and "NOT executed" in rejected
    assert "fast / full / loop" in rejected


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


def test_badge_first_no_rejection(tmp_path):
    """Protocol-compliant run: badge before any edit — the gate never fires."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _BADGE, _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "fs.edit"]
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


def test_gate_off_edits_allowed_without_badge(tmp_path):
    """loop_guard.jspace_badge_gate: false = zero behavior change (the
    one-shot badge-watch nudge stays the only reminder)."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _final("done")])
    rt.config["loop_guard"] = {"jspace_badge_gate": False}
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "fs.edit"]
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
