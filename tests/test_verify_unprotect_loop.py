"""Loop level for caller-declared tamper exceptions (verify.unprotect,
threaded from specialist.delegate allow_test_edits): the child's run is
gated on a verify command whose protect globs hash-guard the tests. The
task REQUIRES the test edit ("adjust the test so it checks the new
name"), so the caller declares the path — declared, the edit passes on
exit code alone; undeclared, the same edit dies as VERIFIER TAMPERING
(live: j-space-loop delegation killed as "not converging"). Real loop,
fake model (convention: the scaffolding helpers are copied from
test_loop_regressions, not shared)."""
import asyncio
import json
from pathlib import Path

from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.tool_base import ToolResult

CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
    # Bare bash for the check command — the explicit operator opt-in to
    # skip firejail, same as tests/test_verify_env.py.
    "tools": {"code": {"run": {"sandbox_prefix": []}}},
}

OLD_TEST = "def test_svc(): assert TIMEOUT == 30"
NEW_TEST = "def test_svc(): assert TIMEOUT_S == 30"


class _RealWrite:
    """fs.write that actually writes into the work_root — the verify gate's
    tamper comparison is over real file hashes."""
    private = False
    name = "fs.write"

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        p = Path(ctx.work_root) / args["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"])
        return ToolResult(status="ok", result={"path": args["path"]})


class _Registry:
    def __init__(self):
        self._tools = {"fs.write": _RealWrite()}

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


def _runtime(script):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.config = dict(CFG)
    rt.registry = _Registry()
    rt.selector = ToolSelector(rt.registry, rt.config)
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

    async def fake_turn(messages, tools_schema, model=None, think=True, sampling=None):
        return {"message": turns.pop(0), "usage": {}}
    rt._model_turn = fake_turn
    return rt


def _run_child(tmp_path, verify):
    """One delegated-child-shaped run: the workspace has test_service.py,
    the child adjusts it to the new name (the task's own requirement),
    then finishes against the verify gate."""
    (tmp_path / "test_service.py").write_text(OLD_TEST)
    rt = _runtime([
        _tc("fs.write", json.dumps({"path": "test_service.py",
                                    "content": NEW_TEST})),
        {"role": "assistant", "content": "renamed, test adjusted"},
    ])
    return asyncio.run(rt.run("rename the setting and adjust the test",
                              work_root=str(tmp_path), verify=verify))


def test_declared_test_edit_passes_on_exit_code_alone(tmp_path):
    out = _run_child(tmp_path, {"command": "true", "max_checks": 1,
                                "unprotect": ["test_service.py"]})
    assert out["status"] == "ok"
    assert out["verified"] is True
    assert "TAMPERING" not in out["answer"]


def test_undeclared_test_edit_dies_as_tampering(tmp_path):
    out = _run_child(tmp_path, {"command": "true", "max_checks": 1})
    assert out["status"] == "unverified"
    assert out["verified"] is False
    assert "VERIFIER TAMPERING" in out["answer"]
    assert "test_service.py" in out["answer"]


def test_unprotect_listed_other_file_still_tampers(tmp_path):
    """The exception never widens: declaring a DIFFERENT path leaves
    test_service.py fully protected."""
    out = _run_child(tmp_path, {"command": "true", "max_checks": 1,
                                "unprotect": ["test_other.py"]})
    assert out["status"] == "unverified"
    assert "VERIFIER TAMPERING" in out["answer"]
