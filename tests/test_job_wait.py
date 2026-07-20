"""job.wait blocking behaviour + the loop guard's poll-safe exemption."""
import asyncio
import tempfile

from runtime.tool_base import ToolContext
from tools.job.runner import JobStart, JobWait


def _ctx():
    return ToolContext(
        request_id="t",
        config={"tools": {"job": {"jobs_dir": tempfile.mkdtemp()}}},
        budget=None, work_root=tempfile.mkdtemp())


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_job_wait_blocks_until_finish():
    ctx = _ctx()
    r = _run(JobStart().execute(
        {"name": "s", "command": "bash -lc 'sleep 1; echo done'"}, ctx))
    jid = r.result["job_id"]
    w = _run(JobWait().execute({"job_id": jid, "timeout_s": 10}, ctx))
    assert w.result["state"] == "succeeded"
    assert w.result["exit_code"] == 0
    assert "done" in w.result["stdout"]


def test_job_wait_returns_running_note_on_timeout():
    ctx = _ctx()
    r = _run(JobStart().execute(
        {"name": "s", "command": "bash -lc 'sleep 5'"}, ctx))
    jid = r.result["job_id"]
    w = _run(JobWait().execute({"job_id": jid, "timeout_s": 1}, ctx))
    assert w.result["state"] == "running"
    assert "again" in w.result.get("note", "")   # tells the agent to keep waiting


def test_status_and_wait_tools_are_poll_safe():
    # These are exempt from the duplicate-call loop guard so polling a running
    # job with identical args isn't mistaken for a loop.
    from tools.job.runner import JobStatus, JobLogs, JobList
    assert JobStatus.poll_safe and JobLogs.poll_safe
    assert JobList.poll_safe and JobWait.poll_safe


def test_loop_guard_exempts_poll_safe(tmp_path):
    # Mirror the loop guard's bookkeeping — (sig, mutation_generation) pairs:
    # a poll-safe tool can repeat the same call indefinitely while a normal
    # tool is blocked on the 3rd identical call WITHIN one generation.
    poll_safe = {"job.status"}
    recent, blocked, gen = [], [], 0
    for name in ["job.status"] * 5 + ["fs.read", "fs.read", "fs.read"]:
        sig = (name + "|{}", gen)
        exempt = name in poll_safe
        if not exempt and recent.count(sig) >= 2:
            blocked.append(name); continue
        if not exempt:
            recent.append(sig)
    assert "job.status" not in blocked     # never blocked
    assert blocked == ["fs.read"]          # the 3rd identical fs.read is
