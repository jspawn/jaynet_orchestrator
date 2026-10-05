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
                        "code.run", "code.patch",
                        "specialist.delegate", "agent.spawn", "agent.fanout")}
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
_FANOUT = _tc("agent.fanout", '{"tasks": ["rename TIMEOUT"]}')
_SHELL_HEREDOC = _tc("code.run",
                     '{"command": "cat > settings.py <<EOF\\nx = 1\\nEOF"}')
_SHELL_SED = _tc("code.run", '{"command": "sed -i s/a/b/ settings.py"}')
_PATCH = _tc("code.patch", '{"path": "settings.py", "patch": "@@ -1 +1 @@"}')
_PATCH_LEDGER = _tc("code.patch",
                    '{"path": ".jspace/WORKSPACE.md", "patch": "@@ -1 +1 @@"}')
_READONLY_SHELL = _tc("code.run", '{"command": "ls -la"}')


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


def test_shell_writes_patch_and_fanout_blocked(tmp_path):
    """audit #28 C1: the write test is _gate_write_like, not the fs.write/
    fs.edit pair — heredoc and sed -i shell writes, code.patch and
    agent.fanout all walked straight through the old list. Each is now
    rejected at dispatch (never executed) with the instructive reason."""
    for call in (_SHELL_HEREDOC, _SHELL_SED, _PATCH, _FANOUT):
        log = []
        rt, seen = _runtime(_Registry(log),
                            [_LOAD_JSPACE, call, _final("done")])
        out = asyncio.run(rt.run("rename the setting",
                                 work_root=str(tmp_path)))
        assert out["status"] == "ok"
        assert log == ["skill.load"], call     # rejected, never executed
        assert _gate_messages(seen[2]), call


def test_shell_write_and_patch_flow_after_unlock(tmp_path):
    """The wider net opens with the same latch: badge + plan, then the
    heredoc write and the patch execute."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _BADGE, _PLAN, _SHELL_HEREDOC,
                         _PATCH, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "run.badge", "code.run", "code.patch"]
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


def test_readonly_shell_passes_unbadged(tmp_path):
    """_gate_write_like fires only on write-ish shell commands — plain
    reads through code.run are not file work and stay open."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _READONLY_SHELL, _final("done")])
    out = asyncio.run(rt.run("look around", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "code.run"]


def test_ledger_patch_writable_without_badge(tmp_path):
    """The .jspace/ exemption survives the wider predicate: a code.patch
    into the ledger is ledger maintenance, not project work."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _PATCH_LEDGER, _final("done")])
    out = asyncio.run(rt.run("plan the work", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert log == ["skill.load", "code.patch"]


def test_gate_state_survives_badge_watch_ablation(tmp_path):
    """audit #28 C2: guards_off: ["badge_watch"] removes the NUDGE only —
    the gate's arming (badge_watch) and unlock (badged) state is written
    by the loop's own post-call path, so the gate still fires."""
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path),
                             guards_off=["badge_watch"]))
    assert out["status"] == "ok"
    assert log == ["skill.load"]
    assert _gate_messages(seen[2])


def test_gate_ablatable_by_its_own_name(tmp_path):
    """guards_off: ["jspace_badge_gate"] is a legal ablation (a registered
    dispatch-gate name — no ValueError at variant validation) and switches
    the gate off for the run."""
    from runtime import eval_runner
    assert eval_runner.check_guards_off(["jspace_badge_gate"]) == []
    log = []
    rt, seen = _runtime(_Registry(log),
                        [_LOAD_JSPACE, _EDIT_PROJECT, _final("done")])
    out = asyncio.run(rt.run("rename the setting", work_root=str(tmp_path),
                             guards_off=["jspace_badge_gate"]))
    assert out["status"] == "ok"
    assert log == ["skill.load", "fs.edit"]
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


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


# ---- auto-delegate salvage closes the ceremony harness-side ---------------
# (j-space-loop live validation 2026-10-05 rep 1: the loop guard's own
# salvage lane bypasses the model-call dispatch where the gate lives — a
# stalled badged-but-planless run got an UNPLANNED specialist
# implementation. Helpers copied from test_loop_regressions' auto-delegate
# scaffolding, never cross-imported.)
import tempfile


class _Stub:
    """Exec-logging stub; read_only stubs never bump the mutation
    generation, so every call is a no-progress turn — the frozen-brain
    input the stall hard-stop (and its auto-delegate salvage) closes."""
    private = False

    def __init__(self, name, log, read_only=True):
        self.name = name
        self.read_only = read_only
        self._log = log

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        self._log.append(self.name)
        return ToolResult(status="ok", tool_name=self.name,
                          result={"text": "specialist report: renamed"})


class _AutoRegistry:
    """Stall-shaped toolset: a no-progress reader, the gate-relevant
    verbs, and the REAL TodosTool (the salvage plan must land in
    rs.todo_list through the loop's own ctx wiring)."""
    def __init__(self, log):
        self._tools = {
            "skill.load": _Stub("skill.load", log, read_only=False),
            "run.badge": _Stub("run.badge", log, read_only=True),
            "x.read": _Stub("x.read", log, read_only=True),
            "fs.edit": _Stub("fs.edit", log, read_only=False),
            "code.check": _Stub("code.check", log, read_only=False),
            "specialist.delegate": _Stub("specialist.delegate", log,
                                         read_only=False),
            "todos": TodosTool(),
        }

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)

    def openai_schemas(self, allowed=None):
        return [t.to_openai_schema() for n, t in self._tools.items()
                if allowed is None or n in allowed]


def _auto_rt(log, script, monkeypatch):
    """Real loop, fake model, default stall ladder (after=2, 3 rungs → the
    final rung arms after 6 no-progress turns), auto_delegate_after=2 —
    copied from test_loop_regressions._stall_stop_rt."""
    import tools.model.catalog as cat

    async def fake_route(config, tag):
        return {"alias": "spec", "mode": "live", "preset": "x"}
    monkeypatch.setattr(cat, "strength_route", fake_route)
    rt, seen = _runtime(_AutoRegistry(log), script)
    rt.config["budgets"] = {**CFG["budgets"], "max_iterations": 40}
    rt.config["loop_guard"] = {"max_rejections": 6}
    events = []

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("spin without progress",
                             work_root=tempfile.mkdtemp(), on_event=on_event))
    return out, events, seen


def _spin_reads(n, start=0):
    """n no-progress reads with DISTINCT args (the duplicate guard counts
    exact repeats — vary them)."""
    import json as _json
    return [_tc("x.read", _json.dumps({"n": i}))
            for i in range(start, start + n)]


def _event_index(events, pred):
    return next((i for i, e in enumerate(events) if pred(e)), None)


def test_auto_delegate_closes_plan_and_latches_when_badged(tmp_path, monkeypatch):
    """Armed + badged + NO plan, refusal streak reaches auto_delegate_after:
    the salvage delegation would bypass the gate entirely — so the harness
    records a minimal, honestly-attributed salvage plan BEFORE delegating,
    and latches the gate (the brain's post-delegation verify/edit steps
    are not rejected afterwards)."""
    log = []
    script = ([_LOAD_JSPACE, _BADGE] + _spin_reads(5)     # 6 no-progress → armed
              + [_tc("x.read", '{"n": 6}'),               # refused #1
                 _tc("x.read", '{"n": 7}'),               # refused #2 → auto-delegate
                 _tc("code.check", '{"command": "pytest -q"}'),
                 _EDIT_PROJECT,                           # latched → executes
                 _final("done")])
    out, events, seen = _auto_rt(log, script, monkeypatch)
    assert out["status"] == "ok"
    assert log.count("specialist.delegate") == 1, "the harness delegated once"
    # The salvage plan landed BEFORE the delegation's tool_result…
    i_todos = _event_index(events, lambda e: e["type"] == "todos"
                           and (e["data"].get("items") or []))
    i_deleg = _event_index(events, lambda e: e["type"] == "tool_result"
                           and e["data"].get("tool") == "specialist.delegate")
    assert i_todos is not None and i_deleg is not None
    assert i_todos < i_deleg
    # …honestly attributed, in the ceremony progress event AND the report note.
    assert any(e["type"] == "progress"
               and "closed the j-space ceremony" in e["data"].get("label", "")
               for e in events)
    reports = [m["content"] for turns in seen for m in turns
               if "Blocked-call streak" in (m.get("content") or "")]
    assert reports and "loop guard, not the brain" in reports[-1]
    # The brain badged itself at turn 2, so the harness did NOT badge —
    # but the gate IS latched: verify + edit after the salvage execute.
    assert "code.check" in log and "fs.edit" in log
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(GATE_MARK not in c for c in flat)


def test_auto_delegate_badges_when_unbadged(tmp_path, monkeypatch):
    """Armed + NOT badged + no plan: the harness closes the badge itself
    ('j-space: full' — the salvage IS effectively a full pass) before
    delegating, then the salvage plan, then the delegation."""
    log = []
    script = ([_LOAD_JSPACE] + _spin_reads(6)             # 6 no-progress → armed
              + [_tc("x.read", '{"n": 6}'),               # refused #1
                 _tc("x.read", '{"n": 7}'),               # refused #2 → auto-delegate
                 # two finals: the verify-after-delegate bounce costs one
                 _final("done"), _final("done")])
    out, events, seen = _auto_rt(log, script, monkeypatch)
    assert out["status"] == "ok"
    assert log.count("specialist.delegate") == 1
    i_badge = _event_index(events, lambda e: e["type"] == "badge")
    i_deleg = _event_index(events, lambda e: e["type"] == "tool_result"
                           and e["data"].get("tool") == "specialist.delegate")
    assert i_badge is not None and i_deleg is not None
    assert events[i_badge]["data"]["label"] == "j-space: full"
    assert i_badge < i_deleg
    i_todos = _event_index(events, lambda e: e["type"] == "todos"
                           and (e["data"].get("items") or []))
    assert i_todos is not None and i_todos < i_deleg
    reports = [m["content"] for turns in seen for m in turns
               if "Blocked-call streak" in (m.get("content") or "")]
    assert reports and "loop guard, not the brain" in reports[-1]


def test_auto_delegate_non_jspace_unchanged(tmp_path, monkeypatch):
    """No badge skill loaded: the salvage lane behaves exactly as before —
    no badge event, no todos ceremony, no attribution sentence."""
    log = []
    script = (_spin_reads(6)
              + [_tc("x.read", '{"n": 6}'),
                 _tc("x.read", '{"n": 7}'),
                 _final("done"), _final("done")])   # bounce, see above
    out, events, seen = _auto_rt(log, script, monkeypatch)
    assert out["status"] == "ok"
    assert log.count("specialist.delegate") == 1
    assert not any(e["type"] == "badge" for e in events)
    assert not any(e["type"] == "progress"
                   and "closed the j-space ceremony" in e["data"].get("label", "")
                   for e in events)
    reports = [m["content"] for turns in seen for m in turns
               if "Blocked-call streak" in (m.get("content") or "")]
    assert reports and "loop guard, not the brain" not in reports[-1]
