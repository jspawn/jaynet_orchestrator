"""Verifier-gated termination: a run with a `verify` check isn't done until the
check passes, and the agent can't edit the tests or fake a vacuous green."""
import asyncio
import tempfile
from pathlib import Path

from runtime.loop import AgentRuntime

# _snapshot_protected is a staticmethod — grab the raw function. It lives on
# VerifyMixin (runtime/verify.py) since the loop split, so go through attribute
# lookup (which resolves inheritance), not AgentRuntime.__dict__.
_snap = AgentRuntime._snapshot_protected


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


def test_normalize_protect_coercion():
    """Models send protect in odd shapes — true ('yes, guard the tests'),
    a bare string, false. A bad shape must never weaken the tamper guard
    (live: protect: true crashed with TypeError: 'bool' is not iterable)."""
    s = _Stub()
    spec = s._normalize_verify({"command": "pytest -q", "protect": True})
    assert "**/test_*.py" in spec["protect"]          # the default set
    spec = s._normalize_verify({"command": "pytest -q", "protect": False})
    assert "**/test_*.py" in spec["protect"]          # false ≠ disable
    spec = s._normalize_verify({"command": "pytest -q", "protect": "x.py"})
    assert spec["protect"] == ["x.py"]                # bare string → one path


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


def test_tamper_records_changed_files_in_state():
    """The caller-facing chain (j-space-loop live validation 2026-10-05,
    child unverified on an instructed test edit): WHICH protected files
    changed is recorded on the verify state, so the loop can surface it in
    the run's error/return dict — the delegate tool turns it into the
    allow_test_edits remedy for the calling brain."""
    root, pats = _mktests("def test(): assert real_impl()")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert True")
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    st = {"attempts": 0, "passed": False, "baseline": base}
    ok, rep = asyncio.run(s._verify(_spec(pats), st, _Ctx(), str(root)))
    assert not ok and "TAMPERING" in rep
    assert st["tampered"] == ["test_a.py"]


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


# ---- unprotect (caller-declared tamper exceptions) ----

def test_normalize_unprotect_shapes():
    """unprotect is list-shaped like protect, but a bad shape must never
    WIDEN the exception (the opposite failure direction): non-list/None →
    empty. A bare string means exactly that one path."""
    s = _Stub()
    spec = s._normalize_verify({"command": "pytest -q",
                                "unprotect": ["test_service.py"]})
    assert spec["unprotect"] == ["test_service.py"]
    assert s._normalize_verify("pytest -q")["unprotect"] == []
    assert s._normalize_verify(
        {"command": "pytest -q", "unprotect": True})["unprotect"] == []
    assert s._normalize_verify(
        {"command": "pytest -q", "unprotect": "t.py"})["unprotect"] == ["t.py"]
    s2 = _Stub({"agent": {"verify": {"unprotect": ["cfg_test.py"]}}})
    assert s2._normalize_verify("pytest -q")["unprotect"] == ["cfg_test.py"]


def _uspec(pats, unprotect):
    return {"command": "pytest -q", "protect": pats, "max_checks": 4,
            "timeout_s": 30, "unprotect": unprotect}


def test_unprotect_declared_test_edit_is_not_tampering():
    """The task says 'adjust the test to the new name': the caller declares
    the path, the child edits it, the verify gate passes on exit code alone."""
    root, pats = _mktests("def test(): assert TIMEOUT == 30")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert TIMEOUT_S == 30")   # the required edit
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _uspec(pats, ["test_a.py"]), base, root)
    assert ok and "passed" in rep


def test_unprotect_never_widens():
    """A protected file NOT in the unprotect list still trips the tamper
    guard, even with another file declared."""
    root, pats = _mktests("def test(): assert real_impl()")
    (root / "test_b.py").write_text("def test_b(): assert real_impl()")
    base = _snap(root, pats)
    async def runner(cmd, cwd, to, ctx):
        (root / "test_b.py").write_text("def test_b(): assert True")  # undeclared
        return (0, "2 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _uspec(pats, ["test_a.py"]), base, root)
    assert not ok and "TAMPERING" in rep and "test_b.py" in rep


def test_unprotect_keeps_exit_code_and_vacuous_checks():
    """Declaring a path waives ONLY the tamper comparison: a red exit and a
    vacuous green still fail honestly."""
    root, pats = _mktests()
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def red(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert TIMEOUT_S == 30")
        return (1, "1 failed")
    s = _Stub(); s._run_verify_command = red
    ok, rep = _run(s, _uspec(pats, ["test_a.py"]), base, root)
    assert not ok and "FAILED" in rep and "TAMPERING" not in rep
    async def vacuous(cmd, cwd, to, ctx):
        tf.write_text("x = 1")
        return (0, "no tests ran in 0.01s")
    s2 = _Stub(); s2._run_verify_command = vacuous
    ok, rep = _run(s2, _uspec(pats, ["test_a.py"]), base, root)
    assert not ok and "NO tests" in rep


# ---- unprotect path normalization + audit trail (config audit D4) ----

def test_unprotect_dot_slash_and_plain_spellings_both_match():
    """"./test_a.py" and "test_a.py" are the same snapshot key — a leading
    "./" must not silently keep protection on the intended file."""
    root, pats = _mktests("def test(): assert TIMEOUT == 30")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert TIMEOUT_S == 30")
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _uspec(pats, ["./test_a.py"]), base, root)
    assert ok and "TAMPERING" not in rep


def test_unprotect_absolute_path_inside_root_matches():
    """An absolute declared path under the work root relativizes to the same
    snapshot key; outside the root it can never match (and says so)."""
    root, pats = _mktests("def test(): assert TIMEOUT == 30")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert TIMEOUT_S == 30")
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _uspec(pats, [str(tf)]), base, root)
    assert ok and "TAMPERING" not in rep


def test_unprotect_unmatched_declared_path_is_diagnosed():
    """A declared path matching no protected file must say so in the report —
    protection still applies, and the brain sees WHY instead of dying as an
    unexplained 'verifier stuck'."""
    root, pats = _mktests("def test(): assert real_impl()")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert True")   # the declared path was a typo
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    ok, rep = _run(s, _uspec(pats, ["test_servce.py"]), base, root)
    assert not ok and "TAMPERING" in rep
    assert "matched NO protected file" in rep and "test_servce.py" in rep


def test_unprotect_waiver_leaves_a_visible_record():
    """A run that went green with tamper protection lifted is indistinguishable
    from a clean one unless the waiver is recorded — the report (which rides
    the emitted verify event) names the exempted files, and the verify state
    carries them for programmatic consumers."""
    root, pats = _mktests("def test(): assert TIMEOUT == 30")
    base = _snap(root, pats)
    tf = root / "test_a.py"
    async def runner(cmd, cwd, to, ctx):
        tf.write_text("def test(): assert TIMEOUT_S == 30")
        return (0, "1 passed")
    s = _Stub(); s._run_verify_command = runner
    st = {"attempts": 0, "passed": False, "baseline": base}
    ok, rep = asyncio.run(
        s._verify(_uspec(pats, ["test_a.py"]), st, _Ctx(), str(root)))
    assert ok
    assert "tamper waiver" in rep and "test_a.py" in rep
    assert st["unprotect_applied"] == ["test_a.py"]
    assert st["unprotect_unmatched"] == []
