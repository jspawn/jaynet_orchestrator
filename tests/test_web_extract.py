"""web.extract: builds an isolated extraction sub-agent with the right tools and
a prompt that carries url/describe/schema, and surfaces the saved file + report."""
import asyncio

from tools.web.extract import WebExtract


class _Ctx:
    def __init__(self, status="ok"):
        self.config = {}; self.calls = []; self._status = status
    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None):
        self.calls.append({"name": name, "tools": tools, "task": task})
        return {"status": self._status, "answer": "saved extracted.json — 12 records",
                "files_changed": ["extracted.json"], "run_id": "r1"}


def _run(ctx, args): return asyncio.run(WebExtract().execute(args, ctx))


def test_requires_url_and_describe():
    assert _run(_Ctx(), {"url": "", "describe": "x"}).status == "error"
    assert _run(_Ctx(), {"url": "http://x", "describe": ""}).status == "error"


def test_spawns_extractor_with_web_and_fs_tools():
    ctx = _Ctx()
    r = _run(ctx, {"url": "https://ex.com/pricing", "describe": "pricing tiers"})
    c = ctx.calls[0]
    assert c["name"] == "extractor"
    for t in ["web.fetch", "web.render", "code.execute", "fs.write"]:
        assert t in c["tools"], t
    assert "https://ex.com/pricing" in c["task"] and "pricing tiers" in c["task"]
    assert r.status == "ok" and r.result["saved_to"] == "extracted.json"
    assert r.result["files_changed"] == ["extracted.json"]


def test_schema_and_output_flow_into_prompt():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "jobs", "schema": "[{title, company}]", "output": "jobs.json"})
    task = ctx.calls[0]["task"]
    assert "[{title, company}]" in task and "jobs.json" in task


def test_render_flag_forces_headless():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d", "render": True})
    assert "headless browser" in ctx.calls[0]["task"]
    ctx2 = _Ctx()
    _run(ctx2, {"url": "http://x", "describe": "d"})
    assert "fall back to web.render" in ctx2.calls[0]["task"]   # auto by default


def test_no_invent_guard_in_prompt():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d"})
    assert "do NOT invent" in ctx.calls[0]["task"]


def test_sub_agent_failure_surfaces_error():
    r = _run(_Ctx(status="budget_exceeded"), {"url": "http://x", "describe": "d"})
    assert r.status == "error"
