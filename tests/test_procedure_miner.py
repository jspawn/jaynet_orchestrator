"""Procedure miner (runtime/procedure_miner.py) + the draft-skill plumbing:
validate_draft guards, draft:true filtering in the model-facing skill
discovery, jaypack shape annotation, and mine_from_case's contrast
requirement — all hermetic (tmp dirs, fake judge, no model calls)."""
from __future__ import annotations

import asyncio

from runtime import jaypack, procedure_miner
from runtime.skills import (
    discover_skills,
    discover_skills_layered_cached,
    render_catalog,
    skills_cache_clear,
)

GOOD_MD = """\
---
name: batch-aggregation
shape: aggregation
description: >
  Aggregate exact counts across many files without reading them whole.
checkpoints:
  - Contract note written
  - One batched pipeline per question
  - Totals cross-checked
---
Ask one batched pipeline per question, then verify the totals.
"""


# ---- validate_draft ----------------------------------------------------------

def test_validate_draft_good_injects_draft_flag():
    r = procedure_miner.validate_draft(GOOD_MD)
    assert r["ok"] and r["name"] == "batch-aggregation"
    assert r["shape"] == "aggregation"
    # never auto-live: the saved text must carry draft: true
    assert "draft: true" in r["draft"]


def test_validate_draft_strips_code_fence():
    r = procedure_miner.validate_draft("```markdown\n" + GOOD_MD + "\n```")
    assert r["ok"] and r["name"] == "batch-aggregation"


def test_validate_draft_rejects_missing_frontmatter_and_shape():
    assert not procedure_miner.validate_draft("just prose")["ok"]
    no_shape = GOOD_MD.replace("shape: aggregation\n", "")
    r = procedure_miner.validate_draft(no_shape)
    assert not r["ok"] and any("shape" in e for e in r["errors"])


def test_validate_draft_rejects_bad_checkpoints_and_name():
    bad_cps = GOOD_MD.replace("  - Contract note written\n", "") \
                     .replace("  - Totals cross-checked\n", "")
    r = procedure_miner.validate_draft(bad_cps)
    assert not r["ok"] and any("checkpoints" in e for e in r["errors"])
    bad_name = GOOD_MD.replace("name: batch-aggregation", "name: Bad Name!")
    r = procedure_miner.validate_draft(bad_name)
    assert not r["ok"] and any("name" in e for e in r["errors"])


# ---- draft:true skills are invisible to the model ----------------------------

def _write_skill(root, name, extra_fm=""):
    sd = root / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n{extra_fm}---\n"
        "do the thing\n")


def test_draft_skills_filtered_from_model_facing_views(tmp_path):
    builtin = tmp_path / "builtin"
    custom = tmp_path / "custom"
    _write_skill(builtin, "live-skill")
    _write_skill(custom, "draft-skill", extra_fm="draft: true\n")

    # discovery keeps the flag (Studio lists drafts via the uncached path)
    skills = discover_skills(custom)
    assert skills["draft-skill"]["draft"] is True

    # the model-facing cached layered view drops drafts entirely
    skills_cache_clear()
    layered = discover_skills_layered_cached(builtin, custom)
    assert "live-skill" in layered and "draft-skill" not in layered

    # and the prompt catalog never renders one (loop init uses the uncached
    # layered discovery, so the filter must live in render_catalog too)
    catalog = render_catalog({**discover_skills(builtin), **skills})
    assert "live-skill" in catalog and "draft-skill" not in catalog
    skills_cache_clear()


# ---- jaypack: shape annotation ------------------------------------------------

def test_jaypack_skill_with_shape_annotates_manifest(tmp_path):
    r = jaypack.Roots(
        skills_builtin=tmp_path / "skills-builtin",
        skills_custom=tmp_path / "custom" / "skills",
        chains_builtin=tmp_path / "cb", chains_custom=tmp_path / "cc",
        conn_custom=tmp_path / "conn", tools_custom=tmp_path / "tools",
        evals_builtin=tmp_path / "eb", evals_custom=tmp_path / "ec",
        plugins_builtin=tmp_path / "pb", plugins_installed=tmp_path / "pi")
    for d in (r.skills_builtin, r.skills_custom):
        d.mkdir(parents=True)
    sd = r.skills_custom / "batch-aggregation"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(GOOD_MD)
    pack = jaypack.build_pack("skill", "batch-aggregation", roots=r)
    manifest = jaypack.inspect_pack(pack)
    assert manifest["kind"] == "skill"
    assert manifest["shape"] == "aggregation"   # the procedure trust signal


def test_jaypack_plain_skill_has_no_shape(tmp_path):
    r = jaypack.Roots(
        skills_builtin=tmp_path / "sb", skills_custom=tmp_path / "sc",
        chains_builtin=tmp_path / "cb", chains_custom=tmp_path / "cc",
        conn_custom=tmp_path / "conn", tools_custom=tmp_path / "tools",
        evals_builtin=tmp_path / "eb", evals_custom=tmp_path / "ec",
        plugins_builtin=tmp_path / "pb", plugins_installed=tmp_path / "pi")
    r.skills_custom.mkdir(parents=True)
    sd = r.skills_custom / "plain"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(
        "---\nname: plain\ndescription: no shape here\n---\nbody\n")
    manifest = jaypack.inspect_pack(
        jaypack.build_pack("skill", "plain", roots=r))
    assert "shape" not in manifest


# ---- mine_from_case ------------------------------------------------------------

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def results(self, test_id=None, limit=50):
        return self._rows


def _row(passed, score=7.0):
    return {"test_id": "c1", "passed": passed, "score": score,
            "judge_notes": "notes", "transcript": [
                {"user": "do x", "status": "ok", "answer": "done",
                 "trajectory": "code.run(ok)", "tools": ["code.run"],
                 "budget": {}}]}


def test_mine_from_case_requires_pass_fail_contrast(monkeypatch):
    async def fake_judge(config, system, user):
        raise AssertionError("judge must not be called without contrast")
    monkeypatch.setattr(procedure_miner, "_judge", fake_judge)
    out = asyncio.run(procedure_miner.mine_from_case(
        {}, _FakeStore([_row(True), _row(True)]), "c1"))
    assert out["status"] == "error" and "PASS and one FAIL" in out["error"]


def test_mine_from_case_ok(monkeypatch):
    seen = {}

    async def fake_judge(config, system, user):
        seen["user"] = user
        return {"status": "ok", "content": GOOD_MD, "model": "fake-judge",
                "cost_usd": 0.001}
    monkeypatch.setattr(procedure_miner, "_judge", fake_judge)
    rows = [_row(True), _row(False, 3.0)]
    out = asyncio.run(procedure_miner.mine_from_case(
        {}, _FakeStore(rows), "c1"))
    assert out["status"] == "ok" and out["ok"]
    assert out["name"] == "batch-aggregation"
    assert out["judge_model"] == "fake-judge"
    assert "draft: true" in out["draft"]
    # both sides of the contrast reached the judge
    assert "PASSED" in seen["user"] and "FAILED" in seen["user"]


def test_mine_from_flag_requires_runs(monkeypatch):
    out = asyncio.run(procedure_miner.mine_from_flag(
        {}, {"id": "f1", "note": "broke"}, []))
    assert out["status"] == "error" and "no inspectable runs" in out["error"]
