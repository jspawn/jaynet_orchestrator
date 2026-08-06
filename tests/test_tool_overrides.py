"""Tool-description overrides (runtime/tool_overrides.py) + the judge's
structured proposal fields (target / proposed_content) + skill bodies in
the judge state block.
"""
from __future__ import annotations

import pytest

from runtime import eval_runner, paths, tool_overrides
from runtime.eval_cases import EvalCase

from conftest import run


class _Tool:
    def __init__(self, name, description="orig"):
        self.name = name
        self.description = description


class _Registry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def all(self):
        return list(self._tools.values())

    def get(self, name):
        return self._tools.get(name)


@pytest.fixture
def ovfile(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    return tmp_path / "custom" / "tool-overrides.yaml"


def test_roundtrip_and_apply(ovfile):
    assert tool_overrides.load() == {}
    reg = _Registry([_Tool("fs.read"), _Tool("web.search")])
    tool_overrides.save({"fs.read": "better wording", "ghost.tool": "stale"})
    assert tool_overrides.load()["fs.read"] == "better wording"
    applied = tool_overrides.apply(reg)
    assert applied == 1                       # unknown names ignored
    assert reg.get("fs.read").description == "better wording"
    assert reg.get("web.search").description == "orig"
    tool_overrides.apply(reg)                 # idempotent


# ---- judge structured fields ---------------------------------------------------

def _case(**kw):
    base = dict(id="demo", name="Demo", turns=["hi"], judge_rubric="r")
    base.update(kw)
    return EvalCase(**base)


def test_judge_parses_target_and_content(monkeypatch):
    async def j(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": false, "score": 3, "notes": "n",'
                           ' "classification": "tool-description",'
                           ' "target": "web.search",'
                           ' "proposed_content": "Search the web for X.",'
                           ' "what": "w", "cause": "c", "fix": "f"}'}
    monkeypatch.setattr(eval_runner, "_model_text", j)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(), [], []))
    assert out["target"] == "web.search"
    assert out["proposed_content"] == "Search the web for X."


def test_judge_clears_fields_for_other_classes(monkeypatch):
    async def j(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "j", "cost_usd": 0.0,
                "tokens": 1, "error": None,
                "content": '{"pass": false, "score": 3, "notes": "n",'
                           ' "classification": "prompt-tweak",'
                           ' "target": "web.search",'
                           ' "proposed_content": "noise",'
                           ' "what": "w", "cause": "c", "fix": "f"}'}
    monkeypatch.setattr(eval_runner, "_model_text", j)
    out = run(eval_runner._judge({}, eval_runner.config({}), _case(), [], []))
    assert out["target"] == "" and out["proposed_content"] == ""


def test_loaded_skill_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom")
    d = tmp_path / "skills" / "tdd"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: test first\n---\nWRITE TESTS FIRST",
        encoding="utf-8")
    turns = [{"trajectory": "skill.load(tdd)→ok; fs.read(x)→ok"}]
    bodies = eval_runner._loaded_skill_bodies(turns)
    assert bodies == {"tdd": "WRITE TESTS FIRST"}
    assert eval_runner._loaded_skill_bodies([{"trajectory": "fs.read(x)→ok"}]) == {}
