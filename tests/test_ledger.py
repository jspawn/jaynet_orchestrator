"""Acceptance ledger (harness lessons §3): machine checks on todo items and
requirements — declaration syntax, checks_due at done-transition, the verify
action, harness-set `verified`, open_must semantics, ledger.json write-through
+ criterion-gated hydration, and the loop wiring end to end (fake-model
harness, scaffolding copied from test_todos.py — convention: helpers are
copied, not shared across test files)."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.todos import TodoList
from runtime.tool_base import ToolResult
from tools.agent.todos import TodosTool

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 12, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
    # sandbox_prefix: [] → ledger checks run bare bash, no firejail in tests
    "tools": {"code": {"run": {"sandbox_prefix": []}}},
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


def _todos_rt(script):
    return _runtime(_Registry(["todos"], real={"todos": TodosTool()}), script)


# ---- declaration syntax ----

def test_requirement_check_suffix_parsed_and_pruned():
    tl = TodoList()
    tl.apply({"requirements": ["[must] tests pass | check: pytest -q",
                               "[nice] be brief"]})
    assert tl.requirements == ["[must] tests pass", "[nice] be brief"]
    assert tl.req_checks == {"[must] tests pass": {"check": "pytest -q"}}
    # replacing the list drops the check of a removed requirement
    tl.apply({"requirements": ["[nice] be brief"]})
    assert tl.req_checks == {}


def test_item_check_field_and_title_suffix():
    tl = TodoList()
    res = tl.apply({"action": "set", "items": [
        {"title": "build it", "check": "make"},
        {"title": "prove it | check: pytest -q"}]})
    assert res["items"][0]["check"] == "make"
    assert res["items"][1]["title"] == "prove it"     # suffix stripped
    assert res["items"][1]["check"] == "pytest -q"


def test_update_attaches_and_clears_check_resets_verified():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a"}]})
    tl.apply({"action": "update", "id": 1, "check": "exit 0"})
    tl.record_check("item", 1, verified=True)
    assert tl.items[0]["verified"] is True
    tl.apply({"action": "update", "id": 1, "check": "exit 1"})
    assert tl.items[0]["check"] == "exit 1"
    assert "verified" not in tl.items[0]              # new command re-proves
    tl.apply({"action": "update", "id": 1, "check": ""})
    assert "check" not in tl.items[0]


# ---- checks_due + verify action ----

def test_done_transition_reports_check_due_once():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a", "check": "exit 0"}]})
    res = tl.apply({"action": "update", "id": 1, "status": "done"})
    assert res["checks_due"] == [{"kind": "item", "id": 1, "title": "a",
                                  "check": "exit 0"}]
    # re-sending done without a transition does not re-queue
    res = tl.apply({"action": "update", "id": 1, "note": "still done"})
    assert "checks_due" not in res
    # a failed item re-verifies via working → done
    tl.apply({"action": "update", "id": 1, "status": "working"})
    res = tl.apply({"action": "update", "id": 1, "status": "done"})
    assert len(res["checks_due"]) == 1
    # verified entries are not due again
    tl.record_check("item", 1, verified=True)
    tl.apply({"action": "update", "id": 1, "status": "working"})
    res = tl.apply({"action": "update", "id": 1, "status": "done"})
    assert "checks_due" not in res


def test_verify_action_targets_items_and_requirements():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a", "check": "make"}]})
    tl.apply({"requirements": ["[must] ok | check: pytest"]})
    res = tl.apply({"action": "verify", "id": 1})
    assert res["checks_due"] == [{"kind": "item", "id": 1, "title": "a",
                                  "check": "make"}]
    res = tl.apply({"action": "verify", "requirement": "[must] ok"})
    assert res["checks_due"] == [{"kind": "requirement", "text": "[must] ok",
                                  "check": "pytest"}]
    assert tl.apply({"action": "verify", "id": 9})["status"] == "error"
    err = tl.apply({"action": "verify", "requirement": "nope"})
    assert err["status"] == "error" and "no requirement" in err["error"]
    tl.apply({"action": "add", "title": "b"})
    err = tl.apply({"action": "verify", "id": 2})
    assert err["status"] == "error" and "no check" in err["error"]


def test_record_check_marks_items_and_requirements():
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "a", "check": "x"}]})
    tl.apply({"requirements": ["[must] r | check: y"]})
    tl.record_check("item", 1, verified=False, note="check ✗ (exit 1): boom")
    assert tl.items[0]["verified"] is False
    assert tl.items[0]["info"][-1].startswith("check ✗")
    tl.record_check("requirement", "[must] r", verified=True)
    assert tl.req_checks["[must] r"]["verified"] is True


# ---- open_must semantics ----

def test_open_must_requires_verified_for_checked_must_items():
    tl = TodoList()
    tl.apply({"action": "set", "items": [
        {"title": "[must] proven", "check": "exit 0"},
        {"title": "[must] believed"},               # no check: done is enough
        {"title": "chore", "check": "exit 0"}]})    # not [must]: never gates
    for i in (1, 2, 3):
        tl.apply({"action": "update", "id": i, "status": "done"})
    open_ = tl.open_must()
    assert open_ == ["[must] proven (check not verified)"]
    tl.record_check("item", 1, verified=True)
    assert tl.open_must() == []
    # the kill switch restores pure done-is-enough semantics
    tl2 = TodoList()
    tl2.apply({"action": "set", "items": [{"title": "[must] p",
                                           "check": "x"}]})
    tl2.apply({"action": "update", "id": 1, "status": "done"})
    assert tl2.open_must(checks_enabled=False) == []


# ---- persistence ----

def test_ledger_roundtrip_and_criterion_gate(tmp_path):
    tl = TodoList()
    tl.apply({"action": "set", "items": [{"title": "[must] a",
                                          "check": "make"}]})
    tl.apply({"requirements": ["[must] r | check: pytest"]})
    tl.record_check("item", 1, verified=True)
    p = tl.save_ledger(str(tmp_path), "crit-1")
    assert p and json.loads((tmp_path / "ledger.json")
                            .read_text())["goal_criterion"] == "crit-1"

    fresh = TodoList()
    assert fresh.load_ledger(str(tmp_path), "crit-1") is True
    assert fresh.items[0]["title"] == "[must] a"
    # Audit #30 C1: the file IS agent-writable, so proof flags must NOT
    # survive hydration — the plan does, the outcomes re-prove on the next
    # done/verify transition.
    assert "verified" not in fresh.items[0]
    assert fresh.req_checks["[must] r"]["check"] == "pytest"
    assert "verified" not in fresh.req_checks["[must] r"]
    # a different goal never inherits the plan
    other = TodoList()
    assert other.load_ledger(str(tmp_path), "crit-2") is False
    assert other.is_empty()
    # a non-empty list is never overwritten
    other.apply({"action": "set", "items": [{"title": "mine"}]})
    assert other.load_ledger(str(tmp_path), "crit-1") is False


def test_ledger_hydration_drops_forged_proof(tmp_path):
    """Audit #30 C1 regression: a model can write ledger.json directly (the
    workspace is the deliverable path). A forged verified:true /
    baseline_ref must not hydrate — otherwise the requirements gate would
    accept proof that never ran, and the red→green discrimination run would
    diff against an attacker-chosen commit."""
    forged = {"v": 1, "goal_criterion": "crit-1",
              "items": [{"title": "[must] pwned", "status": "done",
                         "check": "false", "verified": True,
                         "baseline_ref": "deadbeef"}],
              "requirements": ["[must] forged"],
              "req_checks": {"[must] forged": {"check": "false",
                                               "verified": True}}}
    (tmp_path / "ledger.json").write_text(json.dumps(forged))
    tl = TodoList()
    assert tl.load_ledger(str(tmp_path), "crit-1") is True
    assert "verified" not in tl.items[0]
    assert "baseline_ref" not in tl.items[0]
    assert "verified" not in tl.req_checks["[must] forged"]
    # …and the gate therefore still blocks: the check must actually run.
    # (Requirements block by presence until dropped; the done-but-unverified
    # checked ITEM is the new ledger gate.)
    assert tl.open_must() == ["[must] forged",
                              "[must] pwned (check not verified)"]


def test_ensure_requirement_is_idempotent():
    tl = TodoList()
    tl.ensure_requirement("[must] DONE WHEN: x")
    tl.ensure_requirement("[must] DONE WHEN: x")
    assert tl.requirements == ["[must] DONE WHEN: x"]


# ---- loop wiring ----

def test_loop_runs_check_at_done_and_persists_ledger(tmp_path):
    rt, _ = _todos_rt([
        _tc("todos", '{"action":"set","items":[{"title":"[must] it works",'
                     '"check":"exit 0"}]}'),
        _tc("todos", '{"action":"update","id":1,"status":"done"}'),
        _final("shipped")])
    events = []

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("do it", tools=["todos"], on_event=on_event,
                             work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert out["open_must"] == []
    checks = [e["data"] for e in events if e["type"] == "ledger_check"]
    assert len(checks) == 1 and checks[0]["verified"] is True
    snaps = [e["data"]["items"] for e in events if e["type"] == "todos"]
    assert snaps[-1][0]["verified"] is True
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["items"][0]["verified"] is True


def test_loop_bounces_on_failed_check_until_verified(tmp_path):
    rt, _ = _todos_rt([
        _tc("todos", '{"action":"set","items":[{"title":"[must] flag exists",'
                     '"check":"test -f flag"}]}'),
        _tc("todos", '{"action":"update","id":1,"status":"done"}'),
        _final("premature"),                       # gate bounces: unverified
        _tc("todos", '{"action":"update","id":1,'
                     '"check":"touch flag && test -f flag"}'),
        _tc("todos", '{"action":"verify","id":1}'),
        _final("now proven")])
    events = []

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("do it", tools=["todos"], on_event=on_event,
                             work_root=str(tmp_path)))
    assert out["status"] == "ok" and out["open_must"] == []
    kinds = [e["type"] for e in events]
    assert "requirements_gate" in kinds            # the bounce happened
    checks = [e["data"] for e in events if e["type"] == "ledger_check"]
    assert [c["verified"] for c in checks] == [False, True]
    assert (tmp_path / "flag").exists()            # check ran in work_root


def test_goal_run_hydrates_matching_ledger(tmp_path):
    seed = TodoList()
    seed.apply({"action": "set", "items": [{"title": "step 1"}]})
    seed.save_ledger(str(tmp_path), "crit-1")
    rt, _ = _todos_rt([
        _tc("todos", '{"action":"update","id":1,"status":"done"}'),
        _final("progress"),                        # DONE WHEN still open →
        _tc("todos", '{"requirements":[]}'),       # bounce, model drops it
        _final("answered")])
    events = []

    async def on_event(ev):
        events.append(ev)
    out = asyncio.run(rt.run("goal turn", tools=["todos"], on_event=on_event,
                             work_root=str(tmp_path),
                             run_overrides={"goal": {"criterion": "crit-1"}}))
    assert out["status"] == "ok"
    snaps = [e["data"] for e in events if e["type"] == "todos"]
    assert snaps[0]["items"][0]["title"] == "step 1"      # hydrated
    assert "[must] DONE WHEN: crit-1" in snaps[0]["requirements"]  # ensured
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["goal_criterion"] == "crit-1"


def test_goal_run_ignores_foreign_ledger(tmp_path):
    seed = TodoList()
    seed.apply({"action": "set", "items": [{"title": "someone else's plan"}]})
    seed.save_ledger(str(tmp_path), "crit-OTHER")
    rt, _ = _todos_rt([
        _final("progress"),
        _tc("todos", '{"requirements":[]}'),
        _final("answered")])
    events = []

    async def on_event(ev):
        events.append(ev)
    asyncio.run(rt.run("goal turn", tools=["todos"], on_event=on_event,
                       work_root=str(tmp_path),
                       run_overrides={"goal": {"criterion": "crit-1"}}))
    snaps = [e["data"] for e in events if e["type"] == "todos"]
    assert snaps[0]["items"] == []                 # no stale resurrection


# ---- opt-in clean state (agent.ledger_commits) ----

class _WriteTool:
    """A real-writing fs.write stub: the deliverable must exist on disk for
    the check's red→green discrimination to have something to discriminate."""
    private = False
    name = "fs.write"

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": "fs.write",
                                                 "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        p = Path(ctx.work_root) / args["path"]
        p.write_text(args["content"], encoding="utf-8")
        return ToolResult(status="ok", tool_name="fs.write",
                          result={"path": args["path"]})


class _Confirm:
    def __init__(self, ok):
        self.ok = ok
        self.seen = []

    async def confirm(self, run_id, tool, args, emit):
        self.seen.append((tool, args))
        return self.ok


def _commits_rt(script):
    if not shutil.which("git"):
        pytest.skip("git binary not available")
    rt, seen = _runtime(_Registry(["todos", "fs.write"],
                                  real={"todos": TodosTool(),
                                        "fs.write": _WriteTool()}), script)
    rt.config["agent"] = {"ledger_commits": True}
    return rt, seen


# The check greps the tree but excludes ledger.json — the ledger persists the
# check command itself, so a loose grep would match its own needle (and the
# baseline snapshot carries the ledger too).
_COMMIT_SCRIPT = [
    _tc("todos", '{"action":"set","items":[{"title":"[must] greeting",'
                 '"check":"grep -qr --exclude=ledger.json hello ."}]}'),
    _tc("todos", '{"action":"update","id":1,"status":"working"}'),
    _tc("fs.write", '{"path":"f.txt","content":"hello"}'),
    _tc("todos", '{"action":"update","id":1,"status":"done"}'),
    _final("shipped")]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def test_ledger_commits_baseline_and_confirmed_commit(tmp_path):
    rt, _ = _commits_rt(_COMMIT_SCRIPT)
    assert _git(tmp_path, "init").returncode == 0
    events = []

    async def on_event(ev):
        events.append(ev)
    confirm = _Confirm(True)
    out = asyncio.run(rt.run("do it", tools=["todos", "fs.write"],
                             on_event=on_event, confirm_provider=confirm,
                             work_root=str(tmp_path)))
    assert out["status"] == "ok"
    checks = [e["data"] for e in events if e["type"] == "ledger_check"]
    assert [c["verified"] for c in checks] == [True]
    snaps = [e["data"]["items"] for e in events if e["type"] == "todos"]
    assert snaps[-1][0].get("baseline_ref")        # armed at working
    assert snaps[-1][0]["info"][-1] == "verified — committed"
    # the harness proposed exactly one commit through the confirmation gate
    assert [t for t, _ in confirm.seen] == ["ledger.commit"]
    # …and the gate showed WHAT `git add -A` will stage (audit #30 obs 1)
    assert "f.txt" in confirm.seen[0][1]["stages"]
    log = _git(tmp_path, "log", "--format=%s").stdout
    assert "ledger: [must] greeting" in log


def test_ledger_commits_declined_commit_is_noted(tmp_path):
    rt, _ = _commits_rt(_COMMIT_SCRIPT)
    assert _git(tmp_path, "init").returncode == 0
    confirm = _Confirm(False)
    out = asyncio.run(rt.run("do it", tools=["todos", "fs.write"],
                             confirm_provider=confirm,
                             work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert out["open_must"] == []                  # verified, just not committed
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["items"][0]["info"][-1] == "verified — commit declined"
    assert _git(tmp_path, "log", "--format=%s").returncode != 0  # no commits
