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
