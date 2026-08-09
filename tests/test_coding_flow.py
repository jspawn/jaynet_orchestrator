"""Coding-flow upgrades: repo map / project instructions (context_pack), the
verify baseline pre-run ("not worse" acceptance), code.delegate's isolated
worktree mode, the architect's per-unit mechanical verify, and the spawn
work_root confinement guard."""
import asyncio
import subprocess
from pathlib import Path

from runtime.context_pack import (_MAX_FILES, coding_context,
                                  project_instructions, repo_map)
from runtime.loop import AgentRuntime
from runtime.selector import ToolSelector
from runtime.tool_base import ToolContext, ToolResult
from runtime.verify import VerifyMixin, _verify_sig
from tools.agent.architect import Architect
from tools.code.delegate import CodeDelegate, _make_worktree, _worktree_report

# Loop-driving scaffolding copied from test_loop_regressions (convention:
# helpers are copied, not shared across test files).
CFG = {
    "orchestrator": {"model": "local-orchestrator", "litellm_base": "http://x:4000"},
    "budgets": {"max_iterations": 8, "max_wall_clock_s": 60.0,
                "max_cost_usd": 1.0, "max_total_tokens": 100000},
    "privacy": {"remote_llm_tools": []},
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


# ---- context_pack ----

def _mk_repo(tmp: Path) -> Path:
    (tmp / "app.py").write_text(
        "import os\nfrom pathlib import Path\n\n"
        "class App:\n    pass\n\ndef main():\n    pass\n")
    (tmp / "util.py").write_text("def util_fn():\n    pass\n")
    (tmp / ".venv").mkdir(exist_ok=True)
    (tmp / ".venv" / "skip.py").write_text("def hidden(): pass")
    (tmp / "notes.txt").write_text("not code")
    return tmp


def test_repo_map_lines_symbols_and_skip_dirs(tmp_path):
    out = repo_map(_mk_repo(tmp_path))
    assert "app.py: App, main" in out and "⟵ os, Path" in out
    assert "util.py: util_fn" in out
    assert "skip.py" not in out and "notes.txt" not in out


def test_repo_map_budget_truncates_with_note(tmp_path):
    for i in range(40):
        (tmp_path / f"mod{i:02d}.py").write_text(f"def f{i}():\n    pass\n")
    out = repo_map(tmp_path, max_chars=300)
    assert "omitted" in out and len(out) < 400


def test_repo_map_cached_and_empty_root(tmp_path):
    assert repo_map(tmp_path / "nope") == ""
    a = repo_map(_mk_repo(tmp_path))
    assert repo_map(tmp_path) == a            # same fingerprint → cache hit


def test_repo_map_file_cap_counts_remainder(tmp_path):
    """Audit R1: past _MAX_FILES the loop must STOP appending (and reading)
    and the omitted note must name the true remainder."""
    for i in range(_MAX_FILES + 15):
        (tmp_path / f"m{i:04d}.py").write_text(f"def f{i}():\n    pass\n")
    out = repo_map(tmp_path, max_chars=10**9)
    entries = [l for l in out.splitlines() if not l.startswith("…")]
    assert len(entries) == _MAX_FILES
    assert f"… (15 more files omitted" in out


def test_project_instructions_precedence(tmp_path):
    assert project_instructions(tmp_path) == ""
    (tmp_path / "CLAUDE.md").write_text("claude rules")
    assert "CLAUDE.md" in project_instructions(tmp_path)
    (tmp_path / "AGENTS.md").write_text("agents rules")
    assert "AGENTS.md" in project_instructions(tmp_path)   # earlier in the list
    (tmp_path / "JAYNET.md").write_text("jaynet rules")
    assert "JAYNET.md" in project_instructions(tmp_path)


def test_coding_context_combines_and_disable(tmp_path):
    _mk_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("run pytest first")
    out = coding_context(str(tmp_path), {})
    assert "REPO MAP" in out and "PROJECT INSTRUCTIONS" in out
    no_map = coding_context(str(tmp_path),
                            {"tools": {"code": {"repomap": {"enabled": False}}}})
    assert "REPO MAP" not in no_map and "PROJECT INSTRUCTIONS" in no_map
    assert coding_context(None, {}) == ""


# ---- verify baseline: pre-existing red counts as "not worse" ----

class _V(VerifyMixin):
    pass


def _verify_host(out, code=1):
    v = _V()
    v.config = {}

    async def fake(cmd, cwd, timeout, ctx):
        return code, out
    v._run_verify_command = fake
    return v


def test_verify_accepts_identical_preexisting_failure():
    out = "FAILED test_x.py::test_a - AssertionError in 0.03s"
    v = _verify_host(out, code=1)
    state = {"baseline": {}, "pre": {"code": 1, "sig": _verify_sig(out)}}
    ok, report = asyncio.run(v._verify(
        {"command": "pytest -q", "protect": [], "max_checks": 1, "timeout_s": 1},
        state, None, None))
    assert ok and "pre-existing" in report


def test_verify_rejects_new_or_different_failure():
    v = _verify_host("FAILED test_y.py::test_b - TypeError: nope", code=1)
    state = {"baseline": {},
             "pre": {"code": 1, "sig": _verify_sig("FAILED test_x.py::test_a - AssertionError")}}
    ok, report = asyncio.run(v._verify(
        {"command": "pytest -q", "protect": [], "max_checks": 1, "timeout_s": 1},
        state, None, None))
    assert not ok and "verifier FAILED" in report


def test_verify_green_baseline_still_normal_gate():
    v = _verify_host("1 passed in 0.02s", code=0)
    state = {"baseline": {}, "pre": {"code": 0, "sig": _verify_sig("1 passed")}}
    ok, _ = asyncio.run(v._verify(
        {"command": "pytest -q", "protect": [], "max_checks": 1, "timeout_s": 1},
        state, None, None))
    assert ok


# ---- delegate isolation (real git worktree) ----

def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_make_worktree_and_autocleanup(tmp_path):
    repo = _git_repo(tmp_path)
    ctx = ToolContext(request_id="abc12345", config={}, budget=None,
                      work_root=str(repo))
    wt = asyncio.run(_make_worktree(ctx))
    assert "error" not in wt
    assert Path(wt["path"]).is_dir()
    assert wt["branch"].startswith("jaynet/abc12345-")   # per-call suffix (I3)
    assert wt["base"]                                    # base SHA recorded (I1)
    # scratch dir hidden from the user's git status via .git/info/exclude (I2)
    assert ".jaynet-worktrees/" in (repo / ".git" / "info" / "exclude").read_text()
    rep = asyncio.run(_worktree_report(wt))      # nothing changed → cleaned up
    assert rep == {"cleaned_up": True}
    assert not Path(wt["path"]).exists()


def test_worktree_report_keeps_committed_work(tmp_path):
    """Audit I1 regression: a child that COMMITS in the worktree produced
    something — the report must carry the branch, never delete it."""
    repo = _git_repo(tmp_path)
    ctx = ToolContext(request_id="cab12345", config={}, budget=None,
                      work_root=str(repo))
    wt = asyncio.run(_make_worktree(ctx))
    wtp = Path(wt["path"])
    (wtp / "a.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "."], cwd=wtp, check=True)
    subprocess.run(["git", "commit", "-qm", "child work"], cwd=wtp, check=True)
    rep = asyncio.run(_worktree_report(wt))
    assert rep.get("cleaned_up") is not True
    assert rep["commits"] == 1 and rep["branch"] == wt["branch"]
    assert wtp.is_dir()                        # worktree survives
    out = subprocess.run(["git", "log", "--format=%s", wt["branch"]],
                         cwd=repo, capture_output=True, text=True).stdout
    assert "child work" in out                 # commit reachable on the branch


def test_make_worktree_no_collision_same_run(tmp_path):
    """Audit I3: two isolated delegates in ONE run (same request_id) get
    distinct branches and paths."""
    repo = _git_repo(tmp_path)
    ctx = ToolContext(request_id="same0000", config={}, budget=None,
                      work_root=str(repo))
    a = asyncio.run(_make_worktree(ctx))
    b = asyncio.run(_make_worktree(ctx))
    assert "error" not in a and "error" not in b
    assert a["branch"] != b["branch"] and a["path"] != b["path"]


def test_worktree_report_lists_changes(tmp_path):
    repo = _git_repo(tmp_path)
    ctx = ToolContext(request_id="def67890", config={}, budget=None,
                      work_root=str(repo))
    wt = asyncio.run(_make_worktree(ctx))
    (Path(wt["path"]) / "a.py").write_text("x = 2\n")
    (Path(wt["path"]) / "new.py").write_text("y = 3\n")
    rep = asyncio.run(_worktree_report(wt))
    assert "a.py" in rep["diff_stat"] and "new.py" in rep["untracked"]
    assert rep["worktree"] == wt["path"] and "next" in rep


def test_make_worktree_needs_git_repo(tmp_path):
    ctx = ToolContext(request_id="zzz99999", config={}, budget=None,
                      work_root=str(tmp_path))
    wt = asyncio.run(_make_worktree(ctx))
    assert "git repository" in wt["error"]


class _SpawnCapture:
    """Minimal ctx for CodeDelegate: captures the spawn kwargs, runs nothing."""
    def __init__(self, work_root):
        self.config = {}
        self.work_root = work_root
        self.request_id = "cap00001"
        self.kw = None

    async def spawn(self, task, **kw):
        self.task = task
        self.kw = kw
        return {"status": "ok", "answer": "done", "run_id": "sub1",
                "budget": {}}


def test_delegate_passes_isolated_worktree_to_spawn(tmp_path):
    repo = _git_repo(tmp_path)
    ctx = _SpawnCapture(str(repo))
    res = asyncio.run(CodeDelegate().execute(
        {"task": "change x", "isolated": True}, ctx))
    assert res.status == "ok"
    assert ctx.kw["work_root_path"] and ".jaynet-worktrees" in ctx.kw["work_root_path"]
    assert res.result["isolation"] == {"cleaned_up": True}
    assert "TASK:" in ctx.task


def test_delegate_default_no_worktree(tmp_path):
    repo = _git_repo(tmp_path)
    ctx = _SpawnCapture(str(repo))
    asyncio.run(CodeDelegate().execute({"task": "change x"}, ctx))
    assert ctx.kw["work_root_path"] is None


# ---- architect per-unit verify ----

class _ArchCtx:
    def __init__(self, fail_unit=None):
        self.config = {}
        self.calls = []
        self.fail_unit = fail_unit

    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None, todos_sync=False,
                    work_root_path=None):
        self.calls.append({"name": name, "verify": verify, "task": task})
        if name == "architect" and "REVIEWING" not in task and "Revise" not in task:
            return {"status": "ok",
                    "answer": "GOAL: g\nAPPROACH: a\nUNITS:\n"
                              "- write mod | check: pytest -q\n"
                              "- lint | check: ruff check .\n"}
        if name == "reviewer":
            return {"status": "ok", "answer": "STANCE: agree\nNOTES:"}
        if name and name.startswith("executor:u"):
            n = int(name.rsplit("u", 1)[1])
            if self.fail_unit == n:
                return {"status": "error", "answer": "", "error": "boom",
                        "verified": False}
            return {"status": "ok", "answer": f"unit {n} done", "verified": True,
                    "files_changed": [f"f{n}.py"]}
        return {"status": "ok", "answer": ""}


def test_architect_per_unit_spawns_with_checks():
    ctx = _ArchCtx()
    r = asyncio.run(Architect().execute({"task": "build X"}, ctx))
    executors = [c for c in ctx.calls if c["name"].startswith("executor:u")]
    assert [c["name"] for c in executors] == ["executor:u1", "executor:u2"]
    assert [c["verify"] for c in executors] == ["pytest -q", "ruff check ."]
    assert r.status == "ok" and r.result["per_unit"] is True
    assert r.result["units_done"] == 2 and r.result["verified"] is True
    assert r.result["files_changed"] == ["f1.py", "f2.py"]


def test_architect_per_unit_stops_on_failure():
    ctx = _ArchCtx(fail_unit=1)
    r = asyncio.run(Architect().execute({"task": "build X"}, ctx))
    executors = [c for c in ctx.calls if c["name"].startswith("executor:u")]
    assert len(executors) == 1                    # unit 2 never started
    assert r.status == "error" and r.result["failed_unit"] == 1
    assert r.result["units_done"] == 0


# ---- spawn work_root confinement guard ----

class _SpawnWS:
    name = "test.spawnws"
    private = False

    def needs_confirmation(self, args, ctx):
        return False

    def to_openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": "",
                                                 "parameters": {}}}

    async def execute(self, args, ctx):
        res = await ctx.spawn("x", work_root_path="/etc")
        if res.get("status") == "error":
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=res.get("error"))
        return ToolResult(status="ok", tool_name=self.name, result=res)


def test_spawn_rejects_work_root_outside_roots():
    rt, _ = _runtime(_Registry(["test.spawnws"],
                               real={"test.spawnws": _SpawnWS()}),
                     [_tc("test.spawnws", "{}"), _final("done")])
    out = asyncio.run(rt.run("do a thing"))
    assert "outside this run's allowed roots" in out["trajectory"]
