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
