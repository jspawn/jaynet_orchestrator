"""Self-managed state file (agent.state_file; CLM adaptation, arxiv
2609.37725): the agent maintains state.md in its work_root with the fs.*
tools; the loop re-injects it at the prompt tail every turn so it survives
compaction. Config-gated (default off): no file / disabled flag = zero
injection. Loop-level tests use the fake-model harness (convention: the
scaffolding helpers are copied from test_loop_regressions, not shared)."""
import asyncio
import json

from runtime.loop import _DEFAULT_STATE_FILE_INSTRUCTIONS, AgentRuntime, _compact_messages
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
}

HEADER = "— Your state.md"          # the per-turn injection header marker


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


def _sf_rt(script, **state_cfg):
    rt, seen = _runtime(_Registry(["fs.read"]), script)
    rt.config["agent"] = {"state_file": {"enabled": True, **state_cfg}}
    return rt, seen


# ---- file reading + message building (unit level) ----

def test_read_missing_or_empty_file_is_no_injection(tmp_path):
    assert AgentRuntime._read_state_file(tmp_path) == ("", False)
    (tmp_path / "state.md").write_text("   \n  ")
    assert AgentRuntime._read_state_file(tmp_path) == ("", False)
    assert AgentRuntime._read_state_file(None) == ("", False)
    assert AgentRuntime._read_state_file(tmp_path / "gone") == ("", False)


def test_read_caps_over_max_chars_newest_kept(tmp_path):
    (tmp_path / "state.md").write_text("OLD-" + "x" * 100 + "-NEW")
    content, truncated = AgentRuntime._read_state_file(tmp_path, max_chars=50)
    assert truncated and len(content) == 50
    assert content.endswith("-NEW") and "OLD-" not in content


def test_build_state_anchor_header_and_truncation_note():
    assert AgentRuntime._build_state_anchor("") is None
    a = AgentRuntime._build_state_anchor("BODY")
    assert a["role"] == "system" and HEADER in a["content"]
    assert "survives compaction" in a["content"]      # the one-line reminder
    assert a["content"].endswith("BODY")
    assert "TRUNCATED" not in a["content"]
    assert "TRUNCATED" in AgentRuntime._build_state_anchor("BODY", True)["content"]


# ---- loop-level behavior ----

def test_no_file_no_injection(tmp_path):
    rt, seen = _sf_rt([_final("done")])
    out = asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert out["status"] == "ok"
    assert len(seen) == 1
    assert all(HEADER not in (m["content"] or "") for m in seen[0])
    # …but the one-time instruction overlay did land at run start.
    assert any("continuity memory" in (m["content"] or "") for m in seen[0])


def test_file_content_injected_at_trailing_position(tmp_path):
    (tmp_path / "state.md").write_text("GOAL: ship it\nNEXT: run tests")
    rt, seen = _sf_rt([_tc("fs.read", "{}"), _final("done")])
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert len(seen) == 2
    for call in seen:
        last = call[-1]
        assert last["role"] == "system" and HEADER in last["content"]
        assert "GOAL: ship it" in last["content"]
    # Trailing = after the last transcript message (the tool result).
    assert seen[1][-2].get("role") == "tool"
    # The injection is never persisted into the transcript…
    assert all(HEADER not in (m["content"] or "") for m in seen[1][:-1])


def test_injection_re_read_each_turn(tmp_path):
    p = tmp_path / "state.md"
    p.write_text("v1")
    rt, seen = _sf_rt([_tc("fs.read", "{}"), _final("done")])

    real_read = AgentRuntime._read_state_file

    def editing_read(work_root, max_chars=8000):
        content, truncated = real_read(work_root, max_chars)
        if content == "v1":
            p.write_text("v2")          # the agent's fs.write lands mid-run
        return content, truncated
    rt._read_state_file = editing_read
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert seen[0][-1]["content"].endswith("v1")
    assert seen[1][-1]["content"].endswith("v2")


def test_over_max_chars_capped_and_noted(tmp_path):
    (tmp_path / "state.md").write_text("OLD-" + "x" * 300 + "-NEW")
    rt, seen = _sf_rt([_final("done")], max_chars=80)
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    last = seen[0][-1]
    assert HEADER in last["content"] and "TRUNCATED" in last["content"]
    assert last["content"].endswith("-NEW") and "OLD-" not in last["content"]


def test_injection_survives_compact_messages(tmp_path):
    """A big tool result gets stubbed by _compact_messages; the state-file
    injection rides outside the transcript, so the next turn still ends with
    it verbatim (survival by construction)."""
    (tmp_path / "state.md").write_text("STATE-BODY")
    rt, seen = _runtime(_Registry([], real={"x.big": _BigTool()}),
                        [_tc("x.big", "{}"), _final("done")])
    rt.config["agent"] = {"state_file": {"enabled": True}}
    rt.config["compaction"] = {"enabled": True, "max_result_chars": 100,
                               "keep_last": 0, "every": 1}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert len(seen) == 2
    tool_msg = seen[1][-2]
    assert tool_msg.get("role") == "tool"
    assert '"__compacted__"' in tool_msg["content"]     # the stub happened
    assert "z" * 5000 not in json.dumps(seen[1][:-1])   # transcript shrunk
    last = seen[1][-1]
    assert HEADER in last["content"] and last["content"].endswith("STATE-BODY")


def test_compact_messages_never_touches_the_injection():
    """Unit belt-and-braces: compaction only stubs role=tool messages, so a
    trailing state anchor in the call list is never eligible."""
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "tool", "name": "t", "content": "y" * 3000}]
    anchor = AgentRuntime._build_state_anchor("STATE-BODY")
    call = AgentRuntime._apply_anchor(msgs, anchor, "trailing")
    assert _compact_messages(call, {"enabled": True, "max_result_chars": 100,
                                    "keep_last": 0}) == 1
    assert call[-1] is anchor and call[-1]["content"].endswith("STATE-BODY")


def test_disabled_flag_no_injection_even_with_file(tmp_path):
    (tmp_path / "state.md").write_text("STATE-BODY")
    rt, seen = _runtime(_Registry(["fs.read"]), [_final("done")])
    rt.config["agent"] = {"state_file": {"enabled": False}}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    flat = [m["content"] or "" for call in seen for m in call]
    assert all(HEADER not in c and "STATE-BODY" not in c for c in flat)
    assert all("continuity memory" not in c for c in flat)   # no overlay either


def test_default_config_is_off(tmp_path):
    """The shipped default (no agent.state_file section at all) injects
    nothing — zero cost, harness-summary stays the only continuity path."""
    (tmp_path / "state.md").write_text("STATE-BODY")
    rt, seen = _runtime(_Registry(["fs.read"]), [_final("done")])
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    flat = [m["content"] or "" for call in seen for m in call]
    assert all(HEADER not in c and "STATE-BODY" not in c for c in flat)


def test_custom_instructions_replace_default(tmp_path):
    rt, seen = _sf_rt([_final("done")], instructions="CUSTOM STATE RULES")
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    flat = [m["content"] or "" for m in seen[0]]
    assert any("CUSTOM STATE RULES" in c for c in flat)
    assert all("Do not paste transcripts" not in c for c in flat)


def test_default_instructions_used_when_unset(tmp_path):
    rt, seen = _sf_rt([_final("done")])
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    assert any(_DEFAULT_STATE_FILE_INSTRUCTIONS in (m["content"] or "")
               for m in seen[0])


def test_anchor_on_state_file_rides_inside(tmp_path):
    """anchor.mode on: the state file rides inside the working anchor at the
    anchor's placement — exactly how the todo list behaves."""
    (tmp_path / "state.md").write_text("STATE-BODY")
    rt, seen = _sf_rt([_final("done")])
    rt.config["agent"]["anchor"] = {"mode": "trailing"}
    asyncio.run(rt.run("do a thing", work_root=str(tmp_path)))
    last = seen[0][-1]
    assert "Working anchor" in last["content"] and HEADER in last["content"]
    assert "STATE-BODY" in last["content"]
