"""procedure_miner — distill reusable procedures from run history.

Step 5 of the procedure library (ToDos_for_later.md): frontier models beat
small models mostly by *process discipline*, and process is distillable.
Given a case's pass/fail history (or a flagged session), a strong judge
model extracts "what process won" and "which step the small model skips"
into ONE procedure draft — a SKILL.md with shape/checkpoints frontmatter.

Discipline (same as eval proposals): NOTHING auto-applies. The draft comes
back to the admin UI; saving it via the Studio writes it with `draft: true`,
which keeps it invisible to the model (cached skill discovery filters
drafts) until a human reviews and clears the flag.

Privacy: flag-sourced runs arrive PRE-SCRUBBED by the caller (the flags
endpoint's _scrub_payload — message/answer only when the user opted in via
include_private). Eval transcripts are sandbox artifacts, already free of
user data. The judge may be a cloud alias — this module never adds content
beyond those two pre-cleared sources.
"""

from __future__ import annotations

import json
import logging
import re

import yaml

log = logging.getLogger(__name__)

# Draft shape guards — same spirit as the skill discovery caps: the output
# is injected instructions later, so it is validated hard BEFORE a human
# even sees it.
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_MAX_BODY = 12000
_MAX_CHECKPOINTS = 8

_FORMAT = """\
A procedure is a SKILL.md file: a `---` delimited YAML frontmatter block,
then a markdown instruction body. Frontmatter keys:
  name: <slug, letters/digits/dash>          # matches the future dir name
  shape: <task-shape tag, e.g. debug>        # one word-ish tag, NOT a name
  description: >                             # the ONLY trigger the model
    When to load this, front-loaded.         # sees — make it specific
  checkpoints:                               # 3-6 short, checkable
    - Contract note written                  # statements, verified in order
Body: ordered, imperative steps addressed to the agent, each ending on a
checkable completion criterion. GENERALIZE — no case-specific filenames,
numbers, or answers; this must help on UNSEEN tasks of the same shape.
"""


def _digest_turn(turn: dict, cap: int = 700) -> str:
    """One transcript turn → a compact process view for the judge."""
    parts = [f"USER: {str(turn.get('user') or '')[:300]}"]
    traj = str(turn.get("trajectory") or "")
    if traj:
        parts.append(f"TOOLS: {traj[:cap]}")
    ans = str(turn.get("answer") or "")
    if ans:
        parts.append(f"ANSWER: {ans[:400]}")
    return "\n".join(parts)


def _digest_eval_row(row: dict, cap: int = 3000) -> str:
    """One stored eval result → 'what the agent did', capped. The store
    keeps the transcript as a JSON TEXT blob — decode tolerantly."""
    transcript = row.get("transcript") or []
    if isinstance(transcript, str):
        try:
            transcript = json.loads(transcript)
        except ValueError:
            transcript = []
    out, size = [], 0
    for turn in transcript:
        d = _digest_turn(turn)
        if size + len(d) > cap:
            break
        out.append(d)
        size += len(d)
    status = "PASSED" if row.get("passed") else "FAILED"
    notes = str(row.get("judge_notes") or "")[:400]
    return f"[{status} run, score {row.get('score')}]\n" + \
           "\n---\n".join(out) + (f"\nJUDGE NOTES: {notes}" if notes else "")


def _digest_flag_run(run: dict, cap: int = 3000) -> str:
    """One PRE-SCRUBBED flagged run (flags endpoint shape) → process view."""
    lines = [f"[run status: {run.get('status')}]"]
    size = 0
    for e in (run.get("events") or []):
        kind = e.get("kind")
        if kind not in ("run_start", "model_turn", "tool_result", "run_finish",
                        "error"):
            continue
        line = f"{kind}: {str(e.get('payload'))[:250]}"
        if size + len(line) > cap:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:\w+)?\s*\n(.*)\n```\s*$", t, re.S)
    return m.group(1).strip() if m else t


def validate_draft(text: str) -> dict:
    """Split + validate a judge-produced SKILL.md. Returns {ok, errors,
    name, shape, draft} — `draft` is the text with `draft: true` ensured in
    the frontmatter, ready for the Studio editor."""
    errors: list[str] = []
    t = _strip_fence(text)
    meta: dict = {}
    body = t
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as e:
                errors.append(f"frontmatter is not valid YAML: {e}")
                meta = {}
            body = parts[2].lstrip("\n")
        else:
            errors.append("frontmatter block not closed")
    else:
        errors.append("missing --- frontmatter block")
    name = str(meta.get("name") or "").strip()
    if not _NAME_OK.match(name):
        errors.append(f"invalid/missing name '{name}'")
    shape = str(meta.get("shape") or "").strip()
    if not shape:
        errors.append("missing shape tag (procedures need one for the "
                      "selector/autoload)")
    if not str(meta.get("description") or "").strip():
        errors.append("missing description (the model's only trigger)")
    cps = meta.get("checkpoints") or []
    if not isinstance(cps, list) or not 2 <= len(cps) <= _MAX_CHECKPOINTS:
        errors.append(f"checkpoints must be a list of 2-{_MAX_CHECKPOINTS}")
    if not body.strip():
        errors.append("empty instruction body")
    if len(t) > _MAX_BODY:
        errors.append(f"draft too large ({len(t)} > {_MAX_BODY} chars)")
    draft = t
    if not errors and not meta.get("draft"):
        meta["draft"] = True
        fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
        draft = f"---\n{fm}---\n{body}"
    return {"ok": not errors, "errors": errors, "name": name,
            "shape": shape, "draft": draft}


async def _judge(config: dict, system: str, user: str) -> dict:
    """One judge call through the eval judge alias (cloud or local, same
    config the suite uses — a weak judge silently corrupts drafts too)."""
    from runtime.eval_runner import _model_text
    from runtime.eval_runner import config as eval_config
    ecfg = eval_config(config)
    r = await _model_text(config, str(ecfg["judge_model"]),
                          [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
                          temperature=0.2, want_json=False, max_tokens=3000)
    if r["status"] != "ok":
        return {"status": "error",
                "error": f"judge call failed: {r['error']}"}
    return {"status": "ok", "content": r["content"],
            "model": r["model_name"], "cost_usd": r["cost_usd"]}


_SYSTEM = (
    "You distill reusable PROCEDURES for a small local orchestrator model "
    "from agent run histories. Frontier models win by process discipline; "
    "your job is to extract that discipline so a weaker model can follow "
    "it. From the PASSING runs, identify the process that won (order of "
    "steps, verification habits, what was checked before what). From the "
    "FAILING runs of the same task shape, identify the step the small "
    "model SKIPPED. Write ONE procedure that enforces the winning process "
    "and names the skipped step explicitly. Output ONLY the SKILL.md file "
    "content — no prose, no code fences.\n\n" + _FORMAT)


async def mine_from_case(config: dict, store, case_id: str,
                         limit: int = 3) -> dict:
    """Distill a procedure draft from one eval case's pass/fail history.
    Needs BOTH outcomes — passes show the winning process, fails show the
    skipped step; with only one side there is nothing to contrast."""
    rows = store.results(test_id=case_id, limit=50)
    passes = [r for r in rows if r.get("passed")][:limit]
    fails = [r for r in rows if not r.get("passed")][:limit]
    if not passes or not fails:
        return {"status": "error",
                "error": f"case '{case_id}' needs at least one PASS and one "
                         f"FAIL in its history to mine a procedure "
                         f"(has {len(passes)} pass / {len(fails)} fail) — "
                         f"the contrast IS the distillation signal"}
    user = (f"## Task under analysis: {case_id}\n\n"
            + "\n\n".join(_digest_eval_row(r) for r in passes)
            + "\n\n" + "\n\n".join(_digest_eval_row(r) for r in fails))
    j = await _judge(config, _SYSTEM, user)
    if j["status"] != "ok":
        return j
    v = validate_draft(j["content"])
    return {"status": "ok", "judge_model": j["model"],
            "cost_usd": j["cost_usd"], "source": f"case:{case_id}", **v}


async def mine_from_flag(config: dict, flag: dict, runs: list[dict]) -> dict:
    """Distill a procedure draft from a flagged session. `runs` MUST be the
    pre-scrubbed dicts from the flags endpoint (privacy already applied).
    The user's 'what went wrong' note is the failing-step signal; the run
    digests show what the agent actually did."""
    if not runs:
        return {"status": "error",
                "error": "flag has no inspectable runs (pruned by trace "
                         "retention?) — nothing to mine"}
    note = str(flag.get("note") or "").strip()
    user = (f"## Flagged session — user report: {note or '(no note given)'}"
            "\n\n"
            + "\n\n".join(_digest_flag_run(r) for r in runs))
    j = await _judge(config, _SYSTEM, user)
    if j["status"] != "ok":
        return j
    v = validate_draft(j["content"])
    return {"status": "ok", "judge_model": j["model"],
            "cost_usd": j["cost_usd"], "source": f"flag:{flag.get('id')}",
            **v}
