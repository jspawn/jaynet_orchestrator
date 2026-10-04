"""Per-turn budget visibility (agent.anchor.budget, default on): the brain
never saw its iteration budget, so it over-verified trivial answers and
over-searched — the eval-flake class this fixes. A one-line readout
("budget: iteration 3/8", used/limit; just the used count when uncapped)
rides at the prompt tail every turn: inside the working anchor when
anchor.mode is on, standalone at the todos re-injection slot otherwise.
Rebuilt per turn, never persisted, survives compaction by construction.
Loop-level tests use the fake-model harness (convention: the scaffolding
helpers are copied from test_loop_regressions, not shared)."""
import asyncio
import json

from runtime.loop import AgentRuntime, _compact_messages
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}

MARK = "budget: iteration"          # the per-turn readout marker


class _StubTool:
    private = False

    def __init__(self, name):
        self.name = name

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}


class _BigTool:
    """Returns a large tool result so the compaction pass has something to stub."""
    private = False
    name = "x.big"

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        return ToolResult(status="ok", result={"blob": "z" * 5000})


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
    """One assistant message carrying a single tool call."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def _final(text="done"):
    return {"role": "assistant", "content": text}


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


# ---- message building (unit level) ----

class _Budget:
    def __init__(self, iterations, max_iterations):
        self.iterations = iterations
        self.max_iterations = max_iterations


def test_build_budget_anchor_used_over_limit():
    a = AgentRuntime._build_budget_anchor(_Budget(3, 8))
    assert a == {"role": "system", "content": "budget: iteration 3/8"}


def test_build_budget_anchor_unlimited_shows_used_only():
    a = AgentRuntime._build_budget_anchor(_Budget(3, 0))
    assert a["content"] == "budget: iteration 3"


# ---- loop-level behavior ----

def test_budget_line_trailing_each_turn_with_right_counts(tmp_path):
    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_tc("fs.read", "{}"), _final("done")])
    out = asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert len(seen) == 2
    for i, call in enumerate(seen, start=1):
        last = call[-1]
        assert last["role"] == "system"
        assert last["content"].endswith(f"{MARK} {i}/8")


def test_budget_line_never_persisted(tmp_path):
    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_tc("fs.read", "{}"), _final("done")])
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    # Turn 2's transcript (everything before the trailing injection) carries
    # no budget line from turn 1.
    assert all(MARK not in (m.get("content") or "") for m in seen[1][:-1])


def test_budget_line_unlimited_config(tmp_path):
    rt, seen = _runtime(_Registry(["fs.read"]), [_final("done")])
    rt.config["budgets"] = {**CFG["budgets"], "max_iterations": 0}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert seen[0][-1]["content"] == f"{MARK} 1"


def test_budget_line_absent_when_disabled(tmp_path):
    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_tc("fs.read", "{}"), _final("done")])
    rt.config["agent"] = {"anchor": {"budget": False}}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    flat = [m.get("content") or "" for call in seen for m in call]
    assert all(MARK not in c for c in flat)


def test_budget_line_rides_inside_working_anchor(tmp_path):
    """anchor.mode on: the readout is appended inside the working-anchor
    body at the anchor's placement — the state_file/todos pattern."""
    rt, seen = _runtime(_Registry(["fs.read"]), [_final("done")])
    rt.config["agent"] = {"anchor": {"mode": "trailing"}}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    last = seen[0][-1]
    assert "Working anchor" in last["content"]
    assert last["content"].endswith(f"{MARK} 1/8")


def test_budget_line_survives_compaction(tmp_path):
    """A big tool result gets stubbed by _compact_messages; the budget
    readout rides outside the transcript, so the next turn still ends with
    it, freshly rebuilt (survival by construction)."""
    rt, seen = _runtime(_Registry([], real={"x.big": _BigTool()}),
                        [_tc("x.big", "{}"), _final("done")])
    rt.config["compaction"] = {"enabled": True, "max_result_chars": 100,
                               "keep_last": 0, "every": 1}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert len(seen) == 2
    tool_msg = seen[1][-2]
    assert tool_msg.get("role") == "tool"
    assert '"__compacted__"' in tool_msg["content"]     # the stub happened
    assert "z" * 5000 not in json.dumps(seen[1][:-1])   # transcript shrunk
    assert seen[1][-1]["content"].endswith(f"{MARK} 2/8")


def test_compact_messages_never_touches_the_readout():
    """Unit belt-and-braces: compaction only stubs role=tool messages, so a
    trailing budget anchor in the call list is never eligible."""
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "tool", "name": "t", "content": "y" * 3000}]
    anchor = AgentRuntime._build_budget_anchor(_Budget(2, 8))
    call = AgentRuntime._apply_anchor(msgs, anchor, "trailing")
    assert _compact_messages(call, {"enabled": True, "max_result_chars": 100,
                                    "keep_last": 0}) == 1
    assert call[-1] is anchor and call[-1]["content"] == f"{MARK} 2/8"


def test_capped_run_records_cap_plus_one_with_synthesis(tmp_path):
    """The semantic the eval runner's cap check relies on: a run cut at
    max_iterations records iterations == cap + 1 (tick counts the tick that
    trips the ceiling) with status budget_exceeded, and the final-synthesis
    turn is never ticked — so cap + 1 is the ceiling even when the run
    synthesizes a final answer."""
    rt, seen = _runtime(_Registry(["fs.read"]),
                        [_tc("fs.read", "{}"), _tc("fs.read", "{}"),
                         _final("synthesized answer")])
    rt.config["budgets"] = {**CFG["budgets"], "max_iterations": 2}
    out = asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert out["status"] == "budget_exceeded"
    assert out["error"].startswith("max_iterations")
    assert out["budget"]["iterations"] == 3            # cap + 1, not more
    assert "synthesized answer" in out["answer"]       # final_synthesis ran
    assert len(seen) == 3                              # 2 capped + 1 synthesis


def test_final_synthesis_abstains_without_material(tmp_path):
    """No tool result gathered → final_synthesis returns None without a
    model call and the run keeps the raw termination text — an empty run
    fails honestly, not with a polished non-answer."""
    import types
    rt, seen = _runtime(_Registry(["fs.read"]), [])
    rs = types.SimpleNamespace(messages=[{"role": "user", "content": "hi"}])
    assert asyncio.run(rt._final_synthesis(rs)) is None
    assert seen == []                                  # no model turn spent
