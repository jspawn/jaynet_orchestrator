"""Procedure auto-selector (agent.procedure_selector): a request matching a
shape-tagged skill's keywords gets the procedure body injected at run start
— small brains rarely skill.load on their own. One load per run, brain-only,
confident matches only. Real loop, fake model."""
import asyncio

from runtime.skills import skills_cache_clear
from tests.test_loop_regressions import _final, _Registry, _runtime

SKILL_MD = """---
name: spec-proc
shape: implement-from-spec
description: test procedure
---
PROCEDURE BODY MARKER — follow the steps.
"""


def _rt(tmp_path, script, skill_body=SKILL_MD):
    d = tmp_path / "skills" / "spec-proc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_body)
    skills_cache_clear()
    rt, seen = _runtime(_Registry(["skill.load"]), script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    return rt, seen


def _injected(seen):
    """Distinct procedure injections (the message persists in the transcript,
    so it shows in every later turn's message list — dedupe by content)."""
    return list({m["content"] for msgs in seen for m in msgs
                 if m.get("role") == "system"
                 and isinstance(m.get("content"), str)
                 and "Procedure auto-loaded" in m["content"]})


def test_matching_request_autoloads_procedure(tmp_path):
    rt, seen = _rt(tmp_path, [_final("done"), _final("wrote it")])
    out = asyncio.run(rt.run(
        "Implement the algorithm from the paper and write /app/result.txt"))
    assert out["status"] == "ok"
    inj = _injected(seen)
    assert len(inj) == 1
    assert "spec-proc" in inj[0]
    assert "PROCEDURE BODY MARKER" in inj[0]


def test_unrelated_request_loads_nothing(tmp_path):
    rt, seen = _rt(tmp_path, [_final("done"), _final("wrote it")])
    out = asyncio.run(rt.run("what is the capital of France?"))
    assert out["status"] == "ok"
    assert _injected(seen) == []


def test_disabled_loads_nothing(tmp_path):
    rt, seen = _rt(tmp_path, [_final("done"), _final("wrote it")])
    rt.config["agent"] = {"procedure_selector": {"enabled": False}}
    out = asyncio.run(rt.run(
        "Implement the algorithm from the paper and write /app/result.txt"))
    assert out["status"] == "ok"
    assert _injected(seen) == []


def test_skill_without_shape_tag_is_never_autoloaded(tmp_path):
    no_shape = SKILL_MD.replace("shape: implement-from-spec\n", "")
    rt, seen = _rt(tmp_path, [_final("done"), _final("wrote it")], skill_body=no_shape)
    out = asyncio.run(rt.run(
        "Implement the algorithm from the paper and write /app/result.txt"))
    assert out["status"] == "ok"
    assert _injected(seen) == []


# ---- the shipped procedure shapes (keywords in _DEFAULT_PROCEDURE_SHAPES) ----

def _write_skill(tmp_path, name, shape):
    d = tmp_path / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nshape: {shape}\ndescription: t\n---\n"
        f"BODY OF {name}\n")


def _rt_shapes(tmp_path, script):
    _write_skill(tmp_path, "debug-and-fix", "debug-and-fix")
    _write_skill(tmp_path, "research-and-verify", "research-and-verify")
    skills_cache_clear()
    rt, seen = _runtime(_Registry(["skill.load"]), script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    return rt, seen


def test_debug_request_loads_debug_procedure(tmp_path):
    rt, seen = _rt_shapes(tmp_path, [_final("done"), _final("fixed")])
    out = asyncio.run(rt.run("The tests fail after my change — debug the "
                             "suite and fix the bug in the parser"))
    assert out["status"] == "ok"
    inj = _injected(seen)
    assert len(inj) == 1 and "debug-and-fix" in inj[0]


def test_lookup_request_loads_research_procedure(tmp_path):
    rt, seen = _rt_shapes(tmp_path, [_final("done"), _final("found")])
    out = asyncio.run(rt.run("Find the official codebase of the paper and "
                             "write /app/result.jsonl"))
    assert out["status"] == "ok"
    inj = _injected(seen)
    assert len(inj) == 1 and "research-and-verify" in inj[0]


def test_generic_request_loads_neither_new_shape(tmp_path):
    rt, seen = _rt_shapes(tmp_path, [_final("done"), _final("ok")])
    out = asyncio.run(rt.run("summarize this text for me"))
    assert out["status"] == "ok"
    assert _injected(seen) == []


# ---- loop-enforced checkpoints (procedure todo step 4) ----

SKILL_MD_CPS = """---
name: spec-proc
shape: implement-from-spec
description: test procedure
checkpoints:
  - CHECKPOINT ALPHA
  - CHECKPOINT BETA
---
PROCEDURE BODY MARKER — follow the steps.
"""


def _proc_check_msgs(seen):
    """Distinct procedure-check injections (the message persists in the
    transcript, so it shows in every later turn's list — dedupe by content)."""
    return list({m["content"] for msgs in seen for m in msgs
                 if m.get("role") == "user"
                 and isinstance(m.get("content"), str)
                 and m["content"].startswith("Procedure check")})


def test_checkpoints_nudged_once_before_final_answer(tmp_path):
    """A final answer with an active procedure earns ONE checkpoint nudge;
    the answer after it is accepted (no loop)."""
    rt, seen = _rt(tmp_path, [_final("answer one"), _final("answer two")],
                   skill_body=SKILL_MD_CPS)
    out = asyncio.run(rt.run("Implement the algorithm from the paper"))
    assert out["status"] == "ok"
    assert out["answer"] == "answer two"
    checks = _proc_check_msgs(seen)
    assert len(checks) == 1
    assert "spec-proc" in checks[0]
    assert "CHECKPOINT ALPHA" in checks[0]
    assert "CHECKPOINT BETA" in checks[0]


def test_no_checkpoints_no_procedure_check(tmp_path):
    """A procedure without frontmatter checkpoints changes nothing at the
    final answer — the generic path applies."""
    rt, seen = _rt(tmp_path, [_final("done")])
    out = asyncio.run(rt.run("Implement the algorithm from the paper"))
    assert out["status"] == "ok"
    assert out["answer"] == "done"
    assert _proc_check_msgs(seen) == []


def test_checkpoints_ride_the_stall_ladder(tmp_path):
    """Stall-ladder rungs carry the active procedure's checklist — the
    concrete version of 'make progress'."""
    import json as _json

    from tests.test_loop_regressions import _tc
    from tools.fs.ops import FsRead
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    d = tmp_path / "skills" / "spec-proc"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD_CPS)
    skills_cache_clear()
    reg = _Registry(["skill.load"], real={"fs.read": FsRead()})
    script = [
        _tc("fs.read", _json.dumps({"path": "a.txt"})),   # stall 1
        _tc("fs.read", _json.dumps({"path": "b.txt"})),   # stall 2 → rung 1
        _final("nearly"),                                  # proc check nudge
        _final("done"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    rt.config["budgets"] = {**rt.config["budgets"], "max_iterations": 40}
    out = asyncio.run(rt.run("Implement the algorithm from the paper",
                             work_root=str(tmp_path)))
    assert out["status"] == "ok"
    rungs = {m["content"] for msgs in seen for m in msgs
             if m.get("role") == "system"
             and isinstance(m.get("content"), str)
             and m["content"].startswith("Progress check")}
    assert len(rungs) == 1
    rung = rungs.pop()
    assert "Active procedure 'spec-proc'" in rung
    assert "CHECKPOINT ALPHA" in rung


# ---- deterministic format, long-document and diagnosis skill gates ----

def _write_gate_skill(tmp_path, name, body, resource=False):
    d = tmp_path / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}\n")
    if resource:
        (d / "helper.py").write_text("print('ok')\n")


def _gate_rt(tmp_path, script):
    skills_cache_clear()
    rt, seen = _runtime(_Registry(["skill.load"]), script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    return rt, seen


def _skill_injected(seen):
    return list({m["content"] for msgs in seen for m in msgs
                 if m.get("role") == "system"
                 and isinstance(m.get("content"), str)
                 and "Skill auto-loaded" in m["content"]})


def test_pdf_attachment_loads_skill_and_absolute_resources(tmp_path):
    _write_gate_skill(tmp_path, "pdf", "PDF BODY\npython <files['helper.py']>",
                      resource=True)
    rt, seen = _gate_rt(tmp_path, [_final("done")])
    out = asyncio.run(rt.run(
        "Read this\n\n[Attached files]\n"
        "- report.PDF (binary, 500 B) saved at /tmp/report.PDF\n"
        "  If a skill applies, load it."))
    assert out["status"] == "ok"
    injected = _skill_injected(seen)
    assert len(injected) == 1
    assert "pdf; trigger: attachment" in injected[0]
    assert "PDF BODY" in injected[0]
    assert str(tmp_path / "skills" / "pdf" / "helper.py") in injected[0]
    assert "<files[" not in injected[0]


def test_large_attached_text_uses_original_size(tmp_path):
    _write_gate_skill(tmp_path, "long-document", "LONG BODY")
    rt, seen = _gate_rt(tmp_path, [_final("done")])
    out = asyncio.run(rt.run(
        "Summarize\n\n[Attached files]\n"
        "- notes.txt (text, 50000 B) saved at /tmp/notes.txt\n"
        "  --- begin notes.txt ---\nshort preview\n  --- end notes.txt ---"))
    assert out["status"] == "ok"
    injected = _skill_injected(seen)
    assert len(injected) == 1 and "long-document" in injected[0]
    assert "document_size" in injected[0]


def test_explicit_diagnosis_suppresses_shorter_debug_procedure(tmp_path):
    rt, seen = _rt_shapes(tmp_path, [_final("done")])
    _write_gate_skill(tmp_path, "diagnosing-bugs", "DEEP DIAGNOSIS")
    skills_cache_clear()
    out = asyncio.run(rt.run("Diagnose this flaky test and debug the parser"))
    assert out["status"] == "ok"
    injected = _skill_injected(seen)
    assert len(injected) == 1 and "DEEP DIAGNOSIS" in injected[0]
    assert _injected(seen) == []


def test_disabled_skill_gate_loads_nothing(tmp_path):
    _write_gate_skill(tmp_path, "pdf", "PDF BODY")
    rt, seen = _gate_rt(tmp_path, [_final("done")])
    rt.config["agent"] = {"skill_autoload": {"enabled": False}}
    out = asyncio.run(rt.run(
        "Read\n\n[Attached files]\n"
        "- report.pdf (binary, 500 B) saved at /tmp/report.pdf"))
    assert out["status"] == "ok"
    assert _skill_injected(seen) == []


def test_per_run_disabled_skill_is_not_autoloaded(tmp_path):
    _write_gate_skill(tmp_path, "pdf", "PDF BODY")
    rt, seen = _gate_rt(tmp_path, [_final("done")])
    rt.skills = {}  # minimal fake runtime has no init-time catalog
    out = asyncio.run(rt.run(
        "Read\n\n[Attached files]\n"
        "- report.pdf (binary, 500 B) saved at /tmp/report.pdf",
        run_overrides={"disabled_skills": ["pdf"]}))
    assert out["status"] == "ok"
    assert _skill_injected(seen) == []


def test_repeated_failure_loads_diagnosis_once(tmp_path):
    import json

    from runtime.tool_base import Tool, ToolResult
    from tests.test_loop_regressions import _tc

    class AlwaysFail(Tool):
        name = "code.check"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            return ToolResult(status="error", error="same assertion failed")

    _write_gate_skill(tmp_path, "diagnosing-bugs", "DEEP DIAGNOSIS")
    skills_cache_clear()
    reg = _Registry(["skill.load"], real={"code.check": AlwaysFail()})
    script = [
        _tc("code.check", json.dumps({"command": "first"})),
        _tc("code.check", json.dumps({"command": "second"})),
        _tc("code.check", json.dumps({"command": "third"})),
        _final("still failing"),
    ]
    rt, seen = _runtime(reg, script)
    rt.config["skills"] = {"dir": str(tmp_path / "skills")}
    out = asyncio.run(rt.run("Fix the parser"))
    assert out["status"] == "ok"
    injected = _skill_injected(seen)
    assert len(injected) == 1 and "DEEP DIAGNOSIS" in injected[0]
    assert "repeated_failure" in injected[0]
