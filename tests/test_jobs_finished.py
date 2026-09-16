"""The job completion feed (/api/jobs/finished): the chat UI polls it to
announce detached jobs (job.start) when they reach a terminal state.

Terminal = exit_code file present (succeeded/failed); "ended" (pid gone, no
exit code) and running jobs stay out; jobs owned by someone else stay out.
"""

import json
import time

import pytest


def _mkjob(root, jid, name, exit_code=None, owner=None):
    d = root / jid
    d.mkdir()
    meta = {"job_id": jid, "name": name, "pid": 999999,
            "started_at_epoch": time.time() - 60, "owner": owner}
    (d / "meta.json").write_text(json.dumps(meta))
    if exit_code is not None:
        (d / "exit_code").write_text(str(exit_code))
    return d


@pytest.mark.asyncio
async def test_jobs_finished_feed(web_app, web_client, tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.paths.JOBS_DIR", tmp_path)
    _mkjob(tmp_path, "job-ok", "test-suite", exit_code=0)            # legacy: no owner
    _mkjob(tmp_path, "job-bad", "train", exit_code=1)
    _mkjob(tmp_path, "job-ended", "crashed")                         # no exit_code
    _mkjob(tmp_path, "job-other", "not-yours", exit_code=0,
           owner="someone-else")

    app = web_app()
    async with web_client(app) as c:
        r = await c.get("/api/jobs/finished")
        assert r.status_code == 200, r.text
        jobs = {j["job_id"]: j for j in r.json()["jobs"]}

    assert jobs["job-ok"]["state"] == "succeeded"
    assert jobs["job-ok"]["exit_code"] == 0
    assert jobs["job-ok"]["finished_at"] is not None
    assert jobs["job-bad"]["state"] == "failed"
    assert "job-ended" not in jobs          # no exit code → not terminal
    assert "job-other" not in jobs          # owner-filtered
