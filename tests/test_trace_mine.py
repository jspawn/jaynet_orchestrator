"""trace.mine: sequence extraction, n-gram counting, safety flagging."""
import asyncio, json, sqlite3, tempfile, os
import tools.trace.mine as M
from tools.trace.mine import TraceMine
from runtime.tool_base import ToolContext

def _make_db(runs):
    """runs: {run_id: [tool,...]}. Returns a temp db path with events + runs rows."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, started_at REAL, owner TEXT, user_message TEXT, status TEXT)")
    c.execute("CREATE TABLE events(run_id TEXT, ts REAL, kind TEXT, iteration INT, payload_json TEXT)")
    for rid, seq in runs.items():
        c.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (rid, 1000.0, "u", "m", "ok"))
        for t in seq:
            c.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                      (rid, 1.0, "tool_result", 0, json.dumps({"tool": t})))
    c.commit(); c.close()
    return path

def _ctx(path): return ToolContext(request_id="t", config={"trace": {"db_path": path}}, budget=None)
def _run(path, args): return asyncio.run(TraceMine().execute(args, _ctx(path)))

def test_finds_recurring_readonly_prefix():
    # fs.find -> fs.read opens 3 of 3 runs: a fusable, high-coverage candidate
    path = _make_db({"r1": ["fs.find", "fs.read", "code.run"],
                     "r2": ["fs.find", "fs.read", "fs.write"],
                     "r3": ["fs.find", "fs.read", "git.status"]})
    r = _run(path, {"min_count": 2})
    assert r.status == "ok" and r.result["runs_analyzed"] == 3
    top = r.result["bigrams"][0]
    assert top["sequence"] == "fs.find -> fs.read"
    assert top["count"] == 3 and top["run_coverage"] == 1.0 and top["opens_run"] == 3
    assert top["fusable"] is True and top["safety"] == "read-only"
    os.unlink(path)

def test_flags_side_effects():
    path = _make_db({"r1": ["fs.read", "fs.write"], "r2": ["fs.read", "fs.write"]})
    r = _run(path, {"min_count": 2})
    b = r.result["bigrams"][0]
    assert b["sequence"] == "fs.read -> fs.write"
    assert b["fusable"] is False and b["mutating_steps"] == ["fs.write"]
    os.unlink(path)

def test_trigrams_and_candidates():
    path = _make_db({f"r{i}": ["ops.status", "serve.list", "gpu.status", "model.use"] for i in range(4)})
    r = _run(path, {"min_count": 3})
    tri = r.result["trigrams"][0]
    assert tri["sequence"] == "ops.status -> serve.list -> gpu.status" and tri["fusable"] is True
    assert any(c["fusable"] for c in r.result["top_meta_tool_candidates"])
    os.unlink(path)

def test_min_count_filter():
    path = _make_db({"r1": ["a.x", "b.y"]})   # occurs once
    r = _run(path, {"min_count": 3})
    assert r.result["bigrams"] == []
    os.unlink(path)

def test_empty_db():
    path = _make_db({})
    assert _run(path, {}).result["runs_analyzed"] == 0
    os.unlink(path)
