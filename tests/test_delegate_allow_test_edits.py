"""Caller-declared tamper exceptions (specialist.delegate allow_test_edits):
the CALLING brain — never the child — names relative test/check paths the
TASK legitimately modifies (live: "adjust the test so it checks the new
name" — the specialist edited test_service.py exactly as instructed and
the verify gate's tamper protection killed the delegation as "not
converging"). The declaration lands as verify.unprotect on BOTH the
explicit-verify and the auto_verify path; absent or mis-shaped, the verify
dict is exactly what it was before. Fake ctx.spawn captures the verify
dict the child would run under."""
from __future__ import annotations

import asyncio

from runtime.tool_base import ToolContext
from tests.test_loop_regressions import CFG
from tools.specialist.delegate import SpecialistDelegate


def _ctx(tmp_path, captured):
    async def spawn(task, **kw):
        captured.update(kw)
        captured["task"] = task
        return {"status": "ok", "answer": "done", "run_id": "c",
                "budget": {}, "verified": None}
    # sandbox_prefix: [] → run bare bash, no firejail needed in tests (the
    # explicit operator opt-in, same as tests/test_verify_env.py).
    cfg = dict(CFG, tools={"code": {"delegate": {"model": "coder-alias"},
                                    "run": {"sandbox_prefix": []}}})
    return ToolContext(request_id="t", config=cfg, budget=None,
                       work_root=str(tmp_path), spawn=spawn)


def _delegate(ctx, **args):
    return asyncio.run(SpecialistDelegate().execute(
        {"task": "rename the setting and adjust the test", "fresh": True,
         **args}, ctx))


def test_explicit_verify_string_gains_unprotect(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x",
                    allow_test_edits=["test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] == {"command": "pytest -q -x",
                                  "unprotect": ["test_service.py"]}


def test_explicit_verify_dict_gains_unprotect(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    verify={"command": "pytest -q", "max_checks": 2},
                    allow_test_edits=["test_service.py", "tests/test_api.py"])
    assert res.status == "ok"
    assert captured["verify"]["unprotect"] == ["test_service.py",
                                               "tests/test_api.py"]
    assert captured["verify"]["max_checks"] == 2        # rest untouched


def test_auto_verify_gains_unprotect(tmp_path):
    """No explicit verify: the workspace's auto-detected suite is attached
    (auto_verify) and the declaration rides on that too."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    allow_test_edits=["tests/test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] is not None               # suite auto-attached
    assert captured["verify"]["unprotect"] == ["tests/test_service.py"]


def test_absent_allow_test_edits_no_unprotect_key(tmp_path):
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x")
    assert res.status == "ok"
    assert captured["verify"] == "pytest -q -x"         # passes through raw


def test_misshaped_allow_test_edits_ignored(tmp_path):
    """A non-list value must never widen the exception — ignored entirely."""
    captured = {}
    res = _delegate(_ctx(tmp_path, captured), verify="pytest -q -x",
                    allow_test_edits="test_service.py")
    assert res.status == "ok"
    assert captured["verify"] == "pytest -q -x"


def test_allow_test_edits_without_verify_is_inert(tmp_path):
    """No verify at all (authored-check path): there is no tamper baseline
    to exempt from, so the declaration changes nothing."""
    captured = {}
    res = _delegate(_ctx(tmp_path, captured),
                    allow_test_edits=["test_service.py"])
    assert res.status == "ok"
    assert captured["verify"] is None


# ---- the undeclared-edit death must teach the remedy ------------------------
# (j-space-loop live validation 2026-10-05: child unverified on an
# INSTRUCTED test edit; the brain got the opaque "verifier stuck" line and
# retried blindly into the stall hard-stop.)

def _ctx_child_tampered(tmp_path):
    """Fake ctx whose spawn returns the exact shape a tamper-killed child
    run produces (loop.py: status unverified + error + verify_tampered)."""
    captured = {}

    async def spawn(task, **kw):
        captured.update(kw)
        return {"status": "unverified",
                "error": "verifier stuck on the same failure 2× (not "
                         "converging): cd /x && python -m pytest "
                         "test_service.py -v; protected test/check files "
                         "changed undeclared: test_service.py",
                "verify_tampered": ["test_service.py"],
                "answer": "Done. Rename complete, test passes\n\n"
                          "[NOT VERIFIED — stuck]VERIFIER TAMPERING — the "
                          "protected test/check files changed: "
                          "test_service.py",
                "verified": False, "verify_command": "pytest -q",
                "files_changed": ["config.py", "service.py",
                                  "test_service.py"],
                "run_id": "c", "budget": {}}
    cfg = dict(CFG, tools={"code": {"delegate": {"model": "coder-alias"},
                                    "run": {"sandbox_prefix": []}}})
    return ToolContext(request_id="t", config=cfg, budget=None,
                       work_root=str(tmp_path), spawn=spawn)


def test_undeclared_tamper_death_returns_the_remedy(tmp_path):
    """The error the CALLING BRAIN gets names the changed protected file,
    states the allow_test_edits remedy explicitly, and keeps both readings
    honest (possible test-weakening vs. legitimate task edit)."""
    res = asyncio.run(SpecialistDelegate().execute(
        {"task": "rename and adjust the test", "verify": "pytest -q",
         "fresh": True}, _ctx_child_tampered(tmp_path)))
    assert res.status == "error"
    assert "test_service.py" in res.error
    assert "allow_test_edits: ['test_service.py']" in res.error
    hint = res.result["tamper_hint"]
    assert "test_service.py" in hint
    assert "allow_test_edits: ['test_service.py']" in hint
    assert "test-weakening" in hint          # the suspicious reading stays
    assert "legitimately requires" in hint   # …alongside the legitimate one


def test_arg_description_states_the_consequence():
    """FIX 2 (loose pin): the description leads with the consequence of NOT
    declaring — tamper death, work discarded — so a skimming model sees it."""
    desc = SpecialistDelegate.parameters["properties"]["allow_test_edits"][
        "description"]
    assert "TAMPERING" in desc and "unverified" in desc
    assert "Never use it to make a failing check pass" in desc
