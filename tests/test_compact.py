"""Conversation compaction (/compact): history slicing, prompt building, the
one-shot brain completion, and the /api/chat route's replacement-history
payload. Endpoint tests drive FastAPI in-process (docs/testing-harness.md)."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

import web
from runtime import compact
from runtime.loop import AgentRuntime

ROOT = Path(web.__file__).resolve().parent.parent


def _hist(n_exchanges):
    h = []
    for i in range(n_exchanges):
        h.append({"role": "user", "content": f"u{i}"})
        h.append({"role": "assistant", "content": f"a{i}"})
    return h


# ---- history slicing ---------------------------------------------------------
def test_slice_short_history_nothing_to_do():
    older, kept = compact.slice_history(_hist(3))
    assert older == [] and len(kept) == 6


def test_slice_splits_older_and_verbatim_tail():
    older, kept = compact.slice_history(_hist(10))
    assert len(kept) == compact.KEEP_LAST_EXCHANGES * 2
    assert len(older) == 20 - len(kept)
    assert kept[0]["content"] == "u8"                # tail is verbatim
    assert older[-1]["content"] == "a7"              # summary covers the rest


def test_slice_skips_empty_and_foreign_roles():
    h = [{"role": "system", "content": "x"},
         {"role": "user", "content": ""}] + _hist(10)
    older, kept = compact.slice_history(h)
    assert all(m["content"] for m in older + kept)
    assert all(m["role"] in ("user", "assistant") for m in older + kept)


def test_slice_zero_keep_window():
    older, kept = compact.slice_history(_hist(5), keep_exchanges=0)
    assert len(older) == 10 and kept == []


# ---- prompt building ---------------------------------------------------------
def test_build_summary_messages_includes_transcript_and_focus():
    older, _ = compact.slice_history(_hist(6))
    msgs = compact.build_summary_messages(older, "the database work")
    assert msgs[0]["role"] == "system" and "continuity brief" in msgs[0]["content"]
    assert "u0" in msgs[1]["content"] and "a3" in msgs[1]["content"]
    assert "the database work" in msgs[1]["content"]


def test_build_summary_messages_caps_transcript():
    older = [{"role": "user", "content": "old " + "x" * compact.MAX_TRANSCRIPT_CHARS},
             {"role": "assistant", "content": "recent tail"}]
    body = compact.build_summary_messages(older, "")[1]["content"]
    assert "oldest exchanges dropped" in body
    assert "recent tail" in body                    # the tail survives the cap
    assert len(body) <= compact.MAX_TRANSCRIPT_CHARS + 2000


def test_nothing_note_and_footer():
    assert "Nothing to compact" in compact.nothing_note(4)
    f = compact.result_footer(12, 4, {"total_tokens": 1234})
    assert "12 messages" in f and "2 exchanges" in f and "1,234 tokens" in f


# ---- the one-shot brain call -------------------------------------------------
class _FakeRuntime:
    model = "local-brain"

    async def _model_turn(self, messages, tools_schema, model=None, think=True,
                          sampling=None):
        self.seen = {"tools": tools_schema, "model": model, "think": think}
        return {"message": {"role": "assistant",
                            "content": "<think>ruminate</think>The brief."},
                "usage": {"total_tokens": 321}}


def test_complete_is_tool_free_think_off_and_stripped():
    rt = _FakeRuntime()
    out = asyncio.run(AgentRuntime.complete(rt, [{"role": "user", "content": "x"}]))
    assert out["content"] == "The brief."           # think block stripped
    assert out["usage"]["total_tokens"] == 321
    assert rt.seen["tools"] == [] and rt.seen["think"] is False
    assert rt.seen["model"] == "local-brain"        # always the local brain


# ---- endpoint: /api/chat with /compact ---------------------------------------
def _app(tmp_path, monkeypatch):
    base = tmp_path
    (base / "config").mkdir()
    (base / "prompts").mkdir()
    cfg = yaml.safe_load(open(ROOT / "config/runtime.yaml"))
    cfg["trace"]["db_path"] = str(base / "trace.db")
    cfg["orchestrator"]["system_prompt"] = "prompts/orchestrator.md"
    cfg["web"] = {"chats_db": str(base / "chats.db"),
                  "users_db": str(base / "users.db"),
                  "outputs_dir": str(base / "outputs"),
                  "projects_dir": str(base / "projects")}
    (base / "prompts" / "orchestrator.md").write_text("P")
    yaml.safe_dump(cfg, open(base / "config" / "runtime.yaml", "w"))
    monkeypatch.setenv("ORCH_ADMIN_USER", "admin")
    monkeypatch.setenv("ORCH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("ORCH_SESSION_SECRET", "t")
    from web.server import create_app
    return create_app(str(base / "config" / "runtime.yaml"))


@asynccontextmanager
async def _client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "pw"})
        assert r.status_code == 200
        yield c


@pytest.mark.asyncio
async def test_compact_route_returns_replacement_payload(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    seen = {}

    async def fake_complete(messages, *, think=False, sampling=None):
        seen["messages"] = messages
        return {"content": "SUMMARY BODY", "usage": {"total_tokens": 42}}

    app.state.runtime.complete = fake_complete
    async with _client(app) as c:
        r = await c.post("/api/chat", json={
            "message": "/compact keep the db bits", "history": _hist(8)})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
        text = r.text
    assert "SUMMARY BODY" in text
    assert "compacted 12 messages" in text          # 16 - 4 kept
    assert '"kept_messages": 4' in text
    assert '"dropped_messages": 12' in text
    assert '"summary": "SUMMARY BODY"' in text
    # The focus instruction reached the brain's prompt.
    assert "keep the db bits" in seen["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_compact_short_history_skips_the_model(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    called = {"n": 0}

    async def fake_complete(messages, **kw):
        called["n"] += 1
        return {"content": "X", "usage": {}}

    app.state.runtime.complete = fake_complete
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "/compact",
                                            "history": _hist(2)})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert called["n"] == 0
    assert "Nothing to compact" in r.text
    fin = [ln for ln in r.text.splitlines() if '"run_finish"' in ln][0]
    assert '"compact"' not in fin                   # no replacement payload


@pytest.mark.asyncio
async def test_compact_brain_failure_keeps_history(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def boom(messages, **kw):
        raise RuntimeError("brain offline")

    app.state.runtime.complete = boom
    async with _client(app) as c:
        r = await c.post("/api/chat", json={"message": "/compact",
                                            "history": _hist(8)})
        rid = r.json()["run_id"]
        r = await asyncio.wait_for(c.get(f"/api/stream/{rid}"), timeout=10)
    assert "compact failed" in r.text and "brain offline" in r.text
    fin = [ln for ln in r.text.splitlines() if '"run_finish"' in ln][0]
    assert '"compact"' not in fin                   # client keeps its history
