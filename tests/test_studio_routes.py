"""Studio admin API (web/routes_studio.py): CRUD over the custom layer,
validation, AI drafting (mocked at the litellm helper boundary) and
.jaypack export/import.

Hermetic: the runtime.paths.CUSTOM_* dirs and the runtime's skills/chains dirs
are pointed at tmp_path — nothing touches the real ORCH_DATA or install tree.
Uses the conftest web_app/web_client fixtures (admin/pw session).
"""
from __future__ import annotations

import httpx
import pytest

from runtime import paths
from runtime.tool_base import ToolResult

SKILL_MD = """\
---
name: my-skill
description: a test skill
---

Do the thing, then check it is done.
"""

CHAIN_YAML = """\
description: demo chain
steps:
  - id: one
    prompt: "say hi about {{input}}"
"""

CONNECTOR_YAML = """\
name: custom.meteo
description: current weather
base_url: https://api.example.ch
request: {method: GET, path: /v1/forecast}
params:
  lat: {type: number, required: true}
"""

TOOL_SRC = '''\
from typing import Any

from runtime.tool_base import Tool, ToolContext, ToolResult


class HelloTool(Tool):
    name = "custom.hello"
    description = "say hi"
    read_only = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(status="ok", result="hi")
'''


@pytest.fixture
def studio(web_app, tmp_path, monkeypatch):
    """(app, custom_dir, builtin_skills, builtin_chains) — all tmp-rooted."""
    app = web_app()
    custom = tmp_path / "custom"
    monkeypatch.setattr(paths, "CUSTOM_DIR", custom)
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", custom / "skills")
    monkeypatch.setattr(paths, "CUSTOM_CHAINS_DIR", custom / "chains")
    monkeypatch.setattr(paths, "CUSTOM_TOOLS_DIR", custom / "tools")
    monkeypatch.setattr(paths, "CUSTOM_CONN_DIR", custom / "connectors")
    builtin_skills = tmp_path / "builtin-skills"
    builtin_chains = tmp_path / "builtin-chains"
    builtin_skills.mkdir()
    builtin_chains.mkdir()
    cfg = app.state.runtime.config
    cfg.setdefault("skills", {})["dir"] = str(builtin_skills)
    cfg.setdefault("chains", {})["dir"] = str(builtin_chains)
    return app, custom, builtin_skills, builtin_chains


# ---- auth ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requires_auth(studio):
    app, *_ = studio
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/admin/studio")).status_code == 401


# ---- inventory ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_inventory_shape(studio, web_client):
    app, custom, builtin_skills, _ = studio
    (builtin_skills / "base-skill").mkdir()
    (builtin_skills / "base-skill" / "SKILL.md").write_text(
        "---\nname: base-skill\ndescription: builtin one\n---\n\nbody\n")
    async with web_client(app) as c:
        for kind, name, content in [
                ("skill", "my-skill", SKILL_MD), ("chain", "demo", CHAIN_YAML),
                ("connector", "meteo", CONNECTOR_YAML),
                ("tool", "hello", TOOL_SRC)]:
            r = await c.put(f"/api/admin/studio/{kind}/{name}",
                            json={"content": content})
            assert r.status_code == 200, r.text
        r = await c.get("/api/admin/studio")
        assert r.status_code == 200
        inv = r.json()
        assert set(inv) == {"skills", "chains", "connectors", "tools"}
        skills = {s["name"]: s for s in inv["skills"]}
        assert skills["my-skill"]["origin"] == "custom"
        assert skills["base-skill"]["origin"] == "builtin"
        assert skills["my-skill"]["description"] == "a test skill"
        chains = {ch["name"]: ch for ch in inv["chains"]}
        assert chains["demo"] == {"name": "demo", "origin": "custom",
                                  "description": "demo chain", "steps": 1}
        assert inv["connectors"] == [{"name": "meteo", "origin": "custom",
                                      "description": "current weather"}]
        assert inv["tools"] == [{"name": "hello", "path": "hello.py",
                                 "origin": "custom"}]


# ---- PUT + GET roundtrip per kind ---------------------------------------------

@pytest.mark.asyncio
async def test_put_get_roundtrip_skill(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/skill/my-skill",
                        json={"content": SKILL_MD})
        assert r.json() == {"ok": True, "needs_restart": False}
        assert (custom / "skills" / "my-skill" / "SKILL.md").read_text() == SKILL_MD
        r = await c.get("/api/admin/studio/skill/my-skill")
        body = r.json()
        assert body["content"] == SKILL_MD
        assert body["origin"] == "custom"
        assert body["description"] == "a test skill"
        assert body["resources"] == []


@pytest.mark.asyncio
async def test_put_get_roundtrip_chain(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/chain/demo", json={"content": CHAIN_YAML})
        assert r.json() == {"ok": True, "needs_restart": False}
        assert (custom / "chains" / "demo.yaml").read_text() == CHAIN_YAML
        body = (await c.get("/api/admin/studio/chain/demo")).json()
        assert body["content"] == CHAIN_YAML and body["origin"] == "custom"


@pytest.mark.asyncio
async def test_put_get_roundtrip_connector(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/connector/meteo",
                        json={"content": CONNECTOR_YAML})
        assert r.json() == {"ok": True, "needs_restart": True}
        assert (custom / "connectors" / "meteo.yaml").read_text() == CONNECTOR_YAML
        body = (await c.get("/api/admin/studio/connector/meteo")).json()
        assert body["content"] == CONNECTOR_YAML


@pytest.mark.asyncio
async def test_put_get_roundtrip_tool(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/tool/hello", json={"content": TOOL_SRC})
        assert r.json() == {"ok": True, "needs_restart": True}
        assert (custom / "tools" / "hello.py").read_text() == TOOL_SRC
        body = (await c.get("/api/admin/studio/tool/hello")).json()
        assert body["content"] == TOOL_SRC and body["path"] == "hello.py"


@pytest.mark.asyncio
async def test_get_missing_is_404(studio, web_client):
    app, *_ = studio
    async with web_client(app) as c:
        for kind in ("skill", "chain", "connector", "tool"):
            r = await c.get(f"/api/admin/studio/{kind}/nope")
            assert r.status_code == 404, kind
        assert (await c.get("/api/admin/studio/widget/x")).status_code == 400
        assert (await c.get("/api/admin/studio/skill/../etc")).status_code in (400, 404, 422)


# ---- PUT validation failures ----------------------------------------------------

@pytest.mark.asyncio
async def test_put_rejects_bad_chain(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/chain/bad",
                        json={"content": "steps: []"})
        assert r.status_code == 400 and "steps" in r.json()["detail"]
        r = await c.put("/api/admin/studio/chain/bad",
                        json={"content": "steps: [unclosed"})
        assert r.status_code == 400
        assert not (custom / "chains" / "bad.yaml").exists()


@pytest.mark.asyncio
async def test_put_rejects_bad_frontmatter(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/skill/bad",
                        json={"content": "no frontmatter here"})
        assert r.status_code == 400 and "frontmatter" in r.json()["detail"]
        r = await c.put("/api/admin/studio/skill/bad",
                        json={"content": "---\nname: bad\n---\n\nbody\n"})
        assert r.status_code == 400 and "description" in r.json()["detail"]
        r = await c.put("/api/admin/studio/skill/bad",
                        json={"content": "---\nname: other\ndescription: x\n---\n"})
        assert r.status_code == 400 and "does not match" in r.json()["detail"]
        assert not (custom / "skills" / "bad").exists()


@pytest.mark.asyncio
async def test_put_rejects_bad_connector(studio, web_client):
    app, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/connector/bad",
                        json={"content": "name: nodot\nbase_url: https://x.ch\n"
                                         "request: {method: GET, path: /x}\n"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_rejects_bad_tool(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        r = await c.put("/api/admin/studio/tool/bad",
                        json={"content": "def broken(:\n"})
        assert r.status_code == 400 and "syntax error" in r.json()["detail"]
        r = await c.put("/api/admin/studio/tool/bad",
                        json={"content": "x = 1\n"})
        assert r.status_code == 400 and "Tool subclass" in r.json()["detail"]
        # Name colliding with a registered builtin is refused for NEW files.
        # (The test harness's registry is empty — its tools root is a tmp dir —
        # so seed one "builtin" to collide with.)
        from runtime.tool_base import Tool

        class _FakeBuiltin(Tool):
            name = "fs.read"
            description = "stand-in builtin"

            async def execute(self, args, context):  # pragma: no cover
                raise NotImplementedError

        app.state.runtime.registry.register_instance(_FakeBuiltin())
        src = TOOL_SRC.replace('name = "custom.hello"', 'name = "fs.read"')
        r = await c.put("/api/admin/studio/tool/sneaky", json={"content": src})
        assert r.status_code == 400 and "collides" in r.json()["detail"]
        assert not (custom / "tools").exists() or not list(
            (custom / "tools").rglob("*.py"))


# ---- DELETE ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_custom(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        await c.put("/api/admin/studio/skill/my-skill", json={"content": SKILL_MD})
        await c.put("/api/admin/studio/tool/hello", json={"content": TOOL_SRC})
        r = await c.delete("/api/admin/studio/skill/my-skill")
        assert r.json() == {"ok": True, "needs_restart": False}
        assert not (custom / "skills" / "my-skill").exists()
        r = await c.delete("/api/admin/studio/tool/hello")
        assert r.json() == {"ok": True, "needs_restart": True}
        assert not (custom / "tools" / "hello.py").exists()
        assert (await c.delete("/api/admin/studio/skill/my-skill")).status_code == 404


@pytest.mark.asyncio
async def test_delete_refuses_builtin_only(studio, web_client):
    app, _, builtin_skills, builtin_chains = studio
    (builtin_skills / "base-skill").mkdir()
    (builtin_skills / "base-skill" / "SKILL.md").write_text(
        "---\nname: base-skill\ndescription: builtin\n---\n\nbody\n")
    (builtin_chains / "base-chain.yaml").write_text(CHAIN_YAML)
    async with web_client(app) as c:
        r = await c.delete("/api/admin/studio/skill/base-skill")
        assert r.status_code == 403
        r = await c.delete("/api/admin/studio/chain/base-chain")
        assert r.status_code == 403
        # …and the builtin files are untouched.
        assert (builtin_skills / "base-skill" / "SKILL.md").is_file()
        assert (builtin_chains / "base-chain.yaml").is_file()


# ---- validate endpoint ----------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_endpoint(studio, web_client):
    app, *_ = studio
    async with web_client(app) as c:
        r = await c.post("/api/admin/studio/validate", json={
            "kind": "chain", "name": "demo", "content": CHAIN_YAML})
        assert r.json() == {"ok": True, "errors": []}
        r = await c.post("/api/admin/studio/validate", json={
            "kind": "chain", "name": "demo",
            "content": "steps:\n  - id: a\n"})
        body = r.json()
        assert body["ok"] is False and body["errors"]
        r = await c.post("/api/admin/studio/validate", json={
            "kind": "tool", "name": "hello", "content": TOOL_SRC})
        assert r.json() == {"ok": True, "errors": []}
        r = await c.post("/api/admin/studio/validate", json={
            "kind": "skill", "name": "my-skill", "content": SKILL_MD})
        assert r.json()["ok"] is True
        # Validation never writes.
        r = await c.get("/api/admin/studio")
        assert r.json()["chains"] == [] and r.json()["tools"] == []


# ---- draft endpoint (litellm helper mocked) ---------------------------------------

@pytest.mark.asyncio
async def test_draft_local_only_and_fence_strip(studio, web_client, monkeypatch):
    app, *_ = studio
    seen = {}

    async def fake(alias, task, payload, system, want_json, think, ctx):
        seen.update(alias=alias, task=task, system=system)
        return ToolResult(status="ok",
                          result="```yaml\ndescription: drafted\nsteps:\n"
                                 "  - id: a\n    prompt: hi {{input}}\n```\n")

    monkeypatch.setattr("web.routes_studio._call_via_litellm", fake)
    async with web_client(app) as c:
        r = await c.post("/api/admin/studio/draft", json={
            "kind": "chain", "description": "research a topic and sum it up"})
        assert r.status_code == 200
        draft = r.json()["draft"]
        assert seen["alias"] == "local-orchestrator"
        assert not draft.startswith("```") and not draft.endswith("```")
        assert draft.startswith("description: drafted")
        assert "sum it up" in seen["task"]
        assert "Target format" in seen["system"]


@pytest.mark.asyncio
async def test_draft_litellm_failure_is_502(studio, web_client, monkeypatch):
    app, *_ = studio

    async def fake(alias, task, payload, system, want_json, think, ctx):
        return ToolResult(status="error", result=None, error="proxy down")

    monkeypatch.setattr("web.routes_studio._call_via_litellm", fake)
    async with web_client(app) as c:
        r = await c.post("/api/admin/studio/draft", json={
            "kind": "skill", "description": "anything"})
        assert r.status_code == 502
        assert "proxy down" in r.json()["detail"]


# ---- export / import ------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_import_roundtrip(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        await c.put("/api/admin/studio/chain/packme", json={"content": CHAIN_YAML})
        r = await c.get("/api/admin/studio/export/chain/packme")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "packme.jaypack" in r.headers["content-disposition"]
        pack = r.content
        # Re-import over the existing artifact → 409 with the manifest.
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["manifest"]["kind"] == "chain"
        assert detail["manifest"]["name"] == "packme"
        # Confirm overwrite → ok.
        r = await c.post("/api/admin/studio/import?overwrite=true",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "installed": "packme", "kind": "chain",
                            "name": "packme", "needs_restart": False}
        # Delete, then a fresh import restores it byte-for-byte.
        await c.delete("/api/admin/studio/chain/packme")
        assert not (custom / "chains" / "packme.yaml").exists()
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("packme.jaypack", pack,
                                         "application/zip")})
        assert r.status_code == 200
        assert (custom / "chains" / "packme.yaml").read_text() == CHAIN_YAML


@pytest.mark.asyncio
async def test_export_import_skill_pack(studio, web_client):
    app, custom, *_ = studio
    async with web_client(app) as c:
        await c.put("/api/admin/studio/skill/my-skill", json={"content": SKILL_MD})
        r = await c.get("/api/admin/studio/export/skill/my-skill")
        assert r.status_code == 200
        await c.delete("/api/admin/studio/skill/my-skill")
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("my-skill.jaypack", r.content,
                                         "application/zip")})
        assert r.status_code == 200 and r.json()["kind"] == "skill"
        assert (custom / "skills" / "my-skill" / "SKILL.md").read_text() == SKILL_MD


@pytest.mark.asyncio
async def test_export_missing_404_and_bad_pack_400(studio, web_client):
    app, *_ = studio
    async with web_client(app) as c:
        r = await c.get("/api/admin/studio/export/chain/nope")
        assert r.status_code == 404
        r = await c.get("/api/admin/studio/export/widget/x")
        assert r.status_code == 400
        r = await c.post("/api/admin/studio/import",
                         files={"file": ("x.jaypack", b"not a zip",
                                         "application/zip")})
        assert r.status_code == 400
