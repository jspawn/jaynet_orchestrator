"""Eval harness admin routes: CRUD for eval cases (custom layer), AI drafting
(local model only, same constraint as Studio drafts), background runs,
results/trends and the gated improvement-proposals inbox.

Same shape as web/routes_studio.py — the admin gate is the auth middleware in
web/server.py; no per-route decorator. Custom cases live in
$ORCH_DATA/custom/evals, layered over the repo evals/ seeds (custom wins on id
clash; builtins are never written or deleted through here). All runtime.paths
lookups happen AT CALL TIME so tests can point the area at tmp dirs.

Nothing auto-applies: accepting a proposal is an explicit admin click, and the
side effects (a dated bullet in the git-managed gate prompt, a ready-to-paste
issue file) leave a review trail.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import HTTPException, Request

from runtime import eval_runner, paths
from runtime.eval_cases import get_case, load_cases, parse_case, validate_case_dict
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolContext
from tools.chain.engine import _NAME_OK
from tools.llm.cloud_models import _call_via_litellm, valid_model_names
from web.models import (
    EvalBenchmarkRequest,
    EvalDraftRequest,
    EvalPutRequest,
    EvalRunRequest,
    EvalScheduleRequest,
    EvalValidateRequest,
)
from web.routes_studio import _strip_fence

# Drafts never leave the box: fixed LOCAL alias, never a request parameter.
_DRAFT_ALIAS = "local-orchestrator"

# One suite at a time (an asyncio.Lock, not threads — runs are in-process
# coroutines); the last summary stays in memory for the run-status poll.
# `cancelling` is the admin-cancel flag, polled between cases by the runner.
_RUN_LOCK = asyncio.Lock()
_SUITE_STATE: dict = {"running": False, "current": None, "last": None,
                      "cancelling": False}

_PRIV_CAP = 2000        # chars of private context handed to the test-drafter
_PROPOSALS_MARKER = "<!-- eval-proposals -->"

# Compact schema spec for the drafting system prompts (kept in sync with
# runtime/eval_cases.py: validate_case_dict is the source of truth).
_CASE_SPEC = """\
An eval case is one YAML file:
  id: my-case                 # letters/digits/dash/underscore
  name: short human title
  tags: [web, freshness]      # bulk-run by tag
  driver: scripted            # scripted | adaptive (a driver model writes follow-ups)
  turns:
    - user: "first user message"
    - user: "follow-up question"
  expect:                     # deterministic checks, all optional
    must_use_tools: [web.search]          # every listed tool must be called
    must_use_any_tools: [code.run, code.execute]  # at least one of them
    must_not_use_tools: [llm.call]
    answer_contains_any: ["{year}"]  # {year}/{next_year} auto-substituted
    max_iterations: 10        # per harness turn
    ask_reply: "yes, proceed" # canned answer for ask.user cards
  judge_rubric: |
    Pass if the answer is current and sources are cited.
Every case needs a name, at least one turn, and a judge_rubric (the judge
grades the transcript against it). `expect` keys beyond the five listed are
rejected. Scenarios are non-private by construction: never reference real
user data."""


def register(app, s):
    runtime = s.runtime
    users = s.users
    flags = s.flags
    reports = s.reports
    # Agent-initiated eval.run (tools/eval/run.py) must honour the same
    # global disabled list as the admin route — tools/ can't import web/,
    # so the runner calls back through this hook (audit B5).
    eval_runner.set_disabled_hook(users.get_global_disabled_tools)

    def _store() -> EvalStore:
        return EvalStore(paths.EVAL_DB)

    def _builtin_file(case_id: str) -> Path:
        return paths.HOME / "evals" / f"{case_id}.yaml"

    def _custom_file(case_id: str) -> Path:
        return paths.CUSTOM_EVALS_DIR / f"{case_id}.yaml"

    def _check_id(case_id: str) -> None:
        if not _NAME_OK.match(case_id or ""):
            raise HTTPException(status_code=400,
                                detail=f"invalid case id '{case_id}' (letters, "
                                       f"digits, dash, underscore)")

    def _validate_yaml(fallback_id: str, text: str) -> list[str]:
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return [f"not valid YAML: {e}"]
        return validate_case_dict(fallback_id, raw)

    # ---- list + read (fixed paths before /{case_id}) ----
    @app.get("/api/admin/evals")
    async def eval_list():
        store = _store()
        try:
            latest = store.latest_by_test()
        finally:
            store.close()
        out = []
        for c in load_cases():
            row = {"id": c.id, "name": c.name, "tags": c.tags,
                   "driver": c.driver, "origin": c.origin}
            last = latest.get(c.id)
            if last:
                row["last"] = {"passed": bool(last["passed"]),
                               "score": last["score"], "ts": last["ts"]}
            out.append(row)
        return {"cases": out}

    @app.get("/api/admin/evals/run-status")
    async def eval_run_status():
        return {"running": _SUITE_STATE["running"],
                "current": _SUITE_STATE["current"],
                "last": _SUITE_STATE["last"],
                "cancelling": _SUITE_STATE["cancelling"]}

    @app.post("/api/admin/evals/cancel")
    async def eval_cancel():
        """Stop the running suite/benchmark after the current case. The case
        in flight finishes (and is recorded); every later case is skipped."""
        if not _SUITE_STATE["running"]:
            raise HTTPException(status_code=409,
                                detail="no eval suite is running")
        _SUITE_STATE["cancelling"] = True
        return {"ok": True}

    @app.get("/api/admin/evals/results")
    async def eval_results(test_id: str | None = None, limit: int = 50):
        store = _store()
        try:
            rows = store.results(test_id or None, limit)
        finally:
            store.close()
        for r in rows:      # the transcript blob is for the detail view, not lists
            r.pop("transcript", None)
        return {"results": rows}

    @app.get("/api/admin/evals/trend/{case_id}")
    async def eval_trend(case_id: str, brain: str | None = None):
        _check_id(case_id)
        if brain is not None and not _NAME_OK.match(brain):
            raise HTTPException(status_code=400,
                                detail=f"invalid brain label '{brain}'")
        store = _store()
        try:
            trend = store.trend(case_id, brain=brain or None)
        finally:
            store.close()
        return {"trend": trend}

    @app.get("/api/admin/evals/proposals")
    async def eval_proposals(status: str | None = None):
        store = _store()
        try:
            props = store.proposals(status or None)
        finally:
            store.close()
        return {"proposals": props}

    # ---- statistics (literal paths stay above /{case_id}) ----
    @app.get("/api/admin/evals/stats")
    async def eval_stats(days: int = 30, brain: str | None = None):
        if days < 0 or days > 3650:
            raise HTTPException(status_code=400,
                                detail="days must be between 0 (all time) "
                                       "and 3650")
        if brain is not None and not _NAME_OK.match(brain):
            raise HTTPException(status_code=400,
                                detail=f"invalid brain label '{brain}'")
        brain = brain or None
        since = None if days == 0 else time.time() - days * 86400
        store = _store()
        try:
            out = {"days": days, "brain": brain,
                   "kpis": store.kpis(since, brain=brain),
                   "per_case": store.per_case_stats(since, brain=brain),
                   "series": store.series(since, brain=brain),
                   "versions": store.versions()}
        finally:
            store.close()
        return out

    def _parse_ts(value: str, name: str, end_of_day: bool = False) -> float:
        """Epoch float or YYYY-MM-DD (local); dates given as a window END are
        inclusive of the whole day."""
        v = (value or "").strip()
        try:
            return float(v)
        except ValueError:
            pass
        try:
            ts = datetime.strptime(v, "%Y-%m-%d").timestamp()
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"{name}: expected an epoch timestamp "
                                       f"or YYYY-MM-DD, got '{value}'")
        return ts + 86399.999 if end_of_day else ts

    @app.get("/api/admin/evals/compare")
    async def eval_compare(a_from: str, a_to: str, b_from: str, b_to: str):
        af = _parse_ts(a_from, "a_from")
        at = _parse_ts(a_to, "a_to", end_of_day=True)
        bf = _parse_ts(b_from, "b_from")
        bt = _parse_ts(b_to, "b_to", end_of_day=True)
        store = _store()
        try:
            rows = store.compare(af, at, bf, bt)
        finally:
            store.close()
        return {"a": {"from": af, "to": at}, "b": {"from": bf, "to": bt},
                "cases": rows}

    # ---- scheduled suite runs (literal paths stay above /{case_id}) ----
    _SCHED_MIN_S, _SCHED_MAX_S = 3600, 30 * 86400     # 1 h … 30 d

    def _resolve_selector(selector: str) -> list:
        """'case:<id>' → that one case; 'tag:<tag>' → every case carrying it.
        Anything else is a 400, an unresolvable one a 404 (via _resolve_cases)."""
        kind, _, value = (selector or "").partition(":")
        if kind == "case":
            return _resolve_cases(value.strip(), "")
        if kind == "tag":
            return _resolve_cases("", value.strip())
        raise HTTPException(status_code=400,
                            detail="selector must be 'case:<id>' or 'tag:<tag>'")

    @app.get("/api/admin/evals/schedules")
    async def eval_schedules_list():
        store = _store()
        try:
            return {"schedules": store.schedules()}
        finally:
            store.close()

    @app.post("/api/admin/evals/schedules")
    async def eval_schedules_add(req: EvalScheduleRequest):
        every_s = int(req.every_hours * 3600)
        if not _SCHED_MIN_S <= every_s <= _SCHED_MAX_S:
            raise HTTPException(status_code=400,
                                detail="every_hours must be 1-720")
        # refuse a selector that resolves to nothing (a typo would silently
        # never fire, or blow up unattended every interval)
        cases = _resolve_selector(req.selector)
        store = _store()
        try:
            row = store.add_schedule(selector=req.selector, every_s=every_s)
        finally:
            store.close()
        return {"schedule": row, "cases": len(cases)}

    @app.delete("/api/admin/evals/schedules/{sid}")
    async def eval_schedules_delete(sid: str):
        store = _store()
        try:
            if not store.delete_schedule(sid):
                raise HTTPException(status_code=404,
                                    detail=f"no eval schedule '{sid}'")
        finally:
            store.close()
        return {"ok": True}

    @app.put("/api/admin/evals/schedules/{sid}")
    async def eval_schedules_toggle(sid: str, request: Request):
        body = await request.json()
        store = _store()
        try:
            row = store.set_schedule_enabled(sid, bool(body.get("enabled")))
        finally:
            store.close()
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"no eval schedule '{sid}'")
        return {"schedule": row}

    @app.get("/api/admin/evals/{case_id}")
    async def eval_get(case_id: str):
        _check_id(case_id)
        custom = _custom_file(case_id)
        path = custom if custom.is_file() else _builtin_file(case_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no eval case '{case_id}'")
        case = get_case(case_id)
        body = case.to_dict() if case else {"id": case_id}
        body["yaml"] = path.read_text(encoding="utf-8", errors="replace")
        body["origin"] = "custom" if path == custom else "builtin"
        return body

    # ---- create / update (custom layer only) ----
    @app.put("/api/admin/evals/{case_id}")
    async def eval_put(case_id: str, req: EvalPutRequest):
        _check_id(case_id)
        errors = _validate_yaml(case_id, req.yaml)
        try:
            raw = yaml.safe_load(req.yaml)
        except yaml.YAMLError:
            raw = None
        if isinstance(raw, dict) and raw.get("id") and str(raw["id"]) != case_id:
            errors.append(f"body id '{raw['id']}' does not match the path id "
                          f"'{case_id}'")
        if errors:
            raise HTTPException(status_code=400, detail={"errors": errors})
        d = paths.CUSTOM_EVALS_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{case_id}.yaml").write_text(req.yaml, encoding="utf-8")
        return {"ok": True}

    @app.delete("/api/admin/evals/{case_id}")
    async def eval_delete(case_id: str):
        _check_id(case_id)
        custom = _custom_file(case_id)
        if not custom.is_file():
            if _builtin_file(case_id).is_file():
                raise HTTPException(status_code=403,
                                    detail=f"eval case '{case_id}' is builtin — "
                                           f"only custom cases can be deleted "
                                           f"(duplicate it first)")
            raise HTTPException(status_code=404,
                                detail=f"no eval case '{case_id}'")
        custom.unlink()
        return {"ok": True}

    # ---- validate without writing ----
    @app.post("/api/admin/evals/validate")
    async def eval_validate(req: EvalValidateRequest):
        try:
            raw = yaml.safe_load(req.yaml)
        except yaml.YAMLError:
            raw = None
        fallback = str(raw.get("id")) if isinstance(raw, dict) and raw.get("id") \
            else "case"
        errors = _validate_yaml(fallback, req.yaml)
        return {"ok": not errors, "errors": errors}

    # ---- draft with AI (LOCAL model only) ----
    @app.post("/api/admin/evals/draft")
    async def eval_draft(req: EvalDraftRequest):
        if not (req.prompt or "").strip():
            raise HTTPException(status_code=400,
                                detail="prompt may not be empty")
        system = ("You are drafting a new behavioural eval case for the JayNet "
                  "orchestrator. Output ONLY the case's YAML — no prose, no "
                  "explanation, no markdown code fences.\n\n"
                  "## Target format\n" + _CASE_SPEC)
        ctx = ToolContext(request_id="eval-draft", config=runtime.config,
                          budget=None)
        res = await _call_via_litellm(_DRAFT_ALIAS, req.prompt, None,
                                      system, False, None, ctx)
        if res.status != "ok":
            raise HTTPException(status_code=502,
                                detail=f"draft via {_DRAFT_ALIAS} failed: "
                                       f"{res.error or 'unknown error'}")
        text = _strip_fence(str(res.result or ""))
        # An invalid draft is not an error — the admin fixes it in the editor.
        errors = _validate_yaml("case", text)
        return {"yaml": text, "ok": not errors, "errors": errors}

    # ---- run cases in the background ----
    def _resolve_cases(case_id: str, tag: str) -> list:
        """id runs one case, tag runs every case carrying it — exactly one."""
        if bool(case_id) == bool(tag):
            raise HTTPException(status_code=400,
                                detail="pass exactly one of id (one case) or "
                                       "tag (bulk)")
        if case_id:
            case = get_case(case_id)
            if case is None:
                raise HTTPException(status_code=404,
                                    detail=f"no eval case '{case_id}'")
            return [case]
        cases = [c for c in load_cases() if tag in c.tags]
        if not cases:
            raise HTTPException(status_code=404,
                                detail=f"no eval cases tagged '{tag}'")
        return cases

    async def _acquire_suite_lock():
        """409 when busy, else hold the lock BEFORE returning — checking
        locked() and letting the background task acquire it races (two
        back-to-back POSTs both pass). Also arms a fresh cancel flag."""
        if _RUN_LOCK.locked():
            raise HTTPException(status_code=409,
                                detail="an eval suite is already running")
        await _RUN_LOCK.acquire()
        _SUITE_STATE["cancelling"] = False

    def _release_suite_lock():
        _SUITE_STATE.update(running=False, current=None, cancelling=False)
        _RUN_LOCK.release()

    async def _suite_job(cases: list) -> None:
        """Suite job body shared by the admin Run button and scheduled fires.
        The CALLER holds the run lock (released here either way)."""
        _SUITE_STATE.update(running=True, current=cases[0].id, last=None)
        try:
            store = _store()
            try:
                def progress(cid, row):
                    _SUITE_STATE["current"] = cid
                summary = await eval_runner.run_suite(
                    runtime, cases, store,
                    disabled_tools=set(users.get_global_disabled_tools()),
                    progress=progress,
                    should_stop=lambda: bool(_SUITE_STATE["cancelling"]))
                # run-status is polled every few seconds — keep the
                # payload at aggregate size, transcripts stay in eval.db
                summary.pop("results", None)
                _SUITE_STATE["last"] = summary
            finally:
                store.close()
        finally:
            _release_suite_lock()

    @app.post("/api/admin/evals/run")
    async def eval_run(req: EvalRunRequest):
        cases = _resolve_cases((req.id or "").strip(), (req.tag or "").strip())
        await _acquire_suite_lock()
        try:
            asyncio.create_task(_suite_job(cases))
        except Exception:
            _RUN_LOCK.release()
            raise
        return {"started": True, "cases": len(cases)}

    # ---- scheduled-suite ticker (the endpoints live above /{case_id}) ----
    async def _eval_sched_tick() -> None:
        """Fire due scheduled suites. Skips while any suite runs (admin Run,
        benchmark, or an earlier fire) — the entry stays due and retries next
        tick. Marked fired BEFORE the run: a crash is at-most-once, never a
        double suite."""
        store = _store()
        try:
            due = store.due_schedules()
        finally:
            store.close()
        for entry in due[:2]:
            if _RUN_LOCK.locked():
                continue
            try:
                cases = _resolve_selector(entry["selector"])
            except HTTPException:
                # selector went stale (case deleted/renamed) — disable the
                # schedule instead of erroring every tick
                store = _store()
                try:
                    store.set_schedule_enabled(entry["id"], False)
                finally:
                    store.close()
                continue
            await _RUN_LOCK.acquire()     # uncontested: completes inline
            _SUITE_STATE["cancelling"] = False
            store = _store()
            try:
                store.mark_schedule_fired(entry["id"])
            finally:
                store.close()
            asyncio.create_task(_suite_job(cases))

    s.eval_sched_tick = _eval_sched_tick   # tests drive the tick directly
    app.state.eval_sched_tick = _eval_sched_tick

    async def _start_eval_scheduler() -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(60)
                try:
                    await _eval_sched_tick()
                except Exception as e:
                    print(f"[eval-scheduler] tick failed: {e}")
        asyncio.create_task(loop())
        print("[eval-scheduler] enabled (tick 60s)")

    s.startup_hooks.append(_start_eval_scheduler)

    # ---- benchmark: the same suite under N variants (model × sampling) ----
    _BM_MAX_VARIANTS = 8
    _BM_MAX_TOTAL_RUNS = 150

    @app.get("/api/admin/evals/benchmark/brains")
    async def eval_benchmark_brains():
        store = _store()
        try:
            return {"brains": store.brains()}
        finally:
            store.close()

    @app.get("/api/admin/evals/benchmark/compare")
    async def eval_benchmark_compare(brains: str, days: int = 0):
        labels = [b.strip() for b in (brains or "").split(",") if b.strip()]
        if not labels or len(labels) > _BM_MAX_VARIANTS:
            raise HTTPException(status_code=400,
                                detail=f"pass 1-{_BM_MAX_VARIANTS} comma-"
                                       "separated brain labels")
        if days < 0 or days > 3650:
            raise HTTPException(status_code=400,
                                detail="days must be between 0 (all time) "
                                       "and 3650")
        since = None if days == 0 else time.time() - days * 86400
        store = _store()
        try:
            rows = store.compare_brains(labels, since)
        finally:
            store.close()
        return {"brains": labels, "cases": rows}

    @app.post("/api/admin/evals/benchmark/run")
    async def eval_benchmark_run(req: EvalBenchmarkRequest):
        cases = _resolve_cases((req.id or "").strip(), (req.tag or "").strip())
        known_aliases = None
        variants = []
        labels = set()
        for v in req.variants:
            label = (v.label or "").strip()
            # the compare endpoint transports labels comma-separated — the
            # case-id charset keeps every label selectable
            if not _NAME_OK.match(label):
                raise HTTPException(status_code=400,
                                    detail=f"invalid variant label '{label}' "
                                           "(letters, digits, dash, underscore)")
            if label in labels:
                raise HTTPException(status_code=400,
                                    detail=f"duplicate variant label '{label}'")
            if not 1 <= v.reps <= 10:
                raise HTTPException(status_code=400,
                                    detail="reps must be 1-10")
            if v.sampling is not None and not isinstance(v.sampling, dict):
                raise HTTPException(status_code=400,
                                    detail="sampling must be an object, e.g. "
                                           '{"temperature": 0, "seed": 42}')
            model = (v.model or "").strip() or None
            if model is not None:
                # a typo'd alias would burn every rep and record nothing
                if known_aliases is None:
                    known_aliases = set(valid_model_names(runtime.config))
                if model not in known_aliases:
                    raise HTTPException(
                        status_code=400,
                        detail=f"unknown model '{model}'. valid: "
                               f"{', '.join(sorted(known_aliases))}")
            labels.add(label)
            variants.append({"label": label, "model": model,
                             "sampling": v.sampling, "reps": v.reps})
        if not variants or len(variants) > _BM_MAX_VARIANTS:
            raise HTTPException(status_code=400,
                                detail=f"pass 1-{_BM_MAX_VARIANTS} variants")
        total = len(cases) * sum(v.reps for v in req.variants)
        if total > _BM_MAX_TOTAL_RUNS:
            raise HTTPException(status_code=400,
                                detail=f"{total} runs requested — cap is "
                                       f"{_BM_MAX_TOTAL_RUNS}; trim cases, "
                                       "variants or reps")
        await _acquire_suite_lock()

        async def _job():
            _SUITE_STATE.update(running=True, current=cases[0].id, last=None)
            try:
                store = _store()
                try:
                    # one ceiling across ALL suites of the benchmark — each
                    # suite has its own cap, but 80 cloud suites would still
                    # be real money without a benchmark-level stop
                    cap = float(eval_runner.config(runtime.config)
                                ["benchmark_max_cost_usd"])
                    spent = 0.0
                    stopped_early = False
                    cancelled = False
                    suites = []
                    for v in variants:
                        for rep in range(v["reps"]):
                            if _SUITE_STATE["cancelling"]:
                                cancelled = True
                                break
                            if spent >= cap:
                                stopped_early = True
                                break
                            tag_progress = (
                                f"{v['label']} rep {rep + 1}")

                            def progress(cid, row, t=tag_progress):
                                _SUITE_STATE["current"] = f"{t}: {cid}"
                            summary = await eval_runner.run_suite(
                                runtime, cases, store,
                                disabled_tools=set(
                                    users.get_global_disabled_tools()),
                                variant=v, progress=progress,
                                should_stop=lambda: bool(
                                    _SUITE_STATE["cancelling"]))
                            summary.pop("results", None)
                            spent += float(summary.get("cost_usd") or 0)
                            suites.append({"label": v["label"],
                                           "rep": rep + 1, **summary})
                        if stopped_early or cancelled:
                            break
                    _SUITE_STATE["last"] = {
                        "benchmark": True, "cases": len(cases),
                        "suites": len(suites),
                        "passed": sum(s["passed"] for s in suites),
                        "failed": sum(s["failed"] for s in suites),
                        "stopped_early": stopped_early,
                        "cancelled": cancelled,
                        "cost_usd": round(sum(s["cost_usd"]
                                              for s in suites), 6)}
                finally:
                    store.close()
            finally:
                _release_suite_lock()

        try:
            asyncio.create_task(_job())
        except Exception:
            _RUN_LOCK.release()
            raise
        return {"started": True, "cases": len(cases),
                "variants": len(variants), "runs": total}

    # ---- proposals inbox (gated improvement loop) ----
    _TWEAK_CAP = 5            # accepted tweak bullets per artifact, then consolidate

    def _count_bullets(text: str) -> int:
        if _PROPOSALS_MARKER not in text:
            return 0
        section = text.split(_PROPOSALS_MARKER, 1)[1]
        return sum(1 for ln in section.splitlines() if ln.startswith("- "))

    def _apply_prompt_tweak(prop: dict) -> str:
        from runtime import gate_prompt
        # Accepted tweaks extend the live overlay, never the git-managed
        # shipped prompt (deploys stay conflict-free).
        overlay = gate_prompt.overlay_path(runtime.config)
        if overlay.is_file():
            text = overlay.read_text(encoding="utf-8", errors="replace")
        else:
            text = gate_prompt.shipped_path(runtime.config,
                                            runtime.config_path) \
                .read_text(encoding="utf-8", errors="replace")
        if _count_bullets(text) >= _TWEAK_CAP:
            raise ValueError(
                f"prompt already carries {_TWEAK_CAP} accepted tweaks — "
                "consolidate them into the prose first")
        if _PROPOSALS_MARKER not in text:
            text = text.rstrip() + "\n\n" + _PROPOSALS_MARKER + "\n"
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        text = (text.rstrip() + f"\n- {date} [{prop['test_id']}] "
                f"{prop['fix']}\n")
        gate_prompt.save_overlay(runtime.config, text)
        # Take effect immediately: the runtime caches the prompt at boot
        # (audit B3) — mirror the admin Prompt tab's apply path.
        runtime.system_prompt = text
        return str(overlay)

    def _apply_skill_tweak(prop: dict) -> str:
        """Append the tweak to the skill's custom-layer copy (copying the
        builtin skill down first if untouched) — same overlay philosophy as
        the gate prompt. Takes effect on the next skill.load."""
        name = (prop.get("target") or "").strip()
        if not _NAME_OK.match(name):
            raise ValueError(f"skill-tweak needs a valid skill name as target "
                             f"(got '{name or '—'}')")
        import shutil
        builtin = paths.SKILLS_DIR / name
        custom = paths.CUSTOM_SKILLS_DIR / name
        if not custom.is_dir():
            if not builtin.is_dir():
                raise ValueError(f"no skill '{name}' to tweak")
            custom.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(builtin, custom)
        md = custom / "SKILL.md"
        if not md.is_file():
            # A manually-broken skill dir (no SKILL.md) — a discoverable
            # skill always has one (audit A2).
            raise ValueError(f"skill '{name}' has no SKILL.md to tweak")
        text = md.read_text(encoding="utf-8", errors="replace")
        if _count_bullets(text) >= _TWEAK_CAP:
            raise ValueError(
                f"skill '{name}' already carries {_TWEAK_CAP} accepted "
                "tweaks — consolidate them into the instructions first")
        if _PROPOSALS_MARKER not in text:
            text = text.rstrip() + "\n\n" + _PROPOSALS_MARKER + "\n"
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        text = (text.rstrip() + f"\n- {date} [{prop['test_id']}] "
                f"{prop['fix']}\n")
        md.write_text(text, encoding="utf-8")
        # skill.load reads through the layered discovery cache — drop it so
        # the tweak is live on the NEXT load, not just after a restart
        # (same as the Studio skill-write path).
        from runtime.skills import skills_cache_clear
        skills_cache_clear()
        return str(md)

    def _apply_tool_description(prop: dict) -> str:
        """Replace a tool's description via the custom-layer overrides file
        (builtin tool code stays pristine); applies live immediately."""
        from runtime import tool_overrides
        name = (prop.get("target") or "").strip()
        known = {t.name for t in runtime.registry.all()}
        if name not in known:
            raise ValueError(f"tool-description needs a known tool as target "
                             f"(got '{name or '—'}')")
        content = (prop.get("proposed_content") or "").strip()
        if not content:
            raise ValueError("tool-description proposal has no "
                             "proposed_content (the replacement description)")
        ov = tool_overrides.load()
        ov[name] = content
        path = tool_overrides.save(ov)
        tool_overrides.apply(runtime.registry, ov)
        return str(path)

    # Config proposals apply through the admin override path — but only
    # behavioural knobs, never paths/secrets/tool maps.
    _CONFIG_WHITELIST = frozenset({
        "budgets.max_iterations", "budgets.max_wall_clock_s",
        "budgets.max_cost_usd", "budgets.max_total_tokens",
        "loop_guard.max_rejections", "architect.threshold",
        "eval.max_cost_usd", "eval.suite_max_cost_usd",
        "eval.adaptive_max_turns"})

    def _apply_config(prop: dict) -> str:
        import yaml as _yaml
        key = (prop.get("target") or "").strip()
        if key not in _CONFIG_WHITELIST:
            raise ValueError(
                f"config target '{key or '—'}' is not whitelisted "
                f"(one of {', '.join(sorted(_CONFIG_WHITELIST))})")
        raw = (prop.get("proposed_content") or "").strip()
        if not raw:
            raise ValueError("config proposal has no proposed_content value")
        try:
            value = _yaml.safe_load(raw)
        except _yaml.YAMLError as e:
            raise ValueError(f"proposed value is not a valid scalar: {e}")
        if isinstance(value, (dict, list)):
            raise ValueError("config proposals accept scalar values only")
        cur = users.get_config_overrides()
        cur[key] = value
        users.set_config_overrides(cur)
        parts = key.split(".")
        d = runtime.config
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = value
        return f"override {key} = {value!r}"

    def _write_issue(prop: dict) -> str:
        d = paths.DATA / "eval-issues"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{prop['id']}-{prop['test_id']}.md"
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        f.write_text(
            f"# {prop['what']}\n\n"
            f"Eval case `{prop['test_id']}` · result "
            f"#{prop.get('result_id') or '?'} · {date}\n\n"
            f"## What\n{prop['what']}\n\n"
            f"## Likely cause\n{prop['cause']}\n\n"
            f"## Suggested fix\n{prop['fix']}\n\n"
            f"---\nJudge notes: eval.db results row "
            f"#{prop.get('result_id') or '?'} (admin Eval tab / eval.report).\n",
            encoding="utf-8")
        return str(f)

    @app.post("/api/admin/evals/proposals/{pid}/accept")
    async def eval_proposal_accept(pid: int):
        store = _store()
        try:
            prop = store.get_proposal(pid)
            if prop is None:
                raise HTTPException(status_code=404,
                                    detail=f"no proposal #{pid}")
            prop = store.set_proposal_status(pid, "accepted")
        finally:
            store.close()
        applied, path = "none", None
        cls = prop["classification"]
        try:
            if cls == "prompt-tweak":
                applied, path = "prompt", _apply_prompt_tweak(prop)
            elif cls == "skill-tweak":
                applied, path = "skill", _apply_skill_tweak(prop)
            elif cls == "tool-description":
                applied, path = "tool", _apply_tool_description(prop)
            elif cls == "config":
                applied, path = "config", _apply_config(prop)
            elif cls == "bug-for-dev":
                applied, path = "issue", _write_issue(prop)
        except ValueError as e:
            # Unappliable proposal (bad target, cap reached, …): keep it
            # accepted-but-unapplied and say why.
            return {"proposal": prop, "applied": "none", "path": None,
                    "note": str(e)}
        return {"proposal": prop, "applied": applied, "path": path}

    @app.post("/api/admin/evals/proposals/{pid}/reject")
    async def eval_proposal_reject(pid: int):
        store = _store()
        try:
            prop = store.set_proposal_status(pid, "rejected")
        finally:
            store.close()
        if prop is None:
            raise HTTPException(status_code=404, detail=f"no proposal #{pid}")
        return {"proposal": prop, "applied": "none", "path": None}

    # ---- turn a user flag into a draft eval case ----
    @app.post("/api/admin/flags/{flag_id}/make-test")
    async def eval_make_test(flag_id: str):
        flag = flags.get(flag_id)
        if not flag:
            raise HTTPException(status_code=404, detail="no such flag")
        lines = [f"A user flagged a chat session as broken. "
                 f"Their comment: {flag['comment'] or '(none)'}"]
        for rep in reports.for_runs(flag["run_ids"]):
            lines.append(f"\n## Coroner report ({rep['trigger']}, "
                         f"run ended {rep['status']})\n{rep['report']}")
        # Private context ONLY on the user's explicit opt-in — and even then
        # just the message/final answer, capped.
        if flag.get("include_private") and flag["run_ids"]:
            db = runtime.config["trace"]["db_path"]
            if Path(db).exists():
                conn = sqlite3.connect(db, timeout=10)
                try:
                    q = ",".join("?" * len(flag["run_ids"]))
                    for r in conn.execute(
                            f"SELECT user_message, final_answer FROM runs "
                            f"WHERE id IN ({q})", flag["run_ids"]):
                        lines.append(f"\n## User message (private, opt-in)\n"
                                     f"{(r[0] or '')[:_PRIV_CAP]}")
                        lines.append(f"## Final answer\n{(r[1] or '')[:_PRIV_CAP]}")
                finally:
                    conn.close()
        lines.append("\nTurn this failure into a regression eval case that "
                     "would have caught it. Output ONLY the case YAML.")
        system = ("You are writing a behavioural eval case for the JayNet "
                  "orchestrator from a post-mortem. Output ONLY the case's "
                  "YAML — no prose, no explanation, no markdown code fences.\n\n"
                  "## Target format\n" + _CASE_SPEC)
        # Local-only, same posture as eval_draft (audit S1): the payload can
        # carry coroner reports and — on explicit opt-in — private chat
        # content; none of it may leave the box.
        ctx = ToolContext(request_id="eval-make-test", config=runtime.config,
                          budget=None)
        res = await _call_via_litellm(_DRAFT_ALIAS, "\n".join(lines), None,
                                      system, False, None, ctx)
        if res.status != "ok":
            raise HTTPException(status_code=502,
                                detail=f"draft via {_DRAFT_ALIAS} failed: "
                                       f"{res.error or 'unknown error'}")
        text = _strip_fence(str(res.result or ""))
        errors = _validate_yaml("case", text)
        suggested = f"flag-{flag_id[:8]}"
        try:
            case = parse_case(suggested, text, "custom")
            suggested = case.id
        except ValueError:
            pass
        return {"yaml": text, "suggested_id": suggested,
                "ok": not errors, "errors": errors}
