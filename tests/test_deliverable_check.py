"""Deliverable check (AgentRuntime._missing_deliverables).

Named-but-missing is the signal: input files a task references already exist,
so an absolute path named in the task or the final answer that does NOT exist
in the workspace is almost always an unwritten deliverable — the dominant
small-brain eval failure (solved the task, never called fs.write).
"""
from runtime.loop import AgentRuntime
from runtime.tool_base import ToolContext


def _ctx(tmp_path):
    return ToolContext(request_id="t", config={}, budget=None,
                       work_root=str(tmp_path))


def test_missing_named_file_is_reported(tmp_path):
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "Write the answer to /app/answer.txt and finish.")
    assert missing == ["/app/answer.txt"]


def test_existing_input_file_is_not_reported(tmp_path):
    (tmp_path / "rules.json").write_text("{}")
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "Read /app/rules.json, then write /app/out.txt.")
    assert missing == ["/app/out.txt"]


def test_paths_outside_workspace_are_skipped(tmp_path):
    missing = AgentRuntime._missing_deliverables(
        _ctx(tmp_path), "compare with /etc/passwd and /definitely/not/here.txt")
    # /etc/passwd: real host path outside the workspace — not ours to check.
    # /definitely/...: fictional root, rebased into the workspace → missing.
    assert missing == ["/definitely/not/here.txt"]


def test_urls_and_templates_are_not_matched(tmp_path):
    text = ("see http://example.com/spec.txt and https://x.io/a/b.json; "
            "name it incident_<IP>_<timestamp>.txt like the schema says")
    assert AgentRuntime._missing_deliverables(_ctx(tmp_path), text) == []


def test_duplicates_collapsed_and_capped(tmp_path):
    text = " ".join(f"/app/f{i}.txt" for i in range(10)) + " /app/f0.txt"
    missing = AgentRuntime._missing_deliverables(_ctx(tmp_path), text)
    assert len(missing) == 5
    assert len(set(missing)) == 5


def test_no_workspace_returns_nothing():
    ctx = ToolContext(request_id="t", config={"tools": {"fs": {} }},
                      budget=None, work_root=None)
    assert AgentRuntime._missing_deliverables(ctx, "/app/x.txt") == []
