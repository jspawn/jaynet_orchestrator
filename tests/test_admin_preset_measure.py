"""Admin → Models → Presets: the per-row measure button fires model.measure
as a background job (POST .../measure, GET .../measure-status).

execute() is stubbed — the tool itself is covered in test_model_measure.py;
here we pin the route contract: 404 unknown, 400 archived/remote, 409 while
one measurement runs (it hibernates the whole box), done carries the result.
"""
from __future__ import annotations

import asyncio

import pytest

import web.routes_admin as RA
from runtime.tool_base import ToolResult


@pytest.mark.asyncio
async def test_preset_measure_button(web_app, web_client, monkeypatch):
    RA._MEASURE_JOB.update(name=None, state="idle", result=None, error=None)
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_execute(self, args, ctx):
        assert ctx.is_admin and args["preset"]
        started.set()
        await release.wait()
        return ToolResult(status="ok", tool_name="model.measure",
                          result={"measured": {"total_vram_gib": 12.4,
                                               "ram_gib": 6.1}})
    monkeypatch.setattr("tools.model.measure.ModelMeasure.execute",
                        fake_execute)

    app = web_app()
    async with web_client(app) as c:
        r = await c.post("/api/admin/presets/nope/measure")
        assert r.status_code == 404

        brain = app.state.runtime.config["models"]["presets"]["brain"]
        brain["archived"] = True
        assert (await c.post("/api/admin/presets/brain/measure")
                ).status_code == 400
        brain.pop("archived")
        brain["remote_host"] = "192.168.1.5"
        assert (await c.post("/api/admin/presets/brain/measure")
                ).status_code == 400
        brain.pop("remote_host")

        r = await c.post("/api/admin/presets/brain/measure")
        assert r.status_code == 200 and r.json()["measuring"] == "brain"
        await asyncio.wait_for(started.wait(), 5)
        assert (await c.post("/api/admin/presets/brain/measure")
                ).status_code == 409     # one at a time — box-wide hibernation
        release.set()

        for _ in range(100):
            s = (await c.get("/api/admin/presets/measure-status")).json()
            if s["state"] != "running":
                break
            await asyncio.sleep(0.05)
        assert s["state"] == "done" and s["name"] == "brain"
        assert s["result"]["measured"]["total_vram_gib"] == 12.4
