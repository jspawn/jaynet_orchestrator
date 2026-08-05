"""Admin budget defaults: an explicit 0 means "off / unlimited" for every
ceiling and must stick — the coerce step used to drop v <= 0 everywhere, so
saving 0 silently kept the previous value (and the 600s wall clock kept
killing long agent.spawn runs). Enforcement guards all four ceilings with
`if self.max_X and …` (runtime/budget.py), so 0 never means "kill every run".
"""
import json

import pytest


@pytest.mark.asyncio
async def test_wall_clock_zero_sticks(web_app, web_client):
    app = web_app()
    async with web_client(app) as c:
        r = await c.put("/api/admin/budget-defaults",
                        json={"max_wall_clock_s": 600})
        assert r.status_code == 200 and r.json()["max_wall_clock_s"] == 600
        # the bug: saving 0 dropped the key, GET returned the old 600
        r = await c.put("/api/admin/budget-defaults",
                        json={"max_wall_clock_s": 0})
        assert r.status_code == 200
        assert r.json()["max_wall_clock_s"] == 0
        r = await c.get("/api/admin/budget-defaults")
        assert r.json()["max_wall_clock_s"] == 0


@pytest.mark.asyncio
async def test_wall_clock_zero_persists(web_app, web_client, tmp_path):
    app = web_app()
    async with web_client(app) as c:
        await c.put("/api/admin/budget-defaults", json={"max_wall_clock_s": 0})
    # the on-disk store must carry the 0 (boot reload uses allow_zero too)
    store = tmp_path / "budget-defaults.json"
    assert json.loads(store.read_text())["max_wall_clock_s"] == 0


@pytest.mark.asyncio
async def test_zero_means_unlimited_for_all_ceilings(web_app, web_client):
    # Consistency: 0 = "off / unlimited" for every ceiling, not just the wall
    # clock (the admin editor's toggles send 0 for disabled fields).
    app = web_app()
    async with web_client(app) as c:
        r = await c.put("/api/admin/budget-defaults",
                        json={"max_iterations": 0, "max_wall_clock_s": 0,
                              "max_cost_usd": 0, "max_total_tokens": 0})
        assert r.status_code == 200
        r = await c.get("/api/admin/budget-defaults")
        assert r.json() == {"max_iterations": 0, "max_wall_clock_s": 0,
                            "max_cost_usd": 0, "max_total_tokens": 0}
