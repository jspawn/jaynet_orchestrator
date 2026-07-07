"""web.crawl: bounded paginated extraction into one merged file, with the cap
enforced by the sub-agent's iteration budget."""
import asyncio
from tools.web.crawl import WebCrawl


class _Ctx:
    def __init__(self, status="ok"):
        self.config = {}; self.calls = []; self._status = status
    async def spawn(self, task, *, tools=None, model=None, name=None, budget=None,
                    share_private=None, verify=None):
        self.calls.append({"name": name, "tools": tools, "task": task, "budget": budget})
        return {"status": self._status, "answer": "crawled 8 pages, 96 records",
                "files_changed": ["crawled.json"], "run_id": "r1"}


def _run(ctx, args): return asyncio.run(WebCrawl().execute(args, ctx))


def test_requires_url_and_describe():
    assert _run(_Ctx(), {"url": "", "describe": "x"}).status == "error"
    assert _run(_Ctx(), {"url": "http://x", "describe": ""}).status == "error"


def test_budget_caps_pages():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d", "max_pages": 5})
    assert ctx.calls[0]["budget"] == {"max_iterations": 5 * 4 + 8}
    assert ctx.calls[0]["name"] == "crawler"


def test_max_pages_clamped():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d", "max_pages": 9999})
    # clamped to ceiling 100 -> budget 100*4+8
    assert ctx.calls[0]["budget"]["max_iterations"] == 100 * 4 + 8


def test_page_url_template_pagination():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "jobs", "page_url": "http://x/jobs?page={page}",
               "start_page": 2, "max_pages": 3})
    task = ctx.calls[0]["task"]
    assert "http://x/jobs?page={page}" in task and "from 2 to 4" in task


def test_follow_next_default():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d"})
    assert "next" in ctx.calls[0]["task"].lower() and "same site" in ctx.calls[0]["task"].lower()


def test_append_to_file_and_no_invent():
    ctx = _Ctx()
    _run(ctx, {"url": "http://x", "describe": "d", "output": "out.json"})
    t = ctx.calls[0]["task"]
    assert "APPEND to `out.json`" in t and "in the FILE" in t and "NEVER invent" in t


def test_budget_exceeded_is_ok_with_flag():
    r = _run(_Ctx(status="budget_exceeded"), {"url": "http://x", "describe": "d"})
    assert r.status == "ok" and r.result["hit_budget"] is True


def test_real_error_surfaces():
    r = _run(_Ctx(status="error"), {"url": "http://x", "describe": "d"})
    assert r.status == "error"
