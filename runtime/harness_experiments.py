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

from runtime import brain_overlays, eval_runner

log = logging.getLogger(__name__)

CONFIG_WHITELIST = brain_overlays.CONFIG_WHITELIST
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


def validate_canonical_overlay(raw: Any, runtime, *, allow_empty: bool = True) -> dict:
    """Revalidate a carried multi-edit overlay before it can be accepted."""
    overlay = brain_overlays.clean_overlay(raw)
    skills = set((getattr(runtime, "skills", None) or {}).keys())
    tools = {t.name for t in runtime.registry.all()}
    for name in overlay["skill_additions"]:
        if name not in skills:
            raise ValueError(f"skill_additions target unknown name '{name}'")
    for name in overlay["tool_descriptions"]:
        if name not in tools:
            raise ValueError(f"tool_descriptions target unknown name '{name}'")
    for key, value in overlay["config"].items():
        if key not in EXPERIMENT_CONFIG_WHITELIST:
            raise ValueError(f"config key '{key}' is not whitelisted for experiments")
        _experiment_config_value(key, value)
    if not allow_empty and not _overlay_targets(overlay):
        raise ValueError("candidate must contain one concrete harness edit")
    return overlay


def _singular_overlay(overlay: dict) -> dict:
    """Keep the original proposal schema for a single-cycle experiment."""
    canonical = brain_overlays.clean_overlay(overlay)
    out: dict[str, Any] = {}
    if canonical["prompt_addition"]:
        out["prompt_addition"] = canonical["prompt_addition"]
    for key, singular in (("skill_additions", "skill_addition"),
                          ("tool_descriptions", "tool_description")):
        for name, content in canonical[key].items():
            out[singular] = {"name": name, "content": content}
    for key, value in canonical["config"].items():
        out["config"] = {"key": key, "value": value}
    return out


def validate_candidate(raw: Any, runtime, *, allow_empty: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("candidate must be an object")
    rationale = _clean_text(raw.get("rationale"), "candidate.rationale", 2000)
    overlay = validate_overlay(raw.get("overlay"), runtime, allow_empty=allow_empty)
    return {"rationale": rationale, "overlay": overlay}


def _get_dot(config: dict, key: str) -> Any:
    target: Any = config
    for part in key.split("."):
        if not isinstance(target, dict):
            return None
        target = target.get(part)
    return target


def runtime_view(runtime, overlay: dict):
    """A shallow runtime view with isolated config and fallback telemetry.

    Config edits are applied explicitly by the eval/run composition layer so
    the persistent brain overlay cannot overwrite the candidate being tested.
    """
    view = copy.copy(runtime)
    view.config = copy.deepcopy(runtime.config)
    # Model-client fallback entries append to this list. Give each arm its own
    # list so parallel chats and earlier arms cannot contaminate its verdict.
    view._served_model_log = []
    return view


def new_experiment(case, reps: int, steps: int = 1) -> dict:
    now = time.time()
    return {
        "id": "hx-" + uuid.uuid4().hex[:12],
        "case_id": case.id,
        "case": {"name": case.name, "tags": list(case.tags),
                 "driver": case.driver},
        "reps": reps,
        "steps": steps,
        "cycle": 1,
        "history": [],
        "base_overlay": brain_overlays.empty_overlay(),
        "brain_preset": "",
        "base_overlay_version": 0,
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


def create_experiment(store, case, reps: int, steps: int = 1) -> dict:
    exp = new_experiment(case, reps, steps)
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
    previous = exp.get(key) or {}
    exp[key] = {
        "model": result.get("model_name"),
        "served_model": result.get("served_model"),
        "finish_reason": result.get("finish_reason"),
        "cost_usd": round(float(previous.get("cost_usd") or 0)
                          + float(result.get("cost_usd") or 0), 6),
        "tokens": (int(previous.get("tokens") or 0)
                   + int(result.get("tokens") or 0)),
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


def _comparison(base: dict, comp: dict) -> dict:
    def delta(key: str):
        a, b = base.get(key), comp.get(key)
        return round(b - a, 4) if a is not None and b is not None else None
    return {"pass_rate": delta("pass_rate"), "avg_score": delta("avg_score"),
            "tokens": int(comp.get("tokens") or 0) - int(base.get("tokens") or 0),
            "elapsed_s": round(float(comp.get("elapsed_s") or 0)
                               - float(base.get("elapsed_s") or 0), 2)}


def comparison(exp: dict) -> dict:
    return _comparison(exp["baseline"].get("summary") or {},
                       exp["composed"].get("summary") or {})


def list_row(exp: dict) -> dict:
    return {
        "id": exp["id"], "case_id": exp["case_id"], "case": exp.get("case"),
        "reps": exp["reps"], "steps": int(exp.get("steps") or 1),
        "cycle": int(exp.get("cycle") or 1),
        "status": exp["status"], "stage": exp["stage"],
        "created": exp["created"], "updated": exp["updated"],
        "baseline": (exp.get("baseline") or {}).get("summary") or {},
        "composed": (exp.get("composed") or {}).get("summary") or {},
        "comparison": comparison(exp), "overall": exp.get("overall"),
        "lesson_outcome": (exp.get("lesson") or {}).get("outcome"),
        "total": exp.get("total"),
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


def _artifact_snapshot(runtime, baseline_runs: list[dict],
                       base_overlay: dict | None = None) -> str:
    called = {tool for row in baseline_runs for turn in row.get("transcript") or []
              for tool in turn.get("tools") or []}
    from runtime.preset_store import brain_overlay_for
    model = (getattr(runtime, "model", None)
             or (runtime.config.get("orchestrator") or {}).get("model"))
    overlay = brain_overlay_for(runtime.config, model)
    active = overlay["overlay"]
    carried = brain_overlays.clean_overlay(base_overlay)
    effective_overlay = brain_overlays.stack_experiment(active, carried)
    effective = brain_overlays.config_with_overlay(runtime.config, effective_overlay)
    descriptions = {}
    for tool in runtime.registry.all():
        if tool.name in called:
            descriptions[tool.name] = tool.description
    descriptions.update(effective_overlay["tool_descriptions"])
    skills = [{"name": name, "description": str(s.get("description") or "")[:300]}
              for name, s in sorted((getattr(runtime, "skills", None) or {}).items())]
    config = {key: _get_dot(effective, key)
              for key in sorted(EXPERIMENT_CONFIG_WHITELIST)}
    return json.dumps({
        "system_prompt": (str(getattr(runtime, "system_prompt", "") or "")
                          + ("\n\n" + effective_overlay["prompt_addition"]
                             if effective_overlay["prompt_addition"] else ""))[:14000],
        "brain_overlay": overlay,
        "experiment_base": carried,
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
Skill/tool names must exactly match Current artifacts; never use the eval case id.
No code, paths, secrets, guard changes, model changes, or arbitrary run options."""


async def _editor_call(runtime, system: str, user: str, *,
                       cost_budget: dict | None = None, on_result=None,
                       validate=None) -> dict:
    ecfg = eval_runner.config(runtime.config)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    for attempt in range(2):
        result = await eval_runner._guarded_model_text(
            runtime.config, str(ecfg["judge_model"]), messages,
            temperature=0.2, want_json=True, max_tokens=12000,
            cost_budget=cost_budget)
        if on_result is not None:
            on_result(result)
        if result["status"] != "ok":
            raise ValueError(f"editor model failed: {result['error']}")
        parsed = eval_runner._parse_json(result["content"])
        try:
            if not isinstance(parsed, dict):
                raise ValueError("editor model returned unparseable JSON")
            value = validate(parsed) if validate is not None else parsed
        except ValueError as exc:
            if attempt:
                raise
            messages.extend([
                {"role": "assistant", "content": result["content"]},
                {"role": "user", "content":
                    "The previous response was rejected: " + str(exc) + "\n"
                    "Correct it against the original schema and constraints. "
                    "Use only names from Current artifacts and return ONLY JSON."},
            ])
            continue
        return {"parsed": parsed, "value": value, "result": result}
    raise AssertionError("editor retry loop exhausted")


async def propose(runtime, case, baseline_runs: list[dict],
                  lessons: list[dict], *, artifact_snapshot: str | None = None,
                  cost_budget: dict | None = None, on_result=None) -> tuple[dict, dict]:
    system = """You improve an agent harness through behavioral evidence.
Propose TWO minimal candidates: INVARIANT targets reusable agent discipline
(planning, delegation, verification, context use); DOMAIN targets this task's
domain procedure or tool guidance. Each candidate needs a rationale tied to a
specific baseline observation. Reuse supported lessons only when applicable;
do not repeat regression/no_effect edits without new baseline evidence.
Do not invent task facts or stack unrelated edits.
Return ONLY JSON: {"invariant": {"rationale": "...", "overlay": {}},
"domain": {"rationale": "...", "overlay": {}}}.
""" + _OVERLAY_SPEC
    snapshot = (artifact_snapshot if artifact_snapshot is not None
                else _artifact_snapshot(runtime, baseline_runs))
    user = (f"## Eval case\n{_case_brief(case)}\n\n"
            f"## Baseline executions\n{_digest_runs(baseline_runs)}\n\n"
            f"## Current artifacts\n{snapshot}\n\n"
            f"## Relevant measured lessons (including failed edits)\n"
            f"{json.dumps(lessons, ensure_ascii=False)[:6000]}\n\n"
            f"## Whitelisted config keys\n{sorted(EXPERIMENT_CONFIG_WHITELIST)}")

    def _candidates(parsed):
        return {
            "invariant": validate_candidate(parsed.get("invariant"), runtime),
            "domain": validate_candidate(parsed.get("domain"), runtime),
        }

    called = await _editor_call(runtime, system, user, validate=_candidates,
                                cost_budget=cost_budget, on_result=on_result)
    return called["value"], called["result"]


def _probed_targets(candidates: dict) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {key: set() for key in _OVERLAY_KEYS}
    for candidate in candidates.values():
        overlay = candidate.get("edit") or candidate.get("overlay") or {}
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
Keep, merge or revise ONLY edit targets that were actually probed in this cycle.
Your overlay contains NEW changes on top of Current base overlay; inherited edits
stay in place. Return an empty overlay to keep the current harness unchanged.
Tie every decision to an observed trajectory or deliverable change; a score alone
is not proof. Do not introduce new targets or presume that more instructions are
better. Return ONLY JSON: {"rationale": "...", "overlay": {}}.
""" + _OVERLAY_SPEC
    evidence = {
        "current_base_overlay": exp.get("base_overlay") or {},
        "baseline": exp["baseline"],
        "candidates": exp["candidates"],
    }
    user = (f"## Eval case\n{_case_brief(case)}\n\n"
            f"## Current base overlay\n"
            f"{json.dumps(exp.get('base_overlay') or {}, ensure_ascii=False)}\n\n"
            f"## Matched arm evidence\n{json.dumps(evidence, ensure_ascii=False)[:30000]}")

    def _candidate(parsed):
        candidate = validate_candidate(parsed, runtime, allow_empty=True)
        _assert_previously_probed(candidate["overlay"], exp["candidates"])
        return candidate

    called = await _editor_call(runtime, system, user, validate=_candidate,
                                cost_budget=cost_budget, on_result=on_result)
    return called["value"], called["result"]


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
        case_cap = min(float(eval_runner.config(
            view.config, experiment=overlay)["max_cost_usd"]), remaining)
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


def _overlay_targets(overlay: dict) -> list[str]:
    """Stable retrieval facets for singular edits and canonical overlays."""
    targets = []
    if overlay.get("prompt_addition"):
        targets.append("prompt")
    skills = overlay.get("skill_additions") or {}
    if overlay.get("skill_addition"):
        skills = {overlay["skill_addition"].get("name"): True}
    targets.extend(f"skill:{name}" for name in sorted(skills) if name)
    tools = overlay.get("tool_descriptions") or {}
    if overlay.get("tool_description"):
        tools = {overlay["tool_description"].get("name"): True}
    targets.extend(f"tool:{name}" for name in sorted(tools) if name)
    config = overlay.get("config") or {}
    if "key" in config:
        config = {config["key"]: True}
    targets.extend(f"config:{name}" for name in sorted(config))
    return targets


def _lesson(exp: dict, *, baseline: dict | None = None,
            use_effective: bool = False) -> dict:
    baseline = baseline or exp["baseline"]
    composed = exp["composed"]
    if use_effective:
        edit = composed.get("overlay") or {}
    elif "edit" in composed:
        edit = composed.get("edit") or {}
    else:
        edit = composed.get("overlay") or {}
    targets = _overlay_targets(edit)
    evidence = _comparison(baseline.get("summary") or {},
                           composed.get("summary") or {})
    pass_delta, score_delta = evidence["pass_rate"], evidence["avg_score"]
    if not targets:
        outcome = "no_effect"
    elif (pass_delta or 0) > 0 or (
            pass_delta == 0 and (score_delta or 0) > 0):
        outcome = "supported"
    elif (pass_delta or 0) < 0 or (
            pass_delta == 0 and (score_delta or 0) < 0):
        outcome = "regression"
    else:
        outcome = "no_effect"
    rationale = composed.get("rationale") or "No harness change was supported."
    if outcome == "regression":
        rationale = "Rejected composed edit: " + rationale
    elif outcome == "no_effect":
        rationale = "No measured improvement: " + rationale
    return {
        "outcome": outcome,
        "brain_preset": exp.get("brain_preset") or "",
        "case_id": exp["case_id"],
        "tags": list(exp.get("case", {}).get("tags") or []),
        "targets": targets,
        "applicable_when": (f"case {exp['case_id']}; tags: "
                            + ", ".join(exp.get("case", {}).get("tags") or [])),
        "lesson": rationale,
        "evidence": {"reps": exp["reps"], "comparison": evidence,
                     "baseline": baseline.get("summary"),
                     "composed": composed.get("summary")},
    }


def relevant_lessons(store, case, brain: str, *, exclude_id: str | None = None,
                     limit: int = 3) -> list[dict]:
    """Retrieve measured lessons by case, tags and brain, including failures."""
    tags = set(case.tags or [])
    ranked = []
    for exp in store.harness_experiments(limit=200):
        lesson = exp.get("lesson")
        if (exp["id"] == exclude_id or exp.get("status") != "done"
                or not isinstance(lesson, dict)):
            continue
        lesson_tags = set(lesson.get("tags")
                          or (exp.get("case") or {}).get("tags") or [])
        score = 0
        if exp.get("case_id") == case.id:
            score += 8
        score += 2 * len(tags & lesson_tags)
        if brain and (lesson.get("brain_preset")
                      or exp.get("brain_preset")) == brain:
            score += 3
        if score:
            item = dict(lesson)
            item.setdefault("experiment_id", exp["id"])
            ranked.append((score, float(exp.get("updated") or 0), item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _score, _updated, item in ranked[:limit]]


def _cycle_snapshot(exp: dict) -> dict:
    keys = ("baseline", "candidates", "composed", "propose_meta",
            "compose_meta", "lesson")
    return {"cycle": exp["cycle"],
            **{key: copy.deepcopy(exp[key]) for key in keys}}


async def _run_cycle(runtime, case, store, exp: dict, lessons: list[dict],
                     disabled_tools, progress, should_stop) -> dict:
    """Run one PPC cycle on top of exp's carried, run-scoped base overlay."""
    cycle, steps = exp["cycle"], int(exp.get("steps") or 1)
    base = brain_overlays.clean_overlay(exp.get("base_overlay"))
    prefix = "" if steps == 1 else f"s{cycle}-"
    exp["baseline"] = {"overlay": base, "runs": [], "summary": {}}
    exp["candidates"] = {}
    exp["composed"] = {"rationale": "", "edit": {}, "overlay": base,
                       "runs": [], "summary": {}}
    exp["propose_meta"], exp["compose_meta"] = {}, {}
    _save(store, exp, stage=f"cycle {cycle}/{steps}: baseline")
    await _run_arm(runtime, case, store, exp, exp["baseline"],
                   prefix + "baseline", base, disabled_tools, progress, should_stop)
    _check_stop(exp, should_stop)
    _check_artifacts(runtime, exp)
    exp["baseline_artifacts"] = _artifact_snapshot(
        runtime, exp["baseline"]["runs"], base_overlay=base)
    remaining = _check_budget(runtime, exp)
    _save(store, exp, stage=f"cycle {cycle}/{steps}: propose")
    candidates, _ = await propose(
        runtime, case, exp["baseline"]["runs"], lessons,
        artifact_snapshot=exp["baseline_artifacts"],
        cost_budget={"cap": remaining, "remaining": remaining},
        on_result=lambda result: _meta_usage(exp, "propose_meta", result))
    _check_artifacts(runtime, exp)
    exp["candidates"] = {
        name: {**candidate, "edit": candidate["overlay"],
               "overlay": brain_overlays.merge_overlay(
                   base, brain_overlays.from_experiment(candidate["overlay"])),
               "runs": [], "summary": {}}
        for name, candidate in candidates.items()
    }
    _save(store, exp)
    for name in ("invariant", "domain"):
        candidate = exp["candidates"][name]
        await _run_arm(runtime, case, store, exp, candidate, prefix + name,
                       candidate["overlay"], disabled_tools, progress, should_stop)
    _check_stop(exp, should_stop)
    _check_artifacts(runtime, exp)
    remaining = _check_budget(runtime, exp)
    _save(store, exp, stage=f"cycle {cycle}/{steps}: compose")
    composed, _ = await compose(
        runtime, case, exp,
        cost_budget={"cap": remaining, "remaining": remaining},
        on_result=lambda result: _meta_usage(exp, "compose_meta", result))
    _check_artifacts(runtime, exp)
    effective = brain_overlays.merge_overlay(
        base, brain_overlays.from_experiment(composed["overlay"]))
    exp["composed"].update(rationale=composed["rationale"],
                           edit=composed["overlay"], overlay=effective)
    _save(store, exp)
    await _run_arm(runtime, case, store, exp, exp["composed"],
                   prefix + "composed", effective,
                   disabled_tools, progress, should_stop)
    exp["lesson"] = _lesson(exp)
    exp["history"].append(_cycle_snapshot(exp))
    _save(store, exp)
    return effective


async def run_experiment(runtime, case, store, experiment_id: str,
                         disabled_tools=None, progress=None,
                         should_stop=None) -> dict:
    exp = store.get_harness_experiment(experiment_id)
    if exp is None:
        raise ValueError(f"no harness experiment '{experiment_id}'")
    try:
        from runtime.preset_store import brain_overlay_for
        model = (getattr(runtime, "model", None)
                 or (runtime.config.get("orchestrator") or {}).get("model"))
        overlay = brain_overlay_for(runtime.config, model)
        exp["brain_preset"] = overlay["preset"] or eval_runner.brain_label(runtime)
        exp["base_overlay_version"] = overlay["version"]
        exp["baseline_fingerprint"] = _artifact_fingerprint(runtime)
        exp.setdefault("history", [])
        exp["base_overlay"] = brain_overlays.empty_overlay()
        _save(store, exp)
        for cycle in range(1, int(exp.get("steps") or 1) + 1):
            exp["cycle"] = cycle
            local = [item["lesson"] for item in reversed(exp["history"])
                     if item.get("lesson")]
            lessons = (local + relevant_lessons(
                store, case, exp["brain_preset"], exclude_id=experiment_id))[:3]
            exp["base_overlay"] = await _run_cycle(
                runtime, case, store, exp, lessons,
                disabled_tools, progress, should_stop)
        initial = exp["history"][0]["baseline"]
        exp["overall"] = {
            "baseline": initial.get("summary") or {},
            "composed": exp["composed"].get("summary") or {},
            "comparison": _comparison(initial.get("summary") or {},
                                      exp["composed"].get("summary") or {}),
        }
        exp["lesson"] = _lesson(exp, baseline=initial, use_effective=True)
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
    cycles = exp.get("history") or [exp]
    for cycle in cycles:
        label = f"cycle {cycle.get('cycle', 1)} "
        _require_measured(cycle.get("baseline") or {}, label + "baseline", reps)
        candidates = cycle.get("candidates") or {}
        for name in ("invariant", "domain"):
            _require_measured(candidates.get(name) or {}, label + name, reps)
        _require_measured(cycle.get("composed") or {}, label + "composed", reps)


def create_proposals(store, experiment_id: str) -> list[dict]:
    exp = store.get_harness_experiment(experiment_id)
    if exp is None:
        raise ValueError(f"no harness experiment '{experiment_id}'")
    if exp.get("status") != "done":
        raise ValueError("only a completed experiment can create proposals")
    if exp.get("proposal_ids"):
        raise ValueError("proposals were already created for this experiment")
    _verify_experiment_evidence(exp)
    lesson = exp.get("lesson") or {}
    if lesson and lesson.get("outcome") != "supported":
        raise ValueError("the composed overlay has no measured improvement; "
                         "refine it in another experiment before proposing")
    overlay = (exp.get("composed") or {}).get("overlay") or {}
    if not overlay:
        raise ValueError("the composer kept the baseline; there is nothing to propose")
    brain = str(exp.get("brain_preset") or "")
    if not brain:
        raise ValueError("experiment has no brain identity; rerun it before proposing")
    base_version = int(exp.get("base_overlay_version") or 0)
    cause = str(exp["composed"].get("rationale") or "matched experiment evidence")
    what = f"Harness experiment {experiment_id} for {exp['case_id']}"
    sequential = int(exp.get("steps") or 1) > 1
    if sequential:
        overlay = brain_overlays.clean_overlay(overlay)
    else:
        overlay = _singular_overlay(overlay)
    content = {"schema": 2 if sequential else 1, "base_version": base_version,
               "experiment_id": experiment_id, "overlay": overlay}
    proposal = store.add_proposal(
        test_id=exp["case_id"], result_id=None, classification="brain-overlay",
        target=brain, proposed_content=json.dumps(content, ensure_ascii=False),
        what=what, cause=cause,
        fix=json.dumps(overlay, ensure_ascii=False), brain_preset=brain,
        base_overlay_version=base_version)
    ids = [proposal["id"]] if proposal else []
    exp["proposal_ids"] = ids
    _save(store, exp)
    stored = store.get_proposal(ids[0]) if ids else None
    return [stored] if stored else []
