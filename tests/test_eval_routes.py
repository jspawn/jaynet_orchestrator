"""Eval harness admin API (web/routes_eval.py): case CRUD over the custom
layer, validation, local-only drafting, background runs + run-status, results/
trend reads, the proposals inbox (accept side effects / reject), and the
flag → make-test bridge with its include_private opt-in.

Hermetic: runtime.paths.CUSTOM_EVALS_DIR / EVAL_DB / HOME / DATA are pointed at
tmp_path — nothing touches the real ORCH_DATA or install tree. Uses the
conftest web_app/web_client fixtures (admin/pw session).
"""
from __future__ import annotations

import asyncio
import time

import httpx
from pathlib import Path
import pytest

from runtime import eval_runner, paths
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolResult
from runtime.trace import Trace

CASE_YAML = """\
id: smoke-case
name: Smoke case
tags: [smoke]
driver: scripted
turns:
  - user: "hello"
judge_rubric: |
  Pass if the answer is friendly.
"""


@pytest.fixture
def evalapp(web_app, tmp_path, monkeypatch):
    """(app, custom_evals_dir, builtin_evals_dir) — all tmp-rooted."""
    app = web_app()
    home = tmp_path / "home"
    builtin_evals = home / "evals"
    builtin_evals.mkdir(parents=True)
    custom_evals = tmp_path / "custom" / "evals"
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "DATA", tmp_path)
    monkeypatch.setattr(paths, "CUSTOM_EVALS_DIR", custom_evals)
    monkeypatch.setattr(paths, "EVAL_DB", tmp_path / "eval.db")
    return app, custom_evals, builtin_evals


def _store():
    return EvalStore(paths.EVAL_DB)


# ---- auth ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requires_auth(evalapp):
    app, *_ = evalapp
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/admin/evals")).status_code == 401
        assert (await c.post("/api/admin/evals/validate",
                             json={"yaml": CASE_YAML})).status_code == 401


# ---- list / detail -----------------------------------------------------------

@pytest.mark.asyncio
async def test_list_cases_with_latest(evalapp, web_client):
    app, custom_evals, builtin_evals = evalapp
    (builtin_evals / "smoke-case.yaml").write_text(CASE_YAML)
    s = _store()
    s.record_result(test_id="smoke-case", passed=True, score=9.0,
                    judge_notes="fine", judge_model="m", cost_usd=0.01,
                    tokens=10, elapsed_s=1.0, status="ok", run_ids=[],
                    transcript=[])
    s.close()
    async with web_client(app) as c:
        r = await c.get("/api/admin/evals")
        assert r.status_code == 200
        cases = {x["id"]: x for x in r.json()["cases"]}
        assert cases["smoke-case"]["origin"] == "builtin"
        assert cases["smoke-case"]["driver"] == "scripted"
        assert cases["smoke-case"]["tags"] == ["smoke"]
        assert cases["smoke-case"]["last"]["passed"] is True
        assert cases["smoke-case"]["last"]["score"] == 9.0
        # detail: builtin file source
        d = (await c.get("/api/admin/evals/smoke-case")).json()
        assert d["yaml"] == CASE_YAML and d["origin"] == "builtin"
        assert d["name"] == "Smoke case"
        assert (await c.get("/api/admin/evals/nope")).status_code == 404
        assert (await c.get("/api/admin/evals/bad id")).status_code in (400, 404, 422)


# ---- validate ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_good_and_bad(evalapp, web_client):
    app, *_ = evalapp
    async with web_client(app) as c:
        r = await c.post("/api/admin/evals/validate", json={"yaml": CASE_YAML})
        assert r.json() == {"ok": True, "errors": []}
        r = await c.post("/api/admin/evals/validate",
                         json={"yaml": "name: x\n"})
        body = r.json()
        assert body["ok"] is False and body["errors"]
        r = await c.post("/api/admin/evals/validate",
                         json={"yaml": "turns: [unclosed"})
        assert r.json()["ok"] is False


# ---- PUT / DELETE (custom layer) ----------------------------------------------

@pytest.mark.asyncio
async def test_put_creates_custom_and_delete(evalapp, web_client):
    app, custom_evals, builtin_evals = evalapp
    async with web_client(app) as c:
        r = await c.put("/api/admin/evals/smoke-case", json={"yaml": CASE_YAML})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert (custom_evals / "smoke-case.yaml").read_text() == CASE_YAML
        d = (await c.get("/api/admin/evals/smoke-case")).json()
        assert d["origin"] == "custom"
        # id mismatch between path and body is refused
        bad = CASE_YAML.replace("id: smoke-case", "id: other-id")
        r = await c.put("/api/admin/evals/smoke-case", json={"yaml": bad})
        assert r.status_code == 400
        assert "does not match" in str(r.json()["detail"])
        # invalid YAML → 400 with error list, nothing written
        r = await c.put("/api/admin/evals/broken", json={"yaml": "name: x\n"})
        assert r.status_code == 400 and r.json()["detail"]["errors"]
        assert not (custom_evals / "broken.yaml").exists()
        # delete: custom goes away …
        r = await c.delete("/api/admin/evals/smoke-case")
        assert r.json() == {"ok": True}
        assert not (custom_evals / "smoke-case.yaml").exists()
        # … builtin-only is refused …
        (builtin_evals / "seed.yaml").write_text(CASE_YAML)
        r = await c.delete("/api/admin/evals/seed")
        assert r.status_code == 403
        assert (builtin_evals / "seed.yaml").is_file()
        # … and unknown ids 404
        assert (await c.delete("/api/admin/evals/ghost")).status_code == 404


# ---- run + run-status -----------------------------------------------------------

@pytest.mark.asyncio
async def test_run_case_background_and_status(evalapp, web_client, monkeypatch):
    app, _, builtin_evals = evalapp
    (builtin_evals / "smoke-case.yaml").write_text(CASE_YAML)
    seen = {}

    async def fake_suite(runtime, cases, store, *, disabled_tools=None,
                         progress=None, should_stop=None):
        seen["cases"] = [c.id for c in cases]
        seen["disabled"] = disabled_tools
        if progress:
            progress(cases[0].id, {"test_id": cases[0].id})
        return {"cases": 1, "ran": 1, "passed": 1, "failed": 0,
                "cost_usd": 0.0, "results": []}

    monkeypatch.setattr(eval_runner, "run_suite", fake_suite)
    async with web_client(app) as c:
        # exactly one of id/tag
        assert (await c.post("/api/admin/evals/run", json={})).status_code == 400
        r = await c.post("/api/admin/evals/run",
                         json={"id": "x", "tag": "y"})
        assert r.status_code == 400
        assert (await c.post("/api/admin/evals/run",
                             json={"id": "nope"})).status_code == 404
        assert (await c.post("/api/admin/evals/run",
                             json={"tag": "nope"})).status_code == 404
        r = await c.post("/api/admin/evals/run", json={"id": "smoke-case"})
        assert r.status_code == 200
        assert r.json() == {"started": True, "cases": 1}
        for _ in range(50):
            st = (await c.get("/api/admin/evals/run-status")).json()
            if not st["running"] and st["last"]:
                break
            await asyncio.sleep(0.1)
        assert st["running"] is False
        assert st["last"]["passed"] == 1
        assert seen["cases"] == ["smoke-case"]
        assert isinstance(seen["disabled"], set)


@pytest.mark.asyncio
async def test_cancel_endpoint(evalapp, web_client):
    from web import routes_eval
    app, *_ = evalapp
    async with web_client(app) as c:
        # nothing running → 409
        assert (await c.post("/api/admin/evals/cancel")).status_code == 409
        routes_eval._SUITE_STATE["running"] = True
        try:
            r = await c.post("/api/admin/evals/cancel")
            assert r.status_code == 200
            assert routes_eval._SUITE_STATE["cancelling"] is True
            st = (await c.get("/api/admin/evals/run-status")).json()
            assert st["cancelling"] is True
        finally:
            routes_eval._SUITE_STATE.update(running=False, cancelling=False)


# ---- results + trend -------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_and_trend(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    for i, ok in enumerate([True, False, True]):
        s.record_result(test_id="smoke-case", passed=ok, score=8.0 - i,
                        judge_notes=f"run {i}", judge_model="m", cost_usd=0.01,
                        tokens=10, elapsed_s=1.0, status="ok", run_ids=[],
                        transcript=[])
    s.close()
    async with web_client(app) as c:
        r = (await c.get("/api/admin/evals/results")).json()["results"]
        assert len(r) == 3
        assert "transcript" not in r[0]      # heavy blob stays out of lists
        r = (await c.get("/api/admin/evals/results",
                         params={"test_id": "smoke-case", "limit": 2})).json()
        assert len(r["results"]) == 2
        t = (await c.get("/api/admin/evals/trend/smoke-case")).json()["trend"]
        assert [x["passed"] for x in t] == [1, 0, 1]   # oldest first


# ---- proposals inbox ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposals_accept_prompt_tweak(evalapp, web_client, tmp_path,
                                             monkeypatch):
    from runtime import gate_prompt
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app, *_ = evalapp
    overlay = gate_prompt.overlay_path(app.state.runtime.config)
    s = _store()
    p = s.add_proposal(test_id="smoke-case", result_id=1,
                       classification="prompt-tweak", what="answers undated",
                       cause="prompt never asks for dates",
                       fix="Tell the model to state the date.")
    s.close()
    async with web_client(app) as c:
        props = (await c.get("/api/admin/evals/proposals",
                             params={"status": "pending"})).json()["proposals"]
        assert [x["id"] for x in props] == [p["id"]]
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "prompt" and body["path"] == str(overlay)
        assert body["proposal"]["status"] == "accepted"
        text = overlay.read_text()
        assert "<!-- eval-proposals -->" in text
        assert "Tell the model to state the date." in text
        # no longer pending
        assert (await c.get("/api/admin/evals/proposals",
                            params={"status": "pending"})).json()["proposals"] == []


@pytest.mark.asyncio
async def test_proposals_accept_bug_writes_issue(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    p = s.add_proposal(test_id="smoke-case", result_id=7,
                       classification="bug-for-dev", what="crash on empty",
                       cause="no guard", fix="add a guard")
    q = s.add_proposal(test_id="smoke-case", result_id=8,
                       classification="config", what="too slow",
                       cause="budget", fix="raise it")
    s.close()
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "issue"
        issue = paths.DATA / "eval-issues" / f"{p['id']}-smoke-case.md"
        assert body["path"] == str(issue) and issue.is_file()
        text = issue.read_text()
        assert "crash on empty" in text and "no guard" in text
        assert "add a guard" in text and "#7" in text
        # other classifications: status only
        r = await c.post(f"/api/admin/evals/proposals/{q['id']}/accept")
        assert r.json()["applied"] == "none"
        assert r.json()["proposal"]["status"] == "accepted"
        # unknown id 404s on both transitions
        assert (await c.post(
            "/api/admin/evals/proposals/999/accept")).status_code == 404
        assert (await c.post(
            "/api/admin/evals/proposals/999/reject")).status_code == 404


@pytest.mark.asyncio
async def test_proposals_reject(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    p = s.add_proposal(test_id="smoke-case", result_id=None,
                       classification="config", what="w", cause="c", fix="f")
    s.close()
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/reject")
        assert r.json()["proposal"]["status"] == "rejected"
        assert r.json()["applied"] == "none"


# ---- draft (local model only, litellm helper mocked) -----------------------------

@pytest.mark.asyncio
async def test_draft_validates(evalapp, web_client, monkeypatch):
    app, *_ = evalapp
    from runtime.tool_base import ToolResult

    async def fake(alias, task, payload, system, want_json, think, ctx):
        assert alias == "local-orchestrator"
        return ToolResult(status="ok",
                          result="```yaml\n" + CASE_YAML + "```\n")

    monkeypatch.setattr("web.routes_eval._call_via_litellm", fake)
    async with web_client(app) as c:
        r = await c.post("/api/admin/evals/draft",
                         json={"prompt": "a smoke case"})
        body = r.json()
        assert body["ok"] is True and body["errors"] == []
        assert body["yaml"].startswith("id: smoke-case")   # fence stripped
        # an invalid draft is a 200 with errors, not an HTTP error
        async def bad(alias, task, payload, system, want_json, think, ctx):
            return ToolResult(status="ok", result="name: only a name\n")
        monkeypatch.setattr("web.routes_eval._call_via_litellm", bad)
        r = await c.post("/api/admin/evals/draft", json={"prompt": "x"})
        body = r.json()
        assert r.status_code == 200 and body["ok"] is False and body["errors"]
        assert (await c.post("/api/admin/evals/draft",
                             json={"prompt": "  "})).status_code == 400


# ---- flag include_private round-trip + make-test -----------------------------------

def _seed_private_run(db_path, run_id, owner):
    t = Trace(db_path, log_content=True)
    t.start_run(run_id, "my secret question", owner=owner)
    t.log(run_id, "run_start", 0, {"message": "my secret question"})
    t.log(run_id, "run_finish", 0, {"answer": "secret answer",
                                    "status": "ok"})
    t.finish_run(run_id, "ok", final_answer="secret answer",
                 summary={"tokens": {"total": 5}, "cost_usd": 0.0})
    t.close()


@pytest.mark.asyncio
async def test_flag_include_private_roundtrip(evalapp, web_client, tmp_path):
    app, *_ = evalapp
    rid = "a" * 32
    _seed_private_run(str(tmp_path / "trace.db"), rid, owner="admin")
    async with web_client(app) as c:
        # default: structural only, content stripped
        r = await c.post("/api/flag", json={"run_ids": [rid]})
        fid_plain = r.json()["flag_id"]
        d = (await c.get(f"/api/admin/flags/{fid_plain}")).json()
        assert d["flag"]["include_private"] is False
        ev = next(e for e in d["runs"][0]["events"] if e["kind"] == "run_start")
        assert ev["payload"]["message"] == "<stripped>"
        assert "my secret question" not in str(d)
        # opt-in: run_start/run_finish content comes through (capped)
        r = await c.post("/api/flag", json={"run_ids": [rid],
                                            "include_private": True})
        fid_priv = r.json()["flag_id"]
        listed = (await c.get("/api/admin/flags")).json()["flags"]
        assert {f["id"]: f["include_private"] for f in listed} == \
            {fid_plain: False, fid_priv: True}
        d = (await c.get(f"/api/admin/flags/{fid_priv}")).json()
        assert d["flag"]["include_private"] is True
        evs = {e["kind"]: e["payload"] for e in d["runs"][0]["events"]}
        assert evs["run_start"]["message"] == "my secret question"
        assert evs["run_finish"]["answer"] == "secret answer"


@pytest.mark.asyncio
async def test_make_test_from_flag(evalapp, web_client, tmp_path, monkeypatch):
    app, *_ = evalapp
    rid = "b" * 32
    _seed_private_run(str(tmp_path / "trace.db"), rid, owner="admin")
    seen = {}

    async def fake_draft(alias, task, payload, system, want_json, think, ctx):
        seen["task"] = task
        seen["alias"] = alias
        return ToolResult(status="ok",
                          result="```yaml\n" + CASE_YAML + "```")

    monkeypatch.setattr("web.routes_eval._call_via_litellm", fake_draft)
    async with web_client(app) as c:
        r = await c.post("/api/flag", json={"run_ids": [rid],
                                            "comment": "it broke",
                                            "include_private": True})
        fid = r.json()["flag_id"]
        r = await c.post(f"/api/admin/flags/{fid}/make-test")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["suggested_id"] == "smoke-case"
        assert body["yaml"].startswith("id: smoke-case")
        # audit S1: drafting stays LOCAL even with opted-in private content
        assert seen["alias"] == "local-orchestrator"
        # coroner context + private content in the prompt
        assert "it broke" in seen["task"]
        assert "my secret question" in seen["task"]
        assert "secret answer" in seen["task"]
        # unknown flag → 404
        assert (await c.post("/api/admin/flags/nope/make-test")).status_code == 404


# ---- statistics (/stats, /compare) ---------------------------------------------

def _rec(s, test_id, ts, passed, score):
    row = s.record_result(test_id=test_id, passed=passed, score=score,
                          judge_notes="n", judge_model="m", cost_usd=0.01,
                          tokens=10, elapsed_s=1.0, status="ok", run_ids=[],
                          transcript=[], brain="b1")
    with s._lock, s._conn:
        s._conn.execute("UPDATE results SET ts=? WHERE id=?", (ts, row["id"]))


@pytest.mark.asyncio
async def test_stats_endpoint(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    _rec(s, "smoke-case", time.time(), True, 8.0)
    _rec(s, "smoke-case", time.time() - 40 * 86400, False, 4.0)   # outside 30d
    s.close()
    async with web_client(app) as c:
        r = await c.get("/api/admin/evals/stats")
        assert r.status_code == 200
        body = r.json()
        # the literal route wins over the /{case_id} catch-all
        assert body["days"] == 30
        assert {"days", "kpis", "per_case", "series"} <= set(body)
        assert body["kpis"]["runs"] == 1             # the 40-day-old row is out
        assert body["kpis"]["passed"] == 1
        assert body["per_case"][0]["test_id"] == "smoke-case"
        assert body["per_case"][0]["brains"] == ["b1"]
        r = await c.get("/api/admin/evals/stats", params={"days": 0})
        assert r.json()["kpis"]["runs"] == 2         # 0 = all time
        # window validation
        assert (await c.get("/api/admin/evals/stats",
                            params={"days": -1})).status_code == 400
        assert (await c.get("/api/admin/evals/stats",
                            params={"days": 3651})).status_code == 400


@pytest.mark.asyncio
async def test_stats_and_trend_brain_filter(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    _rec(s, "smoke-case", time.time(), True, 8.0)               # live run
    s.record_result(test_id="smoke-case", passed=False, score=3.0,
                    judge_notes="n", judge_model="m", cost_usd=0.01,
                    tokens=10, elapsed_s=1.0, status="ok", run_ids=[],
                    transcript=[], brain="v1-t0", benchmark=True)
    s.close()
    async with web_client(app) as c:
        body = (await c.get("/api/admin/evals/stats",
                            params={"days": 0})).json()
        assert body["brain"] is None
        assert body["kpis"]["runs"] == 1            # benchmark rep excluded
        body = (await c.get("/api/admin/evals/stats",
                            params={"days": 0, "brain": "v1-t0"})).json()
        assert body["brain"] == "v1-t0"
        assert body["kpis"]["runs"] == 1            # only the variant's rows
        t = (await c.get("/api/admin/evals/trend/smoke-case")).json()["trend"]
        assert len(t) == 1 and t[0]["passed"] == 1
        t = (await c.get("/api/admin/evals/trend/smoke-case",
                         params={"brain": "v1-t0"})).json()["trend"]
        assert len(t) == 1 and t[0]["passed"] == 0
        assert (await c.get("/api/admin/evals/stats",
                            params={"brain": "bad label!"})).status_code == 400
        assert (await c.get("/api/admin/evals/trend/smoke-case",
                            params={"brain": "bad label!"})).status_code == 400


@pytest.mark.asyncio
async def test_compare_endpoint(evalapp, web_client):
    app, *_ = evalapp
    now = time.time()
    s = _store()
    _rec(s, "smoke-case", now - 10 * 86400, True, 8.0)    # window A
    _rec(s, "smoke-case", now - 2 * 86400, False, 4.0)    # window B
    s.close()
    day = lambda ts: time.strftime("%Y-%m-%d", time.localtime(ts))
    async with web_client(app) as c:
        # YYYY-MM-DD dates (the to-day is inclusive)
        r = await c.get("/api/admin/evals/compare", params={
            "a_from": day(now - 12 * 86400), "a_to": day(now - 8 * 86400),
            "b_from": day(now - 3 * 86400), "b_to": day(now)})
        assert r.status_code == 200
        cases = {x["test_id"]: x for x in r.json()["cases"]}
        assert cases["smoke-case"]["a_runs"] == 1
        assert cases["smoke-case"]["b_runs"] == 1
        assert cases["smoke-case"]["pass_delta"] == -1.0
        assert cases["smoke-case"]["score_delta"] == -4.0
        # epoch floats work too
        r = await c.get("/api/admin/evals/compare", params={
            "a_from": now - 12 * 86400, "a_to": now - 8 * 86400,
            "b_from": now - 3 * 86400, "b_to": now})
        assert r.status_code == 200
        assert r.json()["cases"][0]["pass_delta"] == -1.0
        # garbage → 400, not a 500
        r = await c.get("/api/admin/evals/compare", params={
            "a_from": "banana", "a_to": day(now),
            "b_from": day(now), "b_to": day(now)})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_accepted_prompt_tweak_lands_in_overlay(evalapp, web_client,
                                                      tmp_path, monkeypatch):
    """Eval proposal accept must extend the $ORCH_DATA overlay, not the
    git-managed shipped prompt file."""
    from runtime import gate_prompt
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app, *_ = evalapp
    overlay = gate_prompt.overlay_path(app.state.runtime.config)
    store = EvalStore(paths.EVAL_DB)
    prop = store.add_proposal(test_id="t", result_id=None,
                              classification="prompt-tweak",
                              what="w", cause="c", fix="be stricter")
    store.close()
    shipped = Path(gate_prompt.shipped_path(app.state.runtime.config,
                                            app.state.runtime.config_path))
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shipped.write_text("SHIPPED", encoding="utf-8")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{prop['id']}/accept")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == "prompt"
        text = overlay.read_text()
        assert "SHIPPED" in text and "be stricter" in text
        assert "<!-- eval-proposals -->" in text
        assert shipped.read_text() == "SHIPPED"      # pristine
        assert "be stricter" in app.state.runtime.system_prompt


# ---- structured proposal accept paths (skill / tool / config) -----------------

def _proposal(cls, target=None, content=None, fix="f"):
    s = _store()
    p = s.add_proposal(test_id="smoke-case", result_id=1, classification=cls,
                       target=target, proposed_content=content,
                       what="w", cause="c", fix=fix)
    s.close()
    return p


@pytest.mark.asyncio
async def test_accept_skill_tweak_copies_to_custom(evalapp, web_client,
                                                   tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom-skills")
    builtin = tmp_path / "skills" / "tdd"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text("# TDD\nwrite tests first\n",
                                      encoding="utf-8")
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "skills")
    app, *_ = evalapp
    p = _proposal("skill-tweak", target="tdd", fix="demand a failing test first")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "skill", body
        text = (tmp_path / "custom-skills" / "tdd" / "SKILL.md").read_text()
        assert "write tests first" in text          # builtin copied down
        assert "demand a failing test first" in text
        assert "<!-- eval-proposals -->" in text
        assert "demand a failing" not in (builtin / "SKILL.md").read_text()


@pytest.mark.asyncio
async def test_accept_skill_tweak_bad_target(evalapp, web_client):
    app, *_ = evalapp
    p = _proposal("skill-tweak", target="no-such-skill")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "none" and "no skill" in body["note"]


@pytest.mark.asyncio
async def test_accept_skill_tweak_clears_discovery_cache(evalapp, web_client,
                                                         tmp_path, monkeypatch):
    """skill.load reads through the layered discovery cache — an accepted
    tweak must drop it, or the fix only takes effect after a restart."""
    from runtime import skills as skills_mod
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom-skills")
    builtin = tmp_path / "skills" / "tdd"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text("# TDD\nwrite tests first\n",
                                      encoding="utf-8")
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "skills")
    skills_mod.discover_skills_layered_cached(tmp_path / "skills",
                                              tmp_path / "custom-skills")
    assert skills_mod._LAYERED_CACHE            # warm from the eval run
    app, *_ = evalapp
    p = _proposal("skill-tweak", target="tdd", fix="demand a failing test first")
    try:
        async with web_client(app) as c:
            r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
            assert r.json()["applied"] == "skill"
            assert not skills_mod._LAYERED_CACHE    # next load sees the tweak
    finally:
        skills_mod.skills_cache_clear()


def _register_fake_tool(app, name="fake.tool"):
    """The web_app fixture's tmp install root has no tools/ dir, so the
    registry is empty — register one by hand for tool-target tests."""
    from runtime.tool_base import Tool

    class FakeTool(Tool):
        async def execute(self, args, context):
            pass

    t = FakeTool()
    t.name = name
    t.description = "original description"
    app.state.runtime.registry.register_instance(t)
    return t


@pytest.mark.asyncio
async def test_accept_tool_description(evalapp, web_client, tmp_path,
                                       monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app, *_ = evalapp
    _register_fake_tool(app)
    p = _proposal("tool-description", target="fake.tool",
                  content="Read a file from the workspace.")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "tool", body
        from runtime import tool_overrides
        assert tool_overrides.load()["fake.tool"] == "Read a file from the workspace."
        tool = app.state.runtime.registry.get("fake.tool")
        assert tool.description == "Read a file from the workspace."


@pytest.mark.asyncio
async def test_accept_tool_description_needs_content(evalapp, web_client,
                                                     tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CUSTOM_DIR", tmp_path / "custom")
    app, *_ = evalapp
    _register_fake_tool(app)
    p = _proposal("tool-description", target="fake.tool", content=None)
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "none" and "proposed_content" in body["note"]


@pytest.mark.asyncio
async def test_accept_config_whitelisted(evalapp, web_client):
    app, *_ = evalapp
    p = _proposal("config", target="budgets.max_iterations", content="12")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "config", body
        assert app.state.runtime.config["budgets"]["max_iterations"] == 12
        bad = _proposal("config", target="trace.db_path", content="/tmp/x",
                        fix="f2")
        r = await c.post(f"/api/admin/evals/proposals/{bad['id']}/accept")
        body = r.json()
        assert body["applied"] == "none" and "whitelisted" in body["note"]


@pytest.mark.asyncio
async def test_accept_skill_tweak_broken_dir(evalapp, web_client, tmp_path,
                                             monkeypatch):
    """Audit A2: a skill dir without SKILL.md yields a friendly note, not a
    500."""
    monkeypatch.setattr(paths, "CUSTOM_SKILLS_DIR", tmp_path / "custom-skills")
    broken = tmp_path / "skills" / "broken"
    broken.mkdir(parents=True)                    # dir exists, SKILL.md doesn't
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "skills")
    app, *_ = evalapp
    p = _proposal("skill-tweak", target="broken")
    async with web_client(app) as c:
        r = await c.post(f"/api/admin/evals/proposals/{p['id']}/accept")
        body = r.json()
        assert body["applied"] == "none" and "SKILL.md" in body["note"]


# ---- benchmark ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_run_endpoint(evalapp, web_client, monkeypatch):
    app, _, builtin_evals = evalapp
    (builtin_evals / "smoke-case.yaml").write_text(CASE_YAML)
    seen = []

    async def fake_suite(runtime, cases, store, *, disabled_tools=None,
                         variant=None, progress=None, should_stop=None):
        seen.append(variant)
        if progress:
            progress(cases[0].id, {"test_id": cases[0].id})
        return {"cases": 1, "ran": 1, "passed": 1, "failed": 0,
                "cost_usd": 0.0, "results": []}

    monkeypatch.setattr(eval_runner, "run_suite", fake_suite)
    async with web_client(app) as c:
        # validation: no variants, duplicate labels, bad reps, no case selector
        assert (await c.post("/api/admin/evals/benchmark/run",
                             json={"id": "smoke-case",
                                   "variants": []})).status_code == 400
        dup = [{"label": "a", "reps": 1}, {"label": "a", "reps": 1}]
        assert (await c.post("/api/admin/evals/benchmark/run",
                             json={"id": "smoke-case",
                                   "variants": dup})).status_code == 400
        assert (await c.post("/api/admin/evals/benchmark/run",
                             json={"id": "smoke-case", "variants": [
                                 {"label": "a", "reps": 0}]})).status_code == 400
        assert (await c.post("/api/admin/evals/benchmark/run",
                             json={"variants": [
                                 {"label": "a"}]})).status_code == 400
        variants = [
            {"label": "brainA-t0", "model": "local-orchestrator",
             "sampling": {"temperature": 0, "seed": 42}, "reps": 2},
            {"label": "brainA-t07", "model": "local-orchestrator",
             "sampling": {"temperature": 0.7}, "reps": 1}]
        r = await c.post("/api/admin/evals/benchmark/run",
                         json={"id": "smoke-case", "variants": variants})
        assert r.status_code == 200, r.text
        assert r.json() == {"started": True, "cases": 1, "variants": 2,
                            "runs": 3}
        for _ in range(50):
            st = (await c.get("/api/admin/evals/run-status")).json()
            if not st["running"] and st["last"]:
                break
            await asyncio.sleep(0.1)
        assert st["running"] is False
        assert st["last"]["benchmark"] is True
        assert st["last"]["suites"] == 3
        # 2 reps of A then 1 rep of B, labels/models/sampling carried through
        assert [(v["label"], v["model"]) for v in seen] == [
            ("brainA-t0", "local-orchestrator"),
            ("brainA-t0", "local-orchestrator"),
            ("brainA-t07", "local-orchestrator")]
        assert seen[0]["sampling"] == {"temperature": 0, "seed": 42}


@pytest.mark.asyncio
async def test_benchmark_brains_and_compare(evalapp, web_client):
    app, *_ = evalapp
    s = _store()
    for label, ok, score in [("alpha", True, 9.0), ("alpha", False, 3.0),
                             ("beta", True, 7.0)]:
        row = s.record_result(test_id="smoke-case", passed=ok, score=score,
                              judge_notes="n", judge_model="m", cost_usd=0.02,
                              tokens=10, elapsed_s=2.0, status="ok",
                              run_ids=[], transcript=[], brain=label)
    s.close()
    async with web_client(app) as c:
        r = await c.get("/api/admin/evals/benchmark/brains")
        assert r.status_code == 200
        assert "alpha" in r.json()["brains"] and "beta" in r.json()["brains"]
        r = await c.get("/api/admin/evals/benchmark/compare",
                        params={"brains": "alpha,beta"})
        assert r.status_code == 200
        cases = {x["test_id"]: x for x in r.json()["cases"]}
        per = cases["smoke-case"]["per_brain"]
        assert per["alpha"]["runs"] == 2
        assert per["alpha"]["pass_rate"] == 0.5
        assert per["alpha"]["avg_score"] == 6.0
        assert per["beta"]["runs"] == 1
        assert per["beta"]["pass_rate"] == 1.0
        # no labels / too many labels → 400
        assert (await c.get("/api/admin/evals/benchmark/compare",
                            params={"brains": ""})).status_code == 400
