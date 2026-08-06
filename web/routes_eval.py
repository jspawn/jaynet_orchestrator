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
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import HTTPException

from runtime import eval_runner, paths
from runtime.eval_cases import (get_case, load_cases, parse_case,
                                validate_case_dict)
from runtime.eval_store import EvalStore
from runtime.tool_base import ToolContext
from tools.chain.engine import _NAME_OK
from tools.llm.cloud_models import _call_via_litellm
from web.models import (EvalDraftRequest, EvalPutRequest, EvalRunRequest,
                        EvalValidateRequest)
from web.routes_studio import _strip_fence

# Drafts never leave the box: fixed LOCAL alias, never a request parameter.
_DRAFT_ALIAS = "local-orchestrator"

# One suite at a time (an asyncio.Lock, not threads — runs are in-process
# coroutines); the last summary stays in memory for the run-status poll.
_RUN_LOCK = asyncio.Lock()
_SUITE_STATE: dict = {"running": False, "current": None, "last": None}

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
    must_use_tools: [web.search]
    must_not_use_tools: [llm.call]
    answer_contains_any: ["2026"]
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
                "last": _SUITE_STATE["last"]}

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
    async def eval_trend(case_id: str):
        _check_id(case_id)
        store = _store()
        try:
            trend = store.trend(case_id)
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
    async def eval_stats(days: int = 30):
        if days < 0 or days > 3650:
            raise HTTPException(status_code=400,
                                detail="days must be between 0 (all time) "
                                       "and 3650")
        since = None if days == 0 else time.time() - days * 86400
        store = _store()
        try:
            out = {"days": days, "kpis": store.kpis(since),
                   "per_case": store.per_case_stats(since),
                   "series": store.series(since)}
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
    @app.post("/api/admin/evals/run")
    async def eval_run(req: EvalRunRequest):
        case_id = (req.id or "").strip()
        tag = (req.tag or "").strip()
        if bool(case_id) == bool(tag):
            raise HTTPException(status_code=400,
                                detail="pass exactly one of id (one case) or "
                                       "tag (bulk)")
        if case_id:
            case = get_case(case_id)
            if case is None:
                raise HTTPException(status_code=404,
                                    detail=f"no eval case '{case_id}'")
            cases = [case]
        else:
            cases = [c for c in load_cases() if tag in c.tags]
            if not cases:
                raise HTTPException(status_code=404,
                                    detail=f"no eval cases tagged '{tag}'")
        if _RUN_LOCK.locked():
            raise HTTPException(status_code=409,
                                detail="an eval suite is already running")

        async def _job():
            async with _RUN_LOCK:
                _SUITE_STATE.update(running=True, current=cases[0].id,
                                    last=None)
                store = _store()
                try:
                    def progress(cid, row):
                        _SUITE_STATE["current"] = cid
                    summary = await eval_runner.run_suite(
                        runtime, cases, store,
                        disabled_tools=set(users.get_global_disabled_tools()),
                        progress=progress)
                    _SUITE_STATE["last"] = summary
                finally:
                    store.close()
                    _SUITE_STATE.update(running=False, current=None)

        asyncio.create_task(_job())
        return {"started": True, "cases": len(cases)}

    # ---- proposals inbox (gated improvement loop) ----
    def _apply_prompt_tweak(prop: dict) -> str:
        gate = paths.HOME / "prompts" / "orchestrator-gate.md"
        gate.parent.mkdir(parents=True, exist_ok=True)
        text = gate.read_text(encoding="utf-8", errors="replace") \
            if gate.is_file() else ""
        if _PROPOSALS_MARKER not in text:
            text = text.rstrip() + "\n\n" + _PROPOSALS_MARKER + "\n"
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        text = (text.rstrip() + f"\n- {date} [{prop['test_id']}] "
                f"{prop['fix']}\n")
        gate.write_text(text, encoding="utf-8")
        return str(gate)

    def _write_issue(prop: dict) -> str:
        d = paths.DATA / "eval-issues"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{prop['id']}-{prop['test_id']}.md"
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        if prop["classification"] == "prompt-tweak":
            applied, path = "prompt", _apply_prompt_tweak(prop)
        elif prop["classification"] == "bug-for-dev":
            applied, path = "issue", _write_issue(prop)
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
        ecfg = eval_runner.config(runtime.config)
        res = await eval_runner._model_text(
            runtime.config, str(ecfg["judge_model"]),
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n".join(lines)}],
            temperature=0.2, want_json=False)
        if res["status"] != "ok":
            raise HTTPException(status_code=502,
                                detail=f"draft via {res['model_name']} failed: "
                                       f"{res['error'] or 'unknown error'}")
        text = _strip_fence(res["content"])
        errors = _validate_yaml("case", text)
        suggested = f"flag-{flag_id[:8]}"
        try:
            case = parse_case(suggested, text, "custom")
            suggested = case.id
        except ValueError:
            pass
        return {"yaml": text, "suggested_id": suggested,
                "ok": not errors, "errors": errors}
