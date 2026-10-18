"""Admin-only, contrastive harness experiments (Propose–Probe–Compose).

A baseline and two scoped candidates play the SAME scripted eval case under
matched conditions. A composer may keep, merge or revise only the probed edit
targets; the composed overlay is probed too. Every run uses run-scoped overlays
and ``record=False``: live prompts, skills, tools, config and the ordinary eval
ledger stay untouched. Only the explicit create-proposals action feeds the
existing human-gated inbox.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import time
import uuid
from typing import Any

from runtime import eval_runner

log = logging.getLogger(__name__)

CONFIG_WHITELIST = frozenset({
    "budgets.max_iterations", "budgets.max_wall_clock_s",
    "budgets.max_cost_usd", "budgets.max_total_tokens",
    "loop_guard.max_rejections", "architect.threshold",
    "eval.max_cost_usd", "eval.suite_max_cost_usd",
    "eval.adaptive_max_turns",
})
# run_case overwrites budgets.*, reads the suite cap from the unoverlaid live
# runtime, and experiments accept scripted cases only. These are the config
# edits that can therefore change a matched experiment's behavior.
EXPERIMENT_CONFIG_WHITELIST = frozenset({
    "architect.threshold", "loop_guard.max_rejections", "eval.max_cost_usd",
})
_EXPERIMENT_CONFIG_TYPES = {
    "architect.threshold": ("int", 0, 4),
    "loop_guard.max_rejections": ("int", 0, None),
    "eval.max_cost_usd": ("float", 0, None),
}
_OVERLAY_KEYS = frozenset({
    "prompt_addition", "skill_addition", "tool_description", "config",
})
_MAX_TEXT = 3000


class ExperimentCancelled(Exception):
    """Raised between arms/repetitions when the admin requests cancellation."""


class ExperimentBudgetExceeded(Exception):
    """Raised before more work once the shared eval suite cost cap is spent."""


def _clean_text(value: Any, name: str, cap: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > cap:
        raise ValueError(f"{name} exceeds {cap} characters")
    return text


def _validate_named_edit(raw: Any, name: str, known: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    if set(raw) - {"name", "content"}:
        raise ValueError(f"{name} has unknown fields")
    target = _clean_text(raw.get("name"), f"{name}.name", 100)
    if target not in known:
        raise ValueError(f"{name} targets unknown name '{target}'")
    return {"name": target,
            "content": _clean_text(raw.get("content"), f"{name}.content")}


def _experiment_config_value(key: str, value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"config value for '{key}' must be numeric, not bool")
    if value is None or isinstance(value, (dict, list)):
        raise ValueError(f"config value for '{key}' must be a numeric scalar")
    if len(str(value)) > 200:
        raise ValueError("config value is too long")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config value for '{key}' must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"config value for '{key}' must be finite")
    kind, minimum, maximum = _EXPERIMENT_CONFIG_TYPES[key]
    if kind == "int":
        if not number.is_integer():
            raise ValueError(f"config value for '{key}' must be an integer")
        out: int | float = int(number)
    else:
        out = number
    if out < minimum or (key == "eval.max_cost_usd" and out <= 0):
        raise ValueError(f"config value for '{key}' is below its allowed range")
    if maximum is not None and out > maximum:
        raise ValueError(f"config value for '{key}' exceeds {maximum}")
    return out


def _validate_config(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"key", "value"}:
        raise ValueError("config must contain only key and value")
    key = _clean_text(raw.get("key"), "config.key", 100)
    if key not in EXPERIMENT_CONFIG_WHITELIST:
        raise ValueError(f"config key '{key}' is not whitelisted for experiments")
    return {"key": key, "value": _experiment_config_value(key, raw.get("value"))}


def validate_overlay(raw: Any, runtime, *, allow_empty: bool = True) -> dict:
    """Validate model-produced edits as data, never as arbitrary run options."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - _OVERLAY_KEYS:
        raise ValueError("overlay has unknown fields or is not an object")
    out: dict[str, Any] = {}
    if raw.get("prompt_addition"):
        out["prompt_addition"] = _clean_text(
            raw["prompt_addition"], "prompt_addition")
    if raw.get("skill_addition"):
        skills = set((getattr(runtime, "skills", None) or {}).keys())
        out["skill_addition"] = _validate_named_edit(
            raw["skill_addition"], "skill_addition", skills)
    if raw.get("tool_description"):
        tools = {t.name for t in runtime.registry.all()}
        out["tool_description"] = _validate_named_edit(
            raw["tool_description"], "tool_description", tools)
    if raw.get("config"):
        out["config"] = _validate_config(raw["config"])
    if not out and not allow_empty:
        raise ValueError("candidate must contain one concrete harness edit")
    return out


def validate_candidate(raw: Any, runtime, *, allow_empty: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("candidate must be an object")
    rationale = _clean_text(raw.get("rationale"), "candidate.rationale", 2000)
    overlay = validate_overlay(raw.get("overlay"), runtime, allow_empty=allow_empty)
    return {"rationale": rationale, "overlay": overlay}


def _set_dot(config: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    target = config
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _get_dot(config: dict, key: str) -> Any:
    target: Any = config
    for part in key.split("."):
        if not isinstance(target, dict):
            return None
        target = target.get(part)
    return target


def runtime_view(runtime, overlay: dict):
    """A shallow runtime view with isolated config and fallback telemetry."""
    view = copy.copy(runtime)
    view.config = copy.deepcopy(runtime.config)
    if overlay.get("config"):
        c = overlay["config"]
        _set_dot(view.config, c["key"], c["value"])
    # Model-client fallback entries append to this list. Give each arm its own
    # list so parallel chats and earlier arms cannot contaminate its verdict.
    view._served_model_log = []
    return view


def new_experiment(case, reps: int) -> dict:
    now = time.time()
    return {
        "id": "hx-" + uuid.uuid4().hex[:12],
        "case_id": case.id,
        "case": {"name": case.name, "tags": list(case.tags),
                 "driver": case.driver},
        "reps": reps,
        "status": "running",
        "stage": "baseline",
        "created": now,
        "updated": now,
        "baseline": {"overlay": {}, "runs": [], "summary": {}},
        "baseline_fingerprint": "",
        "baseline_artifacts": "",
        "candidates": {},
        "composed": {"rationale": "", "overlay": {}, "runs": [],
                     "summary": {}},
        "propose_meta": {},
        "compose_meta": {},
        "total": {"cost_usd": 0.0, "tokens": 0},
        "lesson": None,
        "proposal_ids": [],
        "error": None,
    }


def create_experiment(store, case, reps: int) -> dict:
    exp = new_experiment(case, reps)
    store.create_harness_experiment(exp)
    return exp


def _save(store, exp: dict, *, status: str | None = None,
          stage: str | None = None) -> None:
    if status is not None:
        exp["status"] = status
    if stage is not None:
        exp["stage"] = stage
    exp["updated"] = time.time()
    store.update_harness_experiment(exp)


def _add_usage(exp: dict, row: dict) -> None:
    total = exp["total"]
    total["cost_usd"] = round(
        float(total["cost_usd"]) + float(row.get("cost_usd") or 0), 6)
    total["tokens"] = int(total["tokens"]) + int(row.get("tokens") or 0)


def _meta_usage(exp: dict, key: str, result: dict) -> None:
    exp[key] = {
        "model": result.get("model_name"),
        "served_model": result.get("served_model"),
        "finish_reason": result.get("finish_reason"),
        "cost_usd": result.get("cost_usd") or 0,
        "tokens": result.get("tokens") or 0,
    }
    _add_usage(exp, result)


def _compact_turn(turn: dict) -> dict:
    return {
        "user": str(turn.get("user") or "")[:500],
        "status": turn.get("status"),
        "answer": str(turn.get("answer") or "")[:1200],
        "trajectory": str(turn.get("trajectory") or "")[:1600],
        "tools": list(turn.get("tools") or []),
    }


def _compact_row(row: dict) -> dict:
    keys = ("test_id", "passed", "score", "judge_notes", "judge_model",
            "cost_usd", "tokens", "elapsed_s", "status", "run_ids",
            "check_failures", "judge_error", "skipped", "note")
    out = {key: row.get(key) for key in keys if key in row}
    out["transcript"] = [_compact_turn(t) for t in row.get("transcript") or []]
    return out


def _measured_row(row: dict) -> bool:
    if (row.get("skipped") or row.get("status") not in ("ok", "mixed")
            or row.get("judge_error") or row.get("passed") is None):
        return False
    raw_score = row.get("score")
    if raw_score is None:
        return False
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return False
    return math.isfinite(score) and 0 <= score <= 10


def _require_measured(arm: dict, label: str, reps: int) -> None:
    runs = arm.get("runs") or []
    measured = sum(1 for row in runs if _measured_row(row))
    if reps < 1 or len(runs) != reps or measured != reps:
        raise ValueError(f"{label} arm lacks usable measured evidence "
                         f"({measured}/{reps} measured repetitions)")


def summarize(runs: list[dict]) -> dict:
    ran = [r for r in runs if not r.get("skipped")]
    counted = [r for r in ran if _measured_row(r)]
    scores = [float(r["score"]) for r in counted]
    return {
        "runs": len(runs),
        "ran": len(ran),
        "counted": len(counted),
        "measured": len(counted),
        "unmeasured": len(ran) - len(counted),
        "passed": sum(1 for r in counted if r.get("passed")),
        "pass_rate": (sum(1 for r in counted if r.get("passed")) / len(counted)
                      if counted else None),
        "avg_score": (sum(scores) / len(scores) if scores else None),
        "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in runs), 6),
        "tokens": sum(int(r.get("tokens") or 0) for r in runs),
        "elapsed_s": round(sum(float(r.get("elapsed_s") or 0) for r in runs), 2),
    }


def comparison(exp: dict) -> dict:
    base = exp["baseline"].get("summary") or {}
    comp = exp["composed"].get("summary") or {}
    def delta(key: str):
        a, b = base.get(key), comp.get(key)
        return round(b - a, 4) if a is not None and b is not None else None
    return {"pass_rate": delta("pass_rate"), "avg_score": delta("avg_score"),
            "tokens": int(comp.get("tokens") or 0) - int(base.get("tokens") or 0),
            "elapsed_s": round(float(comp.get("elapsed_s") or 0)
                               - float(base.get("elapsed_s") or 0), 2)}


def list_row(exp: dict) -> dict:
    return {
        "id": exp["id"], "case_id": exp["case_id"], "case": exp.get("case"),
        "reps": exp["reps"], "status": exp["status"], "stage": exp["stage"],
        "created": exp["created"], "updated": exp["updated"],
        "baseline": (exp.get("baseline") or {}).get("summary") or {},
        "composed": (exp.get("composed") or {}).get("summary") or {},
        "comparison": comparison(exp), "total": exp.get("total"),
        "proposal_ids": exp.get("proposal_ids") or [], "error": exp.get("error"),
    }


def _case_brief(case) -> str:
    return json.dumps({
        "id": case.id, "name": case.name, "tags": case.tags,
        "turns": case.turns, "expect": case.expect,
        "judge_rubric": case.judge_rubric,
    }, ensure_ascii=False, default=str)[:12000]


def _digest_row(row: dict) -> str:
    lines = [f"passed={row.get('passed')} score={row.get('score')} "
             f"status={row.get('status')} notes={row.get('judge_notes')}"]
    for turn in row.get("transcript") or []:
        lines.append("USER: " + str(turn.get("user") or "")[:300])
        lines.append("TOOLS: " + str(turn.get("trajectory") or "")[:1200])
        lines.append("ANSWER: " + str(turn.get("answer") or "")[:800])
    if row.get("check_failures"):
        lines.append("CHECK FAILURES: " + str(row["check_failures"])[:1000])
    return "\n".join(lines)[:7000]


def _digest_runs(runs: list[dict]) -> str:
    return "\n\n--- repetition ---\n\n".join(
        _digest_row(row) for row in runs)[:18000] or "(no executions)"


def _artifact_snapshot(runtime, baseline_runs: list[dict]) -> str:
    called = {tool for row in baseline_runs for turn in row.get("transcript") or []
              for tool in turn.get("tools") or []}
    descriptions = {}
    for tool in runtime.registry.all():
        if tool.name in called:
            descriptions[tool.name] = tool.description
    skills = [{"name": name, "description": str(s.get("description") or "")[:300]}
              for name, s in sorted((getattr(runtime, "skills", None) or {}).items())]
    config = {key: _get_dot(runtime.config, key)
              for key in sorted(EXPERIMENT_CONFIG_WHITELIST)}
    return json.dumps({
        "system_prompt": str(getattr(runtime, "system_prompt", "") or "")[:14000],
        "skills": skills,
        "relevant_tool_descriptions": descriptions,
        "all_tool_names": sorted(t.name for t in runtime.registry.all()),
        "config": config,
    }, ensure_ascii=False, default=str)[:24000]


def _model_artifacts(runtime) -> dict:
    out = {"model": getattr(runtime, "model", None),
           "brain": eval_runner.brain_label(runtime)}
    try:
        from runtime.preset_store import resolve_slot, slot_preset_name
        from runtime.serve_preset import parse_preset
        for slot in ("brain", "specialist"):
            resolved = resolve_slot(runtime.config, slot) or {}
            out[slot] = {"name": slot_preset_name(runtime.config, slot),
                         "preset": parse_preset(resolved.get("preset") or "")}
    except Exception as exc:
        out["preset_error"] = type(exc).__name__
    return out


def _skill_artifacts(runtime) -> list[dict]:
    from runtime import paths
    from runtime import skills as skills_mod
    root = (runtime.config.get("skills") or {}).get("dir") or paths.SKILLS_DIR
    out = []
    for name, metadata in sorted((getattr(runtime, "skills", None) or {}).items()):
        try:
            loaded = skills_mod.load_skill(
                root, name, custom_dir=paths.CUSTOM_SKILLS_DIR)
        except Exception:
            loaded = None
        out.append({"name": name, "metadata": metadata, "loaded": loaded})
    return out


def _artifact_fingerprint(runtime) -> str:
    tools = []
    for tool in runtime.registry.all():
        try:
            schema = tool.to_openai_schema()
        except Exception:
            schema = getattr(tool, "parameters", None)
        tools.append({"name": tool.name, "description": tool.description,
                      "schema": schema})
    artifacts = {
        "config": runtime.config,
        "system_prompt": getattr(runtime, "system_prompt", "") or "",
        "tools": tools,
        "skills": _skill_artifacts(runtime),
        "models": _model_artifacts(runtime),
    }
    encoded = json.dumps(artifacts, sort_keys=True, ensure_ascii=False,
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_artifacts(runtime, exp: dict) -> None:
    expected = exp.get("baseline_fingerprint")
    if not expected or expected != _artifact_fingerprint(runtime):
        raise ValueError("runtime artifacts changed during the matched experiment; "
                         "aborting to prevent unmatched arms")


_OVERLAY_SPEC = """Overlay JSON fields (all optional, at most one of each):
- prompt_addition: instruction text appended to the brain prompt.
- skill_addition: {name, content}; content is appended when that skill loads.
- tool_description: {name, content}; content is the FULL replacement description.
- config: {key, value}; key must be one of the whitelisted keys shown below.
No code, paths, secrets, guard changes, model changes, or arbitrary run options."""


async def _editor_call(runtime, system: str, user: str, *,
                       cost_budget: dict | None = None, on_result=None) -> dict:
    ecfg = eval_runner.config(runtime.config)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    result = await eval_runner._guarded_model_text(
        runtime.config, str(ecfg["judge_model"]), messages,
        temperature=0.2, want_json=True, max_tokens=12000,
        cost_budget=cost_budget)
    if on_result is not None:
        on_result(result)
    if result["status"] != "ok":
        raise ValueError(f"editor model failed: {result['error']}")
    parsed = eval_runner._parse_json(result["content"])
    if parsed is None:
        raise ValueError("editor model returned unparseable JSON")
    return {"parsed": parsed, "result": result}


async def propose(runtime, case, baseline_runs: list[dict],
                  lessons: list[dict], *, artifact_snapshot: str | None = None,
                  cost_budget: dict | None = None, on_result=None) -> tuple[dict, dict]:
    system = """You improve an agent harness through behavioral evidence.
Propose TWO minimal candidates: INVARIANT targets reusable agent discipline
(planning, delegation, verification, context use); DOMAIN targets this task's
domain procedure or tool guidance. Each candidate needs a rationale tied to a
specific baseline observation. Do not invent task facts or stack unrelated edits.
Return ONLY JSON: {"invariant": {"rationale": "...", "overlay": {}},
"domain": {"rationale": "...", "overlay": {}}}.
""" + _OVERLAY_SPEC
    snapshot = (artifact_snapshot if artifact_snapshot is not None
                else _artifact_snapshot(runtime, baseline_runs))
    user = (f"## Eval case\n{_case_brief(case)}\n\n"
            f"## Baseline executions\n{_digest_runs(baseline_runs)}\n\n"
            f"## Current artifacts\n{snapshot}\n\n"
            f"## Prior evidence-backed lessons\n"
            f"{json.dumps(lessons, ensure_ascii=False)[:6000]}\n\n"
            f"## Whitelisted config keys\n{sorted(EXPERIMENT_CONFIG_WHITELIST)}")
    called = await _editor_call(runtime, system, user,
                                cost_budget=cost_budget, on_result=on_result)
    parsed = called["parsed"]
    candidates = {
        "invariant": validate_candidate(parsed.get("invariant"), runtime),
        "domain": validate_candidate(parsed.get("domain"), runtime),
    }
    return candidates, called["result"]


def _probed_targets(candidates: dict) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {key: set() for key in _OVERLAY_KEYS}
    for candidate in candidates.values():
        overlay = candidate.get("overlay") or {}
        for key in _OVERLAY_KEYS:
            edit = overlay.get(key)
            if not edit:
                continue
            if key in ("skill_addition", "tool_description"):
                targets[key].add(str(edit["name"]))
            elif key == "config":
                targets[key].add(str(edit["key"]))
            else:
                targets[key].add("prompt")
    return targets


def _assert_previously_probed(overlay: dict, candidates: dict) -> None:
    targets = _probed_targets(candidates)
    for key in _OVERLAY_KEYS:
        edit = overlay.get(key)
        if not edit:
            continue
        target = ("prompt" if key == "prompt_addition"
                  else str(edit.get("name") if key != "config" else edit.get("key")))
        if target not in targets[key]:
            raise ValueError(f"composed {key} target '{target}' was not probed")


async def compose(runtime, case, exp: dict, *,
                  cost_budget: dict | None = None, on_result=None) -> tuple[dict, dict]:
    system = """Compose a successor harness from matched behavioral contrasts.
Keep, merge or revise ONLY edit targets that were actually probed. You may keep
the baseline by returning an empty overlay. Tie every decision to an observed
trajectory or deliverable change; a score alone is not proof. Do not introduce
new targets or presume that more instructions are better.
Return ONLY JSON: {"rationale": "...", "overlay": {}}.
""" + _OVERLAY_SPEC
    evidence = {
        "baseline": exp["baseline"],
        "candidates": exp["candidates"],
    }
    user = (f"## Eval case\n{_case_brief(case)}\n\n"
            f"## Matched arm evidence\n{json.dumps(evidence, ensure_ascii=False)[:30000]}")
    called = await _editor_call(runtime, system, user,
                                cost_budget=cost_budget, on_result=on_result)
    candidate = validate_candidate(called["parsed"], runtime, allow_empty=True)
    _assert_previously_probed(candidate["overlay"], exp["candidates"])
    return candidate, called["result"]


def _check_stop(exp: dict, should_stop) -> None:
    if should_stop is not None and should_stop():
        raise ExperimentCancelled("cancelled by admin")


def _check_budget(runtime, exp: dict) -> float:
    ecfg = eval_runner.config(runtime.config)
    cap = float(ecfg["suite_max_cost_usd"])
    remaining = cap - float(exp["total"]["cost_usd"])
    if remaining <= 0:
        raise ExperimentBudgetExceeded(f"experiment cost cap ${cap:.2f} reached")
    return remaining


async def _run_arm(runtime, case, store, exp: dict, arm: dict,
                   label: str, overlay: dict, disabled_tools,
                   progress, should_stop) -> None:
    arm["overlay"] = overlay
    arm["runs"] = []
    for rep in range(int(exp["reps"])):
        _check_stop(exp, should_stop)
        _check_artifacts(runtime, exp)
        remaining = _check_budget(runtime, exp)
        _save(store, exp, stage=f"{label} {rep + 1}/{exp['reps']}")
        if progress:
            progress(exp["stage"])
        view = runtime_view(runtime, overlay)
        case_cap = min(float(eval_runner.config(view.config)["max_cost_usd"]),
                       remaining)
        view.config.setdefault("eval", {})["max_cost_usd"] = case_cap
        cost_budget = {"cap": case_cap, "remaining": case_cap}
        row = await eval_runner.run_case(
            view, case, store, disabled_tools=disabled_tools, record=False,
            variant={"label": f"{exp['id']}-{label}",
                     "experiment_overlay": overlay,
                     "experiment_cost_budget": cost_budget})
        arm["runs"].append(_compact_row(row))
        arm["summary"] = summarize(arm["runs"])
        _add_usage(exp, row)
        _save(store, exp)
        _check_artifacts(runtime, exp)
        if float(row.get("cost_usd") or 0) > case_cap + 1e-6:
            raise ExperimentBudgetExceeded(
                f"arm '{label}' exceeded its remaining experiment cost cap")
    _require_measured(arm, label, int(exp["reps"]))


def _lesson(exp: dict) -> dict:
    composed = exp["composed"]
    return {
        "applicable_when": (f"case {exp['case_id']}; tags: "
                            + ", ".join(exp.get("case", {}).get("tags") or [])),
        "lesson": composed.get("rationale") or "No harness change was supported.",
        "evidence": {"reps": exp["reps"], "comparison": comparison(exp),
                     "baseline": exp["baseline"].get("summary"),
                     "composed": composed.get("summary")},
    }


async def run_experiment(runtime, case, store, experiment_id: str,
                         disabled_tools=None, progress=None,
                         should_stop=None) -> dict:
    exp = store.get_harness_experiment(experiment_id)
    if exp is None:
        raise ValueError(f"no harness experiment '{experiment_id}'")
    try:
        exp["baseline_fingerprint"] = _artifact_fingerprint(runtime)
        _save(store, exp)
        await _run_arm(runtime, case, store, exp, exp["baseline"], "baseline",
                       {}, disabled_tools, progress, should_stop)
        _check_stop(exp, should_stop)
        _check_artifacts(runtime, exp)
        exp["baseline_artifacts"] = _artifact_snapshot(
            runtime, exp["baseline"]["runs"])
        remaining = _check_budget(runtime, exp)
        _save(store, exp, stage="propose")
        lessons = [e["lesson"] for e in store.harness_experiments(limit=20)
                   if e["id"] != experiment_id and e.get("lesson")]
        candidates, _ = await propose(
            runtime, case, exp["baseline"]["runs"], lessons[:3],
            artifact_snapshot=exp["baseline_artifacts"],
            cost_budget={"cap": remaining, "remaining": remaining},
            on_result=lambda result: _meta_usage(exp, "propose_meta", result))
        _check_artifacts(runtime, exp)
        exp["candidates"] = {
            name: {**candidate, "runs": [], "summary": {}}
            for name, candidate in candidates.items()
        }
        _save(store, exp)
        for name in ("invariant", "domain"):
            await _run_arm(runtime, case, store, exp, exp["candidates"][name],
                           name, exp["candidates"][name]["overlay"],
                           disabled_tools, progress, should_stop)
        _check_stop(exp, should_stop)
        _check_artifacts(runtime, exp)
        remaining = _check_budget(runtime, exp)
        _save(store, exp, stage="compose")
        composed, _ = await compose(
            runtime, case, exp,
            cost_budget={"cap": remaining, "remaining": remaining},
            on_result=lambda result: _meta_usage(exp, "compose_meta", result))
        _check_artifacts(runtime, exp)
        exp["composed"].update(composed)
        _save(store, exp)
        await _run_arm(runtime, case, store, exp, exp["composed"], "composed",
                       composed["overlay"], disabled_tools, progress, should_stop)
        exp["lesson"] = _lesson(exp)
        _save(store, exp, status="done", stage="complete")
    except ExperimentCancelled as exc:
        exp["error"] = str(exc)
        _save(store, exp, status="cancelled", stage="cancelled")
    except Exception as exc:
        log.exception("harness experiment %s failed", experiment_id)
        exp["error"] = f"{type(exc).__name__}: {exc}"
        _save(store, exp, status="error", stage="failed")
    return list_row(exp)


def _verify_experiment_evidence(exp: dict) -> None:
    reps = int(exp.get("reps") or 0)
    _require_measured(exp.get("baseline") or {}, "baseline", reps)
    candidates = exp.get("candidates") or {}
    for name in ("invariant", "domain"):
        _require_measured(candidates.get(name) or {}, name, reps)
    _require_measured(exp.get("composed") or {}, "composed", reps)


def create_proposals(store, experiment_id: str) -> list[dict]:
    exp = store.get_harness_experiment(experiment_id)
    if exp is None:
        raise ValueError(f"no harness experiment '{experiment_id}'")
    if exp.get("status") != "done":
        raise ValueError("only a completed experiment can create proposals")
    if exp.get("proposal_ids"):
        raise ValueError("proposals were already created for this experiment")
    _verify_experiment_evidence(exp)
    overlay = (exp.get("composed") or {}).get("overlay") or {}
    if not overlay:
        raise ValueError("the composer kept the baseline; there is nothing to propose")
    cause = str(exp["composed"].get("rationale") or "matched experiment evidence")
    what = f"Harness experiment {experiment_id} for {exp['case_id']}"
    created: list[dict] = []
    if overlay.get("prompt_addition"):
        created.append(store.add_proposal(
            test_id=exp["case_id"], result_id=None, classification="prompt-tweak",
            target=None, proposed_content=None, what=what, cause=cause,
            fix=overlay["prompt_addition"]))
    for key, classification in (("skill_addition", "skill-tweak"),
                                ("tool_description", "tool-description")):
        edit = overlay.get(key)
        if not edit:
            continue
        created.append(store.add_proposal(
            test_id=exp["case_id"], result_id=None, classification=classification,
            target=edit["name"],
            proposed_content=edit["content"] if key == "tool_description" else None,
            what=what, cause=cause, fix=edit["content"]))
    if overlay.get("config"):
        edit = overlay["config"]
        created.append(store.add_proposal(
            test_id=exp["case_id"], result_id=None, classification="config",
            target=edit["key"], proposed_content=json.dumps(edit["value"]),
            what=what, cause=cause,
            fix=f"Set {edit['key']} to {edit['value']!r}"))
    ids = [p["id"] for p in created if p]
    exp["proposal_ids"] = ids
    _save(store, exp)
    proposals = [store.get_proposal(pid) for pid in ids]
    return [p for p in proposals if p]
