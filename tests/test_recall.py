"""Scoped local recall: intent, caps, allowlists, tracing and private taint."""

from types import SimpleNamespace

import pytest

from runtime.recall import memory_query, prefetch_recall, recall_intents
from runtime.tool_base import Tool, ToolContext, ToolResult


def _config(max_chars=1600):
    return {"tool_selection": {
        "keyword_namespaces": {
            "memory": ["remember", "memory", "do you recall", "we discussed"],
            "rag": ["retrieve", "knowledge base", "collection"],
            "kg": ["knowledge", "graph"],
        },
        "recall": {"enabled": True, "max_chars": max_chars,
                   "collection": None, "min_score": 0.5},
    }}


class _RecallTool(Tool):
    private = True

    def __init__(self, name, payload):
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.payload = payload
        self.calls = []

    async def execute(self, args, ctx):
        self.calls.append((args, ctx.owner, ctx.project_id))
        if isinstance(self.payload, Exception):
            raise self.payload
        return ToolResult(status="ok", result=self.payload)


class _Runtime:
    def __init__(self, *tools):
        self.tools = {tool.name: tool for tool in tools}
        self.config = {"web": {"tool_preview_chars": 8000}}
        self.cost_table = {}

    async def _execute_tool(self, name, args, ctx):
        tool = self.tools[name]
        try:
            result = await tool.execute(args, ctx)
        except Exception as exc:
            result = ToolResult(status="error", result=None, error=str(exc))
        result.tool_name = name
        result.private = tool.private
        return result


def _state(query, allowed=None):
    return SimpleNamespace(
        allowed=allowed, messages=[{"role": "system", "content": "stable"},
                                   {"role": "user", "content": query}],
        private_taint=set(), trajectory=[], tools_used=[],
        budget=SimpleNamespace(cost_usd=0.0))


async def _run(runtime, rs, query, *, collection=None, depth=0, disabled=None,
               config=None):
    ctx = ToolContext(request_id="t", budget=None, config=config or _config(),
                      owner="alice", project_id="project-1")
    events = []

    async def emit(kind, iteration, data):
        events.append((kind, iteration, data))

    async def emit_cost(model, delta):
        raise AssertionError("recall stubs do not bill tokens")

    await prefetch_recall(runtime, rs, ctx, query=query, collection=collection,
                          depth=depth, disabled_tools=disabled,
                          emit=emit, emit_cost=emit_cost)
    return ctx, events


def test_intent_is_narrow_and_memory_query_uses_content_terms():
    cfg = _config()
    assert recall_intents("What do you remember about my telescope?", cfg) == {"memory"}
    assert recall_intents("Please remember that my telescope is a Dobsonian", cfg) == set()
    assert recall_intents("Tell me about knowledge graphs", cfg) == set()
    assert recall_intents("Retrieve notes from the telescope collection", cfg) == {"rag"}
    query = memory_query("What do you remember about my favourite telescope?")
    assert "telescope" in query and "favourite" in query
    assert "what" not in query.lower() and " OR " in query


@pytest.mark.asyncio
async def test_memory_prefetch_injects_private_evidence_and_records_call():
    tool = _RecallTool("memory.search", {"matches": [{
        "kind": "fact", "tags": "telescope", "content": "Favourite: Dobsonian",
    }]})
    runtime = _Runtime(tool)
    query = "What do you remember about my telescope?"
    rs = _state(query)
    ctx, events = await _run(runtime, rs, query)

    assert tool.calls[0] == ({"query": "telescope", "limit": 3},
                             "alice", "project-1")
    assert [m["role"] for m in rs.messages] == ["system", "system", "user"]
    assert "Dobsonian" in rs.messages[1]["content"]
    assert rs.private_taint == {1} and ctx.private_taint is True
    assert rs.tools_used == ["memory.search"]
    assert rs.trajectory == ["memory.search(telescope)→ok"]
    assert events[0][0:2] == ("tool_result", 0)
    assert events[0][2]["recall_prefetch"] is True

    with_notice = _state(query)
    with_notice.messages.append({"role": "system", "content": "run-start notice"})
    await _run(runtime, with_notice, query)
    assert [m["role"] for m in with_notice.messages] == [
        "system", "system", "user", "system"]
    assert with_notice.private_taint == {1}


@pytest.mark.asyncio
async def test_empty_and_irrelevant_recall_injects_and_taints_nothing():
    tool = _RecallTool("memory.search", {"matches": []})
    runtime = _Runtime(tool)
    query = "What do you remember about my telescope?"
    rs = _state(query)
    ctx, events = await _run(runtime, rs, query)
    assert len(rs.messages) == 2 and not rs.private_taint
    assert not hasattr(ctx, "private_taint") or not ctx.private_taint
    assert len(events) == 1                    # attempted search is still traced

    unrelated = _state("Tell me about knowledge graphs")
    await _run(runtime, unrelated, "Tell me about knowledge graphs")
    assert len(tool.calls) == 1


@pytest.mark.asyncio
async def test_rag_requires_collection_and_uses_threshold_and_graph():
    tool = _RecallTool("rag.search", {
        "matches": [{"collection": "scope", "source": "notes.txt", "score": 0.9,
                     "text": "The telescope is a Dobsonian."}],
        "graph_excerpt": "telescope -[type]-> Dobsonian",
    })
    runtime = _Runtime(tool)
    query = "Retrieve my telescope notes from the knowledge base"
    without_scope = _state(query)
    await _run(runtime, without_scope, query)
    assert tool.calls == []

    rs = _state(query)
    await _run(runtime, rs, query, collection="scope")
    assert tool.calls[0][0] == {"query": query, "collection": "scope", "top_k": 3,
                                "min_score": 0.5}
    assert "Dobsonian" in rs.messages[1]["content"]
    assert "Project graph" in rs.messages[1]["content"]

    inferred_query = "Retrieve notes from telescope collection"
    inferred = _state(inferred_query)
    await _run(runtime, inferred, inferred_query)
    assert tool.calls[1][0]["collection"] == "telescope"


@pytest.mark.asyncio
async def test_prefetch_respects_depth_allowlist_disabled_and_cap():
    tool = _RecallTool("memory.search", {"matches": [{
        "kind": "fact", "content": "x" * 1000,
    }]})
    runtime = _Runtime(tool)
    query = "What do you remember about telescopes?"
    for kwargs in ({"depth": 1}, {"disabled": {"memory.search"}}):
        rs = _state(query)
        await _run(runtime, rs, query, **kwargs)
        assert len(rs.messages) == 2
    rs = _state(query, allowed=[])
    await _run(runtime, rs, query)
    assert tool.calls == []

    rs = _state(query)
    await _run(runtime, rs, query, config=_config(max_chars=200))
    assert len(rs.messages[1]["content"]) == 200
    assert rs.messages[1]["content"].endswith("…")
