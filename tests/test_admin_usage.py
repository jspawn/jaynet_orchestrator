"""Admin usage endpoint: per-tool and per-skill call counts + last-used,
aggregated on demand from the trace events table (read-only — nothing is
collected at runtime beyond what the trace already logs)."""
import json
import sqlite3

import pytest

pytestmark = pytest.mark.asyncio


def _seed_trace(db_path):
    # web_app's runtime already created the schema via Trace() — just insert.
    conn = sqlite3.connect(db_path)
    rows = [
        (100.0, {"tool": "fs.read"}),
        (200.0, {"tool": "fs.read"}),
        (300.0, {"tool": "skill.load", "args": {"name": "j-space"}}),
        (400.0, {"tool": "code.run"}),
    ]
    for ts, payload in rows:
        conn.execute("INSERT INTO events (run_id, ts, kind, iteration,"
                     " payload_json) VALUES ('r', ?, 'tool_result', 1, ?)",
                     (ts, json.dumps(payload)))
    conn.commit()
    conn.close()


async def test_usage_tools_aggregates_counts_and_last_used(web_app, web_client,
                                                           tmp_path):
    app = web_app()
    _seed_trace(tmp_path / "trace.db")
    async with web_client(app) as c:
        r = await c.get("/api/admin/usage/tools")
    assert r.status_code == 200
    d = r.json()
    tools = {t["name"]: t for t in d["tools"]}
    assert tools["fs.read"]["uses"] == 2
    assert tools["fs.read"]["last_used"] == 200.0
    assert tools["code.run"]["uses"] == 1
    # the fixture registry has no discovered tools, so these arrive as
    # trace-only (removed/unregistered) entries rather than dropped
    assert tools["fs.read"]["registered"] is False
    skills = {s["name"]: s for s in d["skills"]}
    assert skills["j-space"]["uses"] == 1
    assert skills["j-space"]["last_used"] == 300.0


async def test_usage_tools_empty_without_trace_db(web_app, web_client):
    app = web_app()                      # no trace.db file at all
    async with web_client(app) as c:
        r = await c.get("/api/admin/usage/tools")
    assert r.status_code == 200
    d = r.json()
    assert all(t["uses"] == 0 for t in d["tools"])
    assert all(s["uses"] == 0 for s in d["skills"])
