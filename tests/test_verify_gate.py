"""Verifier-gated termination: a run with a `verify` check isn't done until the
check passes, and the agent can't edit the tests or fake a vacuous green."""
import asyncio
import tempfile
from pathlib import Path
from runtime.loop import AgentRuntime

# _snapshot_protected is a staticmethod — grab the raw function.
_snap = AgentRuntime.__dict__["_snapshot_protected"].__func__


async def _val(v):
    return v


class _Stub:
    _normalize_verify = AgentRuntime._normalize_verify
    _snapshot_protected = staticmethod(_snap)
    _verify = AgentRuntime._verify
    def __init__(self, cfg=None):
        self.config = cfg or {}


class _Ctx:
    config = {}


def _run(stub, spec, baseline, root):
    st = {"attempts": 0, "passed": False, "baseline": baseline}
    return asyncio.run(stub._verify(spec, st, _Ctx(), str(root)))


def _mktests(body="def test_ok(): assert 1"):
    root = Path(tempfile.mkdtemp())
    (root / "test_a.py").write_text(body)
    return root, ["**/test_*.py"]


# ---- normalization ----
def test_normalize_string_and_dict_and_empty():
    s = _Stub()
    spec = s._normalize_verify("pytest -q")
    assert spec["command"] == "pytest -q" and spec["max_checks"] == 4 and spec["timeout_s"] == 180
    assert "**/test_*.py" in spec["protect"]
    spec2 = s._normalize_verify({"command": "ruff check .", "max_checks": 2, "protect": ["x.py"]})
    assert spec2["max_checks"] == 2 and spec2["protect"] == ["x.py"]
    assert s._normalize_verify("") is None
    assert s._normalize_verify(None) is None
    assert s._normalize_verify({"command": "   "}) is None


def test_normalize_uses_config_defaults():
    s = _Stub({"agent": {"verify": {"max_checks": 7, "timeout_s": 30}}})
    spec = s._normalize_verify("make test")
    assert spec["max_checks"] == 7 and spec["timeout_s"] == 30


# ---- snapshot ----
def test_snapshot_captures_only_tests_and_sees_change():
    root = Path(tempfile.mkdtemp())
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_x(): assert 1")
    (root / "src.py").write_text("x = 1")
    pats = ["**/test_*.py", "**/tests/**/*.py", "**/conftest.py"]
    a = _snap(root, pats)
    assert "tests/test_a.py" in a and "src.py" not in a
    (root / "tests" / "test_a.py").write_text("def test_x(): assert 0")
    assert _snap(root, pats) != a


# ---- verdicts ----
def _spec(pats):
    return {"command": "pytest -q", "protect": pats, "max_checks": 4, "timeout_s": 30}


def test_pass_on_zero_exit_with_real_tests():
    root, pats = _mktests(); base = _snap(root, pats)
    s = _Stub(); s._run_verify_command = lambda *a, **k: _val((0, "== 3 passed in 0.1s =="))
    ok, rep = _run(s, _spec(pats), base, root)
    assert ok and "passed" in rep


def test_fail_on_nonzero_exit():
    root, pats = _mktests(); base = _snap(root, pats)
    s = _Stub(); s._run_verify_command = lambda *a, **k: _val((1, "1 failed, 2 passed"))
    ok, rep = _run(s, _spec(pats), base, root)
    assert not ok and "FAILED" in rep


def test_fail_on_test_tampering():
    root, pats = _mktests("def test(): assert real_impl()")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert True")   # edit the test to force a green
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _spec(pats), base, root)
    assert not ok and "TAMPERING" in rep


def test_fail_on_vacuous_pass():
    root, pats = _mktests("x = 1")   # no tests collected
    base = _snap(root, pats)
    s = _Stub(); s._run_verify_command = lambda *a, **k: _val((0, "no tests ran in 0.01s"))
    ok, rep = _run(s, _spec(pats), base, root)
    assert not ok and "NO tests" in rep


def test_pass_when_agent_authored_new_tests():
    # The delegate flow: the agent WRITES its own tests, then implements against
    # them. A file newly CREATED under the protect globs is not tampering.
    root = Path(tempfile.mkdtemp())
    pats = ["**/test_*.py"]
    base = _snap(root, pats)                 # empty baseline: no tests at run start
    async def runner(cmd, cwd, to, ctx):
        (root / "test_new.py").write_text("def test_x(): assert 1")   # child authors tests
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _spec(pats), base, root)
    assert ok, rep


def test_fail_on_baseline_test_deleted():
    root, pats = _mktests()
    base = _snap(root, pats)
    async def runner(cmd, cwd, to, ctx):
        (root / "test_a.py").unlink()        # baseline test deleted mid-check
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _spec(pats), base, root)
    assert not ok and "TAMPERING" in rep
