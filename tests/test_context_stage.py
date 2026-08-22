"""context.stage: oversized text -> workspace file, confined + idempotent."""
import asyncio

from runtime.tool_base import ToolContext
from tools.agent.stage import ContextStage


def _ctx(tmp_path, **kw):
    return ToolContext(request_id="t", budget=None, config={},
                       work_root=str(tmp_path), **kw)


def test_stages_text_and_returns_path(tmp_path):
    r = asyncio.run(ContextStage().execute(
        {"text": "hello world", "name": "My Dump!"}, _ctx(tmp_path)))
    assert r.status == "ok"
    p = r.result["path"]
    assert p.startswith(str(tmp_path)) and "/staged/" in p
    assert p.endswith(".txt") and "My-Dump" in p
    assert r.result["chars"] == 11
    assert "don't read it" in r.result["note"]
    assert open(p).read() == "hello world"


def test_same_content_is_idempotent(tmp_path):
    a = asyncio.run(ContextStage().execute({"text": "same"}, _ctx(tmp_path)))
    b = asyncio.run(ContextStage().execute({"text": "same"}, _ctx(tmp_path)))
    assert a.result["path"] == b.result["path"]
    # different content -> different file (content-hashed name)
    c = asyncio.run(ContextStage().execute({"text": "other"}, _ctx(tmp_path)))
    assert c.result["path"] != a.result["path"]
    assert len(list((tmp_path / "staged").iterdir())) == 2


def test_empty_and_oversize_rejected(tmp_path):
    t = ContextStage()
    assert asyncio.run(t.execute({"text": "  "}, _ctx(tmp_path))).status == "error"
    big = asyncio.run(t.execute({"text": "x" * 5_000_001}, _ctx(tmp_path)))
    assert big.status == "error" and "too large" in big.error


def test_no_workspace_errors():
    ctx = ToolContext(request_id="t", budget=None, config={"tools": {"fs": {}}})
    r = asyncio.run(ContextStage().execute({"text": "x"}, ctx))
    assert r.status == "error" and "workspace" in r.error
