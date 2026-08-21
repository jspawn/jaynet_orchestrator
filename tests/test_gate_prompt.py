"""Gate prompt layering (runtime/gate_prompt.py + admin prompt routes):
shipped default stays pristine, live edits go to the $ORCH_DATA overlay,
revert deletes it. Hermetic via tmp-rooted paths (conftest web_app fixture).
"""
from __future__ import annotations

import pytest

from runtime import gate_prompt, paths

_CFG = {"orchestrator": {"system_prompt": "prompts/orchestrator-gate.md"}}


@pytest.fixture
def lay(tmp_path, monkeypatch):
    """(shipped_file, overlay_file) with all roots tmp-bound."""
    home = tmp_path / "home"
    shipped = home / "prompts" / "orchestrator-gate.md"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("SHIPPED PROMPT", encoding="utf-8")
    overlay = tmp_path / "custom" / "orchestrator-gate.md"
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    return shipped, overlay, home / "config" / "runtime.yaml"


def test_load_shipped_when_no_overlay(lay):
    shipped, overlay, cfg_path = lay
    content, layer = gate_prompt.load(_CFG, cfg_path)
    assert (content, layer) == ("SHIPPED PROMPT", "shipped")


def test_overlay_wins_and_revert(lay):
    shipped, overlay, cfg_path = lay
    gate_prompt.save_overlay(_CFG, "LIVE EDIT")
    assert overlay.read_text() == "LIVE EDIT"
    content, layer = gate_prompt.load(_CFG, cfg_path)
    assert (content, layer) == ("LIVE EDIT", "custom")
    assert shipped.read_text() == "SHIPPED PROMPT"   # untouched
    assert gate_prompt.revert(_CFG) is True
    assert gate_prompt.load(_CFG, cfg_path)[0] == "SHIPPED PROMPT"
    assert gate_prompt.revert(_CFG) is False         # idempotent


# ---- routes -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_routes_use_overlay(web_app, web_client, tmp_path,
                                         monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app = web_app()
    overlay = gate_prompt.overlay_path(app.state.runtime.config)
    async with web_client(app) as c:
        r = await c.get("/api/admin/prompt")
        assert r.status_code == 200 and r.json()["layer"] == "shipped"

        r = await c.put("/api/admin/prompt", json={"content": "EDITED"})
        assert r.status_code == 200 and r.json()["layer"] == "custom"
        assert overlay.read_text() == "EDITED"
        assert app.state.runtime.system_prompt == "EDITED"
        assert (await c.get("/api/admin/prompt")).json()["layer"] == "custom"

        r = await c.delete("/api/admin/prompt")
        assert r.status_code == 200 and r.json()["layer"] == "shipped"
        assert not overlay.exists()
        assert app.state.runtime.system_prompt != "EDITED"
        assert (await c.delete("/api/admin/prompt")).status_code == 404


# ---- eval-tweak consolidation -------------------------------------------------

_MARKER_TEXT = ("BASE PROMPT PROSE — the operating rules live here, long "
                "enough to pass the draft sanity check.\n\n"
                "<!-- eval-proposals -->\n"
                "- 2026-08-20 [t1] always cite the graph before grepping\n"
                "- 2026-08-21 [t2] never guess dates, call datetime.now\n")


async def _fake_model(cfg, alias, messages, **kw):
    return {"status": "ok", "model_name": "fake-judge", "cost_usd": 0.0,
            "tokens": 1, "error": None,
            "content": "CONSOLIDATED PROMPT — base prose with graph-citing "
                       "and datetime discipline folded in. " + "x" * 200}


@pytest.mark.asyncio
async def test_prompt_consolidate_flow(web_app, web_client, tmp_path,
                                       monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    from runtime import eval_runner
    monkeypatch.setattr(eval_runner, "_model_text", _fake_model)
    app = web_app()
    rt = app.state.runtime
    rt.system_prompt = _MARKER_TEXT
    async with web_client(app) as c:
        r = await c.get("/api/admin/prompt")
        assert r.json()["tweak_bullets"] == 2

        # Draft: the model folds the bullets; nothing saved yet.
        r = await c.post("/api/admin/prompt/consolidate")
        assert r.status_code == 200
        d = r.json()
        assert d["bullets"] == 2 and d["model"] == "fake-judge"
        assert "eval-proposals" not in d["draft"]
        assert rt.system_prompt == _MARKER_TEXT

        # Apply: timestamped backup, overlay saved, runtime updated live.
        r = await c.post("/api/admin/prompt/consolidate/apply",
                         json={"content": d["draft"]})
        assert r.status_code == 200
        backup = Path(r.json()["backup"])
        assert backup.read_text() == _MARKER_TEXT
        assert rt.system_prompt == d["draft"]
        assert gate_prompt.overlay_path(rt.config).read_text() == d["draft"]

        # No bullets left -> nothing to consolidate.
        r = await c.post("/api/admin/prompt/consolidate")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_prompt_consolidate_rejects_bad_draft(web_app, web_client,
                                                    tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    from runtime import eval_runner

    async def bad_model(cfg, alias, messages, **kw):
        return {"status": "ok", "model_name": "m", "cost_usd": 0.0,
                "tokens": 1, "error": None, "content": "short"}
    monkeypatch.setattr(eval_runner, "_model_text", bad_model)
    app = web_app()
    app.state.runtime.system_prompt = _MARKER_TEXT
    async with web_client(app) as c:
        r = await c.post("/api/admin/prompt/consolidate")
        assert r.status_code == 502
        # Marker still there, prompt untouched.
        assert app.state.runtime.system_prompt == _MARKER_TEXT
