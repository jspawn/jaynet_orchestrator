"""Pid-reuse guards: job.cancel and serving.stop_server must verify process
identity via /proc/<pid>/cmdline before signaling a process group, and a job
with a written exit_code is finished, not cancelable. All /proc reads and
signals are faked — no real processes are touched."""
import asyncio
import json
import signal
import tempfile
from pathlib import Path

import pytest
from conftest import run

from runtime import serving
from runtime.tool_base import ToolContext
from tools.job import runner
from tools.job.runner import JobCancel, JobLogs, JobStatus, JobWait

# ------------------------------- job.cancel -------------------------------- #

def _ctx(root: str) -> ToolContext:
    return ToolContext(
        request_id="t",
        config={"tools": {"job": {"jobs_root": root}}},
        budget=None, work_root=tempfile.mkdtemp())


def _job_dir(root: Path, jid: str, pid: int = 4321, with_exit: bool = False) -> Path:
    d = root / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "job_id": jid, "command": "sleep 100", "pid": pid}))
    if with_exit:
        (d / "exit_code").write_text("0")
    return d


def test_cancel_finished_job_reports_state_and_never_kills(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append(sig))
    _job_dir(tmp_path, "j1", with_exit=True)
    r = run(JobCancel().execute({"job_id": "j1"}, _ctx(str(tmp_path))))
    assert r.status == "ok"
    assert r.result["state"] == "succeeded" and r.result["exit_code"] == 0
    assert "finished" in r.result["note"]
    assert killed == []


def test_cancel_refuses_recycled_pid(tmp_path, monkeypatch):
    _job_dir(tmp_path, "j2")
    killed = []
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: True)
    # pid alive, but it's someone else's process now
    monkeypatch.setattr(runner, "_pid_cmdline", lambda pid: "python -m http.server")
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append(sig))
    r = run(JobCancel().execute({"job_id": "j2"}, _ctx(str(tmp_path))))
    assert r.status == "error" and "recycled" in r.error
    assert killed == []


def test_cancel_kills_group_when_identity_matches(tmp_path, monkeypatch):
    d = _job_dir(tmp_path, "j3")
    sent = []
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: not sent)  # dies on SIGTERM
    monkeypatch.setattr(runner, "_pid_cmdline", lambda pid: f"bash {d}/run.sh")
    monkeypatch.setattr("os.getpgid", lambda pid: 4321)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))
    r = run(JobCancel().execute({"job_id": "j3", "grace_s": 1}, _ctx(str(tmp_path))))
    assert r.status == "ok" and r.result["signal"] == "SIGTERM"
    assert sent == [signal.SIGTERM]
    assert (d / "exit_code").read_text() == "143"  # deterministic 'failed' state


def test_cancel_does_not_escalate_if_pid_changes_hands(tmp_path, monkeypatch):
    d = _job_dir(tmp_path, "j4")
    sent = []
    calls = {"n": 0}

    def fake_cmdline(pid):
        calls["n"] += 1
        # matches at the pre-SIGTERM check, recycled by the SIGKILL re-check
        return f"bash {d}/run.sh" if calls["n"] == 1 else "someone-else --innocent"

    monkeypatch.setattr(runner, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(runner, "_pid_cmdline", fake_cmdline)
    monkeypatch.setattr("os.getpgid", lambda pid: 4321)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))
    r = run(JobCancel().execute({"job_id": "j4", "grace_s": 0}, _ctx(str(tmp_path))))
    assert r.status == "error" and "SIGKILL" in r.error
    assert sent == [signal.SIGTERM]  # TERM went out while identity held; KILL refused


def test_pid_cmdline_reads_proc_format(tmp_path, monkeypatch):
    # NUL-separated argv flattened to spaces; '' when unreadable.
    f = tmp_path / "cmdline"
    f.write_bytes(b"bash\x00/srv/jobs/j9/run.sh\x00")
    orig = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: orig(f))
    assert runner._pid_cmdline(4321) == "bash /srv/jobs/j9/run.sh"


def test_cancel_real_job_e2e(tmp_path):
    # Real process + real /proc: the identity check must not break the happy
    # path. Same local-process style as tests/test_job_wait.py.
    from tools.job.runner import JobStart, JobStatus
    ctx = _ctx(str(tmp_path))
    r = run(JobStart().execute({"name": "e2e", "command": "sleep 60"}, ctx))
    jid = r.result["job_id"]
    c = run(JobCancel().execute({"job_id": jid, "grace_s": 3}, ctx))
    assert c.status == "ok" and c.result["signal"] == "SIGTERM"
    s = run(JobStatus().execute({"job_id": jid}, ctx))
    assert s.result["state"] == "failed" and s.result["exit_code"] == 143


# ------------------------------ serving.stop_server ------------------------- #

def _server_entry(tmp_path: Path, pid: int = 4321) -> dict:
    d = tmp_path / "brain1"
    d.mkdir(exist_ok=True)
    return {"name": "brain1", "pid": pid, "log_dir": str(d)}


def test_stop_server_refuses_recycled_pid(tmp_path, monkeypatch):
    entry = _server_entry(tmp_path)
    sent = []
    monkeypatch.setattr(serving, "pid_alive", lambda pid: True)
    monkeypatch.setattr(serving, "pid_cmdline", lambda pid: "vim /etc/fstab")
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))
    assert serving.stop_server(entry, grace_s=1) is False
    assert sent == []


def test_stop_server_kills_matching_group(tmp_path, monkeypatch):
    entry = _server_entry(tmp_path)
    sent = []
    monkeypatch.setattr(serving, "pid_alive", lambda pid: not sent)  # dies on SIGTERM
    monkeypatch.setattr(serving, "pid_cmdline",
                        lambda pid: f"bash {entry['log_dir']}/run.sh")
    monkeypatch.setattr("os.getpgid", lambda pid: 4321)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))
    assert serving.stop_server(entry, grace_s=1) is True
    assert sent == [signal.SIGTERM]


def test_stop_server_escalates_to_sigkill_when_lingering(tmp_path, monkeypatch):
    entry = _server_entry(tmp_path)
    sent = []
    monkeypatch.setattr(serving, "pid_alive", lambda pid: True)
    monkeypatch.setattr(serving, "pid_cmdline",
                        lambda pid: f"bash {entry['log_dir']}/run.sh")
    monkeypatch.setattr("os.getpgid", lambda pid: 4321)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))
    assert serving.stop_server(entry, grace_s=0) is True
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_stop_server_missing_pid_is_noop(tmp_path):
    assert serving.stop_server({"name": "brain1"}, grace_s=0) is False


# --------------------------- job_id path traversal -------------------------- #

@pytest.mark.parametrize("cls", [JobStatus, JobWait, JobLogs, JobCancel])
@pytest.mark.parametrize("jid", ["..", "a/b", "a\\b", "/etc", "x/../y"])
def test_job_id_traversal_rejected_on_all_tools(tmp_path, monkeypatch, cls, jid):
    _job_dir(tmp_path, "j1")
    killed = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append(sig))
    r = run(cls().execute({"job_id": jid}, _ctx(str(tmp_path))))
    assert r.status == "error" and "invalid job_id" in r.error
    assert killed == []


def test_job_id_traversal_to_existing_dir_rejected(tmp_path, monkeypatch):
    # '../<root name>' resolves to the real jobs root — it EXISTS, so only the
    # guard (not the no-such-job check) can stop it, on every entry point.
    jid = f"../{tmp_path.name}"
    _job_dir(tmp_path, "j1")
    killed = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed.append(sig))
    for cls in (JobStatus, JobWait, JobLogs, JobCancel):
        r = run(cls().execute({"job_id": jid}, _ctx(str(tmp_path))))
        assert r.status == "error" and "invalid job_id" in r.error, (cls, jid)
    assert killed == []


def test_plain_job_ids_still_accepted(tmp_path):
    d = _job_dir(tmp_path, "20260718-220000-train_run")
    r = run(JobStatus().execute({"job_id": d.name}, _ctx(str(tmp_path))))
    assert r.status == "ok" and r.result["job_id"] == d.name


def test_cancel_grace_loop_does_not_block_the_event_loop(tmp_path, monkeypatch):
    # The grace wait must be asyncio.sleep, not time.sleep: with a lingering
    # pid, other coroutines on the loop must run DURING the grace window.
    d = _job_dir(tmp_path, "j5")
    sent = []
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: True)  # lingers: full grace
    monkeypatch.setattr(runner, "_pid_cmdline", lambda pid: f"bash {d}/run.sh")
    monkeypatch.setattr("os.getpgid", lambda pid: 4321)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: sent.append(sig))

    async def scenario():
        task = asyncio.create_task(
            JobCancel().execute({"job_id": "j5", "grace_s": 1}, _ctx(str(tmp_path))))
        await asyncio.sleep(0.3)     # lands inside the grace window
        ran_during_grace = not task.done()
        return ran_during_grace, await task

    ran_during_grace, r = run(scenario())
    assert ran_during_grace, "cancel blocked the event loop during the grace wait"
    assert r.status == "ok" and r.result["signal"] == "SIGKILL"  # lingering -> escalate
    assert sent == [signal.SIGTERM, signal.SIGKILL]
