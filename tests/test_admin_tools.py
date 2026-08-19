"""Admin → Tools grid API: per-tool descriptions ride along with the
enable/disable flags (the UI renders them under each tool name)."""
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture
def tools_linked(tmp_path):
    """web_app's runtime discovers tools at <config>/../tools — point the
    tmp install root at the real plugin dir."""
    (tmp_path / "tools").symlink_to(TOOLS_DIR)


@pytest.mark.asyncio
async def test_disabled_tools_include_descriptions(web_app, web_client,
                                                   tools_linked):
    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/admin/disabled-tools")
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert tools, "registry should be populated"
        by_name = {t["name"]: t for t in tools}
        # Descriptions are the first line of the registry text (overrides
        # applied); spot-check a shipped tool whose description is stable.
        assert by_name["web.search"]["description"]
        assert "\n" not in by_name["web.search"]["description"]
        assert all(set(t) == {"name", "disabled", "description"}
                   for t in tools)


@pytest.mark.asyncio
async def test_disabled_tools_put_still_roundtrips(web_app, web_client,
                                                   tools_linked):
    app = web_app()
    async with web_client(app) as c:
        r = await c.put("/api/admin/disabled-tools",
                        json={"disabled": ["web.fetch"]})
        assert r.status_code == 200
        r = await c.get("/api/admin/disabled-tools")
        by_name = {t["name"]: t for t in r.json()["tools"]}
        assert by_name["web.fetch"]["disabled"] is True
        assert by_name["web.search"]["disabled"] is False
