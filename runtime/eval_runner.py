"""Eval runner — plays eval cases through the REAL agent loop and grades them.

This is the behavioural complement to the unit suite: each case's turns go
through ``AgentRuntime.run`` exactly like a chat message (same toolset rules
as an unattended channel), one conversation threaded via ``history=``, then a
judge model grades the transcript against the case's rubric. Results and
improvement proposals land in EvalStore (eval.db).

Design rules (mirroring the coroner, web/watchdog.py):

- **The budget is $ plus a wall clock.** Harness runs disable the iteration/
  token ceilings (0 = unlimited) and cap spend at ``eval.max_cost_usd`` per
  case / ``eval.suite_max_cost_usd`` per bulk run. A scenario's own
  ``expect.max_iterations`` is a *check*, not a cap. The exception:
  ``eval.turn_wall_clock_s`` (default 1800) bounds each case turn — with a
  local brain the $ cap can never fire (cost $0.00), and without this a
  crash-retry loop blocks the whole suite for hours.
- **Unattended toolset**: globally disabled tools and eval.run itself are
  excluded (no recursive evals). Confirmation-gated tools are excluded too,
  EXCEPT the sandbox-confined ones (fs.write/fs.edit), which run
  auto-approved against the per-case sandbox so cases can exercise real
  write flows. privacy.remote_llm_tools (llm.call) stay IN while the
  confirmation gate is enabled: the auto-deny provider exercises the real
  privacy gate instead of hiding it. If the gate is globally disabled it
  cannot deny — those tools fall back to excluded.
- **Eval runs are tagged** in trace.db via ``owner="_eval"`` and get a fresh
  per-case work_root in a temp sandbox.
- **Cases may require tools or a project fixture.** ``requires_tools`` skips
  (never fails) a case whose tools aren't in this install's toolset — e.g. a
  disabled plugin. ``project.files``/``project.graph`` seed a project inside
  the sandbox (graphify graph pre-built via the CLI when requested) and bind
  the run to it via ``project_id`` + a ``config_patch`` redirecting
  ``web.projects_dir`` at the sandbox.
- **Container cases** (``container: {image, workdir}``, Terminal-Bench full
  mode) run the whole case against a podman container started over the case
  work_root: code.execute is routed INSIDE it via a run_overrides tools_patch
  (never via config — chats can't get this), and the checker grades through
  ``EVAL_CONTAINER_ID`` while the container is still up. Missing podman or
  image → skip, like requires_tools.
- **Judge + driver** are one-shot chat calls through the LiteLLM proxy,
  default the configured cloud alias, falling back to local-specialist when
  the cloud is unreachable. The judge sees ONLY eval material (scenarios are
  non-private by construction), never user chats: the transcript plus a state
  block — the run's available tools, the live system prompt, descriptions of
  rubric-relevant/called tools, and a config slice — so proposals are made
  with knowledge of what the agent actually had.
- **Nothing auto-applies**: failures produce dedup'd proposals for the admin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx

from runtime.eval_cases import EvalCase
from runtime.eval_store import EvalStore
from tools.eval.compare import _cost
from tools.llm.cloud_models import resolve_model_alias

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "max_cost_usd": 0.50,          # per test case (harness turns + judge)
    "suite_max_cost_usd": 2.00,    # per bulk run
    "benchmark_max_cost_usd": 10.00,  # across ALL suites of one benchmark
    "judge_model": "glm-5.2",      # falls back to local-specialist
    "driver_model": "glm-5.2",     # adaptive driver (writes follow-up probes)
    "adaptive_max_turns": 6,
    "judge_temperature": 0.0,      # benchmark trends must not wobble
    "turn_wall_clock_s": 1800,     # per case turn; 0 = unlimited. The $ cap
                                   # can't fire on $0.00 local brains — this
                                   # is the ceiling that stops a stuck case.
}
_FALLBACK_ALIAS = "local-specialist"
_EVAL_OWNER = "_eval"

_ANSWER_CAP = 2000        # transcript chars per harness answer
_TRAJ_CAP = 1000          # trajectory chars per turn, handed to the judge
                          # (the loop caps trajectories at 800 — don't cut shorter)

# Gated tools whose effects are confined to the per-case sandbox work_root —
# eval runs exercise them for real (auto-approved by _EvalConfirm). Every
# other gated tool stays excluded: ops.run/serve.*/job.*/git remotes reach
# outside the sandbox.
_CONFINED_GATED = frozenset({"fs.write", "fs.edit"})

# Module-level runtime backref so the eval.* tools (which only get a
# ToolContext) can reach the live AgentRuntime. Set by web/server.py at
# startup; tests set it directly.
_RUNTIME = None
# Hook returning the globally disabled tool names (web users store). Set by
# web/routes_eval.py at registration — tools/ must not import web/, and the
# agent-initiated eval.run path must honour the same disabled list as the
# admin route (audit B5).
_DISABLED_HOOK = None


def set_runtime(runtime) -> None:
    global _RUNTIME
    _RUNTIME = runtime


def get_runtime():
    return _RUNTIME


def set_disabled_hook(fn) -> None:
    global _DISABLED_HOOK
    _DISABLED_HOOK = fn


def config(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    raw = (cfg.get("eval") or {})
    for k in out:
        if raw.get(k) is not None:
            out[k] = raw[k]
    out["enabled"] = bool(out["enabled"])
    return out


# ---- unattended providers ---------------------------------------------------

class _EvalConfirm:
    """Confirmation provider for eval runs: auto-approves the sandbox-
    confined gated tools (_CONFINED_GATED) so cases exercise real write
    flows; denies everything else, so a gated/cloud call tests the model's
    fallback path instead of hanging."""
    async def confirm(self, run_id, tool_name, args, emit, reason=None):
        return tool_name in _CONFINED_GATED


class _ScriptedAsk:
    """ask.user provider answering with a fixed reply (per-case `ask_reply`)."""
    def __init__(self, reply: str):
        self._reply = reply
    async def ask(self, run_id, questions, emit):
        return {q.get("id", "q"): self._reply for q in questions
                if isinstance(q, dict)}


# ---- judge / driver model calls ---------------------------------------------

# Judge verdict budget. 4000 was lost reality: reasoning judges (glm-5.2 via
# OpenRouter) burn most of it on thinking, then the JSON verdict truncates →
# "judge returned unparseable JSON" on exactly the long transcripts. 12k
# leaves room for reasoning + verdict; the retry below runs at the same
# budget, and a finish_reason == "length" is reported as truncation, not
# "unparseable".
_JUDGE_MAX_TOKENS = 12000

async def _model_text(cfg: dict, alias_in: str, messages: list[dict], *,
                      temperature: float, want_json: bool,
                      max_tokens: int = 2000) -> dict:
    """One-shot completion through the LiteLLM proxy with alias resolution and
    fallback to the local specialist. Returns {status, content, model_name,
    cost_usd, tokens, error}."""
    from runtime.paths import LITELLM_BASE
    base = (cfg.get("orchestrator", {}) or {}).get("litellm_base", LITELLM_BASE)
    # Mirror model_client._auth_headers: no header at all when the key is
    # unset (keyless localhost proxy), never a bare "Bearer " (audit S3).
    key = os.environ.get("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    aliases = [alias_in]
    if alias_in != _FALLBACK_ALIAS:
        aliases.append(_FALLBACK_ALIAS)
    last_err = "no alias resolved"
    for alias in aliases:
        model_name = resolve_model_alias(alias, cfg) or alias
        body = {"model": model_name, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        if want_json:
            body["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.post(f"{base}/v1/chat/completions",
                                      json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning("eval model call via %s failed: %s", model_name, last_err)
            continue
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        finish = data["choices"][0].get("finish_reason")
        usage = data.get("usage", {}) or {}
        ptd = usage.get("prompt_tokens_details")
        cached = ptd.get("cached_tokens", 0) if isinstance(ptd, dict) else 0
        prompt_t = int(usage.get("prompt_tokens", 0) or 0)
        completion_t = int(usage.get("completion_tokens", 0) or 0)
        cost = _cost(model_name, prompt_t, completion_t, cached,
                     cfg.get("costs", {}))
        return {"status": "ok", "content": content, "model_name": model_name,
                "cost_usd": cost, "tokens": prompt_t + completion_t,
                "finish_reason": finish, "error": None}
    return {"status": "error", "content": "", "model_name": alias_in,
            "cost_usd": 0.0, "tokens": 0, "finish_reason": None,
            "error": last_err}


def _parse_json(text: str) -> dict | None:
    """Tolerant JSON extraction — models sometimes wrap in prose/fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return None


# ---- podman (container eval cases) ---------------------------------------------

_PODMAN_TIMEOUT_S = 60


def _podman(*args: str, timeout: int = _PODMAN_TIMEOUT_S) -> tuple[int, bytes]:
    """One podman call: (exit_code, combined_output). Never raises — a missing
    binary or a timeout comes back as rc 127 so callers fail/skip cleanly.
    Synchronous by design: the calls here (image exists / run -d / stop) all
    return immediately, same posture as _run_seed_code."""
    import subprocess
    try:
        proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout or b""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e).encode()


def _container_preflight(case: EvalCase) -> str | None:
    """Why a container case cannot run here (None = it can). Container mode
    is a capability like requires_tools: unavailable backend → skip, never
    fail. The image is built by bench.import (full mode) — a missing image
    means the import never ran (or the data dir moved), not a case bug."""
    if shutil.which("podman") is None:
        return "container case but podman is not installed on this host"
    rc, _ = _podman("image", "exists", str(case.container["image"]))
    if rc != 0:
        return (f"container image '{case.container['image']}' is not present "
                f"locally — re-run bench.import (mode: full) to build it")
    return None


def _container_start(image: str, workdir: str, work_root) -> tuple[str | None, str]:
    """Start the case container: no network, bounded resources, the case
    work_root bind-mounted at the container workdir (that mount is what makes
    code.execute calls share state like a real terminal). --rm + `sleep
    infinity`: the container exists only to be exec'd into and disappears on
    stop. Returns (container_id, error).

    The bind mount would HIDE the image's own workdir content — and TB task
    fixtures live in /app inside the image. So first materialize that content
    into the work_root (throwaway `podman create` + `podman cp`): the agent
    then sees the task files both in-container AND via host-side fs.* tools.
    A copy failure is tolerated (a non-TB image may have no /app)."""
    rc, out = _podman("create", image)
    if rc == 0:
        tmp = out.decode("utf-8", "replace").strip().splitlines()[-1]
        _podman("cp", f"{tmp}:{workdir}/.", f"{work_root}/")
        _podman("rm", tmp)
    rc, out = _podman("run", "-d", "--rm", "--network", "none",
                      "--memory", "2g", "--cpus", "2",
                      "-v", f"{work_root}:{workdir}:rw",
                      image, "sleep", "infinity")
    if rc != 0:
        return None, (out.decode("utf-8", "replace").strip()[-400:]
                      or f"podman run exited {rc}")
    return out.decode("utf-8", "replace").strip().splitlines()[-1], ""


def _container_stop(container_id: str) -> None:
    """Stop the case container (started with --rm, so stopping removes it).
    Tolerates an already-gone container — cleanup must never mask the real
    result."""
    rc, out = _podman("stop", container_id, timeout=30)
    if rc != 0:
        log.info("eval container %s stop: %s", container_id,
                 out.decode("utf-8", "replace").strip()[-200:])


# ---- deterministic expectation checks ---------------------------------------

_SKILL_LOAD_RE = re.compile(r"skill\.load\(([^)…\s]+)")
_SKILL_BODY_CAP = 1500      # chars per skill body handed to the judge


def _loaded_skill_bodies(turns: list[dict],
                         skills_dir: str | Path | None = None) -> dict[str, str]:
    """Bodies of the skills the agent actually loaded (from the trajectory's
    skill.load(<name>) hints), so the judge can ground skill-tweak proposals
    in the instructions the agent really followed. Layered: custom wins.
    `skills_dir` honours the runtime's skills.dir override (audit A3)."""
    names: set[str] = set()
    for t in turns:
        names.update(_SKILL_LOAD_RE.findall(t.get("trajectory") or ""))
    if not names:
        return {}
    from runtime import paths
    from runtime import skills as skills_mod
    root = skills_dir or paths.SKILLS_DIR
    out: dict[str, str] = {}
    for name in sorted(names):
        s = skills_mod.load_skill(root, name,
                                  custom_dir=paths.CUSTOM_SKILLS_DIR)
        if s and s.get("instructions"):
            out[name] = s["instructions"][:_SKILL_BODY_CAP]
    return out


_TOOL_RE = re.compile(r"([a-z][a-z0-9_]*\.[a-z0-9_]+)\(")


def _called_tools(turns: list[dict]) -> set[str]:
    """Every tool invoked across the turns. Prefers the structural
    `tools_used` list the loop returns (audit B1/B2: the trajectory display
    string drops hint-less tools like memory.append/ask.user/code.run and
    truncates past 14 entries); the regex over the trajectory is only a
    fallback for rows recorded before tools_used existed."""
    called: set[str] = set()
    have_structural = False
    for t in turns:
        if t.get("tools") is not None:
            have_structural = True
            called.update(t["tools"])
    if not have_structural:
        for t in turns:
            called.update(_TOOL_RE.findall(t.get("trajectory") or ""))
    return called


def _subst_years(needles: list[str]) -> list[str]:
    """{year}/{next_year} placeholders in answer_contains_any — keeps the
    check deterministic without hardcoding a year that expires (audit B6)."""
    year = time.localtime().tm_year
    return [n.replace("{year}", str(year)).replace("{next_year}", str(year + 1))
            for n in needles]


# ---- exact match (GAIA-style) -------------------------------------------------

def _normalize_exact(s: str) -> str:
    """GAIA-scorer normalization: case/unicode-fold, strip articles and
    punctuation, collapse whitespace, unify number formatting (1,000 → 1000,
    42.0 → 42). Makes 'exact match' robust to phrasing, not to content."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", str(s)).lower()
    while True:
        s2 = re.sub(r"(\d),(\d)", r"\1\2", s)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"(\d+)\.0+\b", r"\1", s)
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _exact_candidates(answer: str) -> list[str]:
    """What the agent 'meant' as its final answer: text after the last
    'final answer:' marker, else the last non-empty line, else the whole
    answer — compared normalized against answer_exact_any."""
    import re
    cands: list[str] = []
    parts = re.split(r"final answer\s*[:：]", answer, flags=re.IGNORECASE)
    if len(parts) > 1 and parts[-1].strip():
        cands.append(parts[-1].strip())
    lines = [l.strip() for l in answer.splitlines() if l.strip()]
    if lines:
        cands.append(lines[-1])
    cands.append(answer)
    return cands


_CHECKER_TIMEOUT_S = 120


def _run_checker(script: str, work_root, transcript: list[dict],
                 container: dict | None = None) -> list[str]:
    """Run a case's expect.checker grading script AFTER the last turn, while
    the per-case sandbox still exists. Same posture as project.seed_code:
    scrubbed env, cwd = the case work_root (fixture files are readable),
    EVAL_ANSWER carries the final answer. Exit 0 = pass; anything else is a
    deterministic check failure with the output tail as the message.

    Container cases additionally get EVAL_CONTAINER_ID /
    EVAL_CONTAINER_WORKDIR so the checker can grade INSIDE the still-running
    container (podman cp/exec); host-side checkers never see those keys."""
    import subprocess
    answer = ""
    for t in reversed(transcript):
        if (t.get("answer") or "").strip():
            answer = t["answer"].strip()
            break
    from runtime.tool_base import scrub_env
    env = scrub_env(dict(os.environ))
    env["EVAL_ANSWER"] = answer[:8000]
    env["EVAL_WORK_ROOT"] = str(work_root)
    if container:
        env["EVAL_CONTAINER_ID"] = str(container["id"])
        env["EVAL_CONTAINER_WORKDIR"] = str(container.get("workdir") or "/app")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=str(work_root),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=_CHECKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return [f"checker timed out after {_CHECKER_TIMEOUT_S}s"]
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", errors="replace")[-600:]
        return [f"checker failed (exit {proc.returncode}): "
                f"{tail.strip() or 'no output'}"]
    return []


def check_expectations(case: EvalCase, turns: list[dict],
                       available: set[str] | None = None) -> list[str]:
    """Returns a list of expectation failures (empty = all deterministic
    checks passed). `turns` are the executed harness turns (result dicts).
    `available` (the run's tool allowlist) lets a missing must_use tool be
    reported as a harness/toolset problem instead of an agent behaviour one —
    a rubric can never be satisfied by a tool the run never exposed."""
    failures: list[str] = []
    exp = case.expect or {}
    called = _called_tools(turns)
    for name in exp.get("must_use_tools") or []:
        if name not in called:
            if available is not None and name not in available:
                failures.append(
                    f"expected tool '{name}' was never called AND was not "
                    f"available in this run (excluded from the eval toolset) "
                    f"— fix the case or the toolset, not the prompt")
            else:
                failures.append(f"expected tool '{name}' was never called")
    any_of = exp.get("must_use_any_tools") or []
    if any_of and not any(n in called for n in any_of):
        if available is not None and not any(n in available for n in any_of):
            failures.append(
                f"none of {any_of} was called AND none was available in this "
                f"run (excluded from the eval toolset) — fix the case or the "
                f"toolset, not the prompt")
        else:
            failures.append(f"none of the expected tools {any_of} was called")
    for name in exp.get("must_not_use_tools") or []:
        if name in called:
            failures.append(f"forbidden tool '{name}' was called")
    needles = _subst_years(exp.get("answer_contains_any") or [])
    if needles:
        blob = "\n".join((t.get("answer") or "") for t in turns).lower()
        if not any(n.lower() in blob for n in needles):
            failures.append(f"no answer contained any of {needles}")
    exact = exp.get("answer_exact_any") or []
    if exact:
        candidates: list[str] = []
        for t in reversed(turns):
            ans = (t.get("answer") or "").strip()
            if ans:
                candidates = _exact_candidates(ans)
                break
        wanted = {_normalize_exact(n) for n in exact}
        if not any(_normalize_exact(c) in wanted for c in candidates):
            failures.append(
                f"final answer did not exactly match any of {exact}")
    cap = exp.get("max_iterations")
    if cap:
        for i, t in enumerate(turns):
            it = int(((t.get("budget") or {}).get("iterations")) or 0)
            if it > cap:
                failures.append(f"turn {i + 1} used {it} iterations (cap {cap})")
    return failures


# ---- the judge ---------------------------------------------------------------

_JUDGE_SYSTEM = """You are the judge of an LLM-agent eval harness. You grade ONE finished test case from its scenario, deterministic check results, and transcript (user turns, agent answers, tool trajectories).

Reply with a single JSON object:
{
  "pass": true|false,          // does the run satisfy the rubric?
  "score": 0-10,               // 10 = exemplary, 7-9 good, 5-6 weak pass, <5 fail
  "notes": "≤80 words: what was good/bad, for the admin",
  "classification": "none|prompt-tweak|skill-tweak|tool-description|config|bad-test|bug-for-dev",
  "what": "",                  // on failure: one sentence, what went wrong
  "cause": "",                 // on failure: most likely root cause
  "fix": "",                   // on failure: one concrete, minimal change
  "target": "",                // the artifact to change: tool name (tool-description), skill name (skill-tweak), config dotpath (config); "" otherwise
  "proposed_content": ""       // tool-description: the FULL replacement description. config: the proposed value (scalar). Otherwise ""
}
Classification guide: prompt-tweak = system-prompt wording would prevent it; skill-tweak = a loaded skill's instructions misled the model; tool-description = a tool's description/schema misled the model; config = a budget/threshold/flag value; bad-test = the scenario or rubric is wrong, or impossible under this run's available tools, not the agent; bug-for-dev = a real code defect. Use "none" on pass.
Rules for the context block (when provided):
- AVAILABLE TOOLS is authoritative: if a rubric-required tool is absent there, the run could never have called it — classify bad-test or config, never prompt-tweak.
- Trajectory lines are tool(arg)→status plus errors only. A →ok call DID execute and return output even though you cannot see it — never infer fabricated results from that absence.
- The LIVE SYSTEM PROMPT is what the agent actually saw: do not propose wording it already contains.
- tool-description proposals must come with a complete replacement description in proposed_content (not a diff, not advice) and target set to the tool name.
Grade ONLY against the rubric and checks. Be strict but fair; do not invent facts beyond the transcript."""


async def _judge(cfg: dict, ecfg: dict, case: EvalCase,
                 turns: list[dict], check_failures: list[str],
                 state: dict | None = None) -> dict:
    """Grade a finished case. Returns {pass, score, notes, classification,
    what, cause, fix, judge_model, cost_usd, tokens, error}. `state` is the
    state block — available tools, live system prompt, targeted tool
    descriptions, config slice — so the judge proposes fixes with knowledge
    of what the agent actually had (audit: state-blind judges misdiagnosed
    harness exclusions as prompt problems)."""
    lines = [f"SCENARIO: {case.name} (id {case.id}, driver {case.driver})",
             f"RUBRIC: {case.judge_rubric}"]
    if check_failures:
        lines.append("DETERMINISTIC CHECK FAILURES:\n- " + "\n- ".join(check_failures))
    else:
        lines.append("DETERMINISTIC CHECKS: all passed")
    lines.append("TRANSCRIPT:")
    for i, t in enumerate(turns):
        lines.append(f"--- turn {i + 1} ---")
        lines.append(f"user: {t.get('user', '')}")
        lines.append(f"status: {t.get('status', '?')}")
        answer = (t.get("answer") or "")[:_ANSWER_CAP]
        lines.append(f"answer: {answer}")
        traj = (t.get("trajectory") or "")[:_TRAJ_CAP]
        if traj:
            lines.append(f"trajectory: {traj}")
    if state:
        avail = state.get("available_tools") or []
        lines.append(f"AVAILABLE TOOLS this run could call ({len(avail)}) — "
                     "authoritative:\n" + ", ".join(sorted(avail)))
        descs = state.get("tool_descriptions") or {}
        if descs:
            lines.append("TOOL DESCRIPTIONS (rubric-relevant + tools the "
                         "agent called):")
            lines += [f"- {n}: {d}" for n, d in sorted(descs.items())]
        bodies = state.get("skill_bodies") or {}
        if bodies:
            lines.append("SKILL INSTRUCTIONS the agent loaded (current text "
                         "— propose skill-tweaks against this):")
            lines += [f"--- skill {n} ---\n{b}" for n, b in sorted(bodies.items())]
        if state.get("config"):
            lines.append("RELEVANT CONFIG: "
                         + json.dumps(state["config"], default=str))
        prompt = state.get("system_prompt") or ""
        if prompt:
            lines.append("LIVE SYSTEM PROMPT (what the agent actually ran "
                         "with):\n---\n" + prompt + "\n---")
    r = await _model_text(
        cfg, str(ecfg["judge_model"]),
        [{"role": "system", "content": _JUDGE_SYSTEM},
         {"role": "user", "content": "\n".join(lines)}],
        temperature=float(ecfg["judge_temperature"]), want_json=True,
        max_tokens=_JUDGE_MAX_TOKENS)
    out = {"pass": False, "score": None, "notes": "", "classification": "none",
           "target": "", "proposed_content": "",
           "what": "", "cause": "", "fix": "", "judge_model": r["model_name"],
           "cost_usd": r["cost_usd"], "tokens": r["tokens"], "error": r["error"]}
    if r["status"] != "ok":
        out["notes"] = f"judge unavailable: {r['error']}"
        return out
    truncated = r.get("finish_reason") == "length"
    parsed = _parse_json(r["content"])
    if not parsed:
        # One retry: unparseable verdicts were a real failure mode (prose
        # around the JSON, truncation). Costs an extra call only when hit.
        r = await _model_text(
            cfg, str(ecfg["judge_model"]),
            [{"role": "system", "content": _JUDGE_SYSTEM},
             {"role": "user", "content": "\n".join(lines)},
             {"role": "assistant", "content": r["content"]},
             {"role": "user", "content": "Your reply was not valid JSON. "
                                         "Reply with ONLY the JSON object — "
                                         "no prose, no markdown fences."}],
            temperature=float(ecfg["judge_temperature"]), want_json=True,
            max_tokens=_JUDGE_MAX_TOKENS)
        out["cost_usd"] += r["cost_usd"]
        out["tokens"] += r["tokens"]
        out["judge_model"] = r["model_name"]
        truncated = truncated or r.get("finish_reason") == "length"
        parsed = _parse_json(r["content"])
    if not parsed:
        out["notes"] = ("judge verdict truncated at the token cap"
                        if truncated else "judge returned unparseable JSON")
        out["error"] = "bad judge json"
        return out
    out["pass"] = bool(parsed.get("pass"))
    try:
        out["score"] = max(0.0, min(10.0, float(parsed.get("score"))))
    except (TypeError, ValueError):
        out["score"] = None
    for k in ("notes", "classification", "what", "cause", "fix"):
        out[k] = str(parsed.get(k) or "")[:2000]
    out["target"] = str(parsed.get("target") or "")[:200]
    out["proposed_content"] = str(parsed.get("proposed_content") or "")[:4000]
    # Structural fields only make sense with their classification.
    if out["classification"] not in ("tool-description", "skill-tweak", "config"):
        out["target"] = out["proposed_content"] = ""
    return out


# ---- adaptive driver ---------------------------------------------------------

_DRIVER_SYSTEM = """You are the driver of an LLM-agent eval: you play the USER in a test conversation. From the scenario, rubric, and transcript so far, write the next user message — natural follow-ups, challenges, or new angles that probe the rubric. End the conversation when the rubric has been sufficiently tested.

Reply with a single JSON object: {"message": "the next user turn"} or {"done": true}.
Keep messages short and realistic. Never reveal you are a test driver."""


async def _next_probe(cfg: dict, ecfg: dict, case: EvalCase,
                      turns: list[dict]) -> dict:
    """The adaptive driver's next user message. Returns {message|done, cost_usd,
    tokens}; message is None when the driver ends the conversation or errors."""
    lines = [f"SCENARIO: {case.name}", f"RUBRIC: {case.judge_rubric}",
             "TRANSCRIPT SO FAR:"]
    for i, t in enumerate(turns):
        answer = (t.get("answer") or "")[:800]
        lines.append(f"user: {t.get('user', '')}\nagent ({t.get('status', '?')}): {answer}")
    r = await _model_text(
        cfg, str(ecfg["driver_model"]),
        [{"role": "system", "content": _DRIVER_SYSTEM},
         {"role": "user", "content": "\n".join(lines)}],
        temperature=0.7, want_json=True, max_tokens=500)
    out = {"message": None, "cost_usd": r["cost_usd"], "tokens": r["tokens"]}
    if r["status"] != "ok":
        log.warning("eval driver unavailable (%s) — ending adaptive case", r["error"])
        return out
    parsed = _parse_json(r["content"]) or {}
    if not parsed.get("done"):
        msg = str(parsed.get("message") or "").strip()
        out["message"] = msg or None
    return out


# ---- project fixtures ---------------------------------------------------------

_GRAPH_BUILD_TIMEOUT_S = 300


def _seed_project(sandbox: str, case: EvalCase) -> tuple[str, str, dict]:
    """Create the case's project fixture under <sandbox>/projects and return
    (project_id, work_root, config_patch). File paths were validated at load
    time (relative, no '..'). The layout mirrors web/projects.py so the
    graphify plugin's per-project resolution works unchanged — pointed at the
    sandbox via the config_patch (web.projects_dir).

    `seed_code` (optional) then runs as a Python snippet with the fixture dir
    as cwd — for LARGE generated fixtures (a seeded 200KB log) that would be
    absurd as YAML literals. Trusted content (it ships with the case like
    `files` does), but still run with a scrubbed env and a hard timeout."""
    projects_root = Path(sandbox) / "projects"
    pid = f"eval-{case.id}"
    files = projects_root / _EVAL_OWNER / pid / "files"
    for rel, content in (case.project.get("files") or {}).items():
        dest = files / str(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(str(content), encoding="utf-8")
    seed = case.project.get("seed_code")
    if seed:
        files.mkdir(parents=True, exist_ok=True)
        _run_seed_code(seed, files)
    (files.parent / "project.json").write_text(json.dumps(
        {"id": pid, "name": case.name}), encoding="utf-8")
    return pid, str(files), {"web": {"projects_dir": str(projects_root)}}


_SEED_CODE_TIMEOUT_S = 120


def _run_seed_code(seed: str, files_dir: Path) -> None:
    import subprocess

    from runtime.tool_base import scrub_env
    proc = subprocess.run(
        [sys.executable, "-c", seed], cwd=str(files_dir),
        env=scrub_env(dict(os.environ)),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=_SEED_CODE_TIMEOUT_S)
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"seed_code exited {proc.returncode}: {tail}")


async def _prebuild_graph(cfg: dict, projects_root: str, pid: str) -> str | None:
    """Build the fixture project's graph via the graphify CLI (synchronous;
    a code-only fixture needs no LLM — extraction is local AST). Returns an
    error string, None on success. Shells the same CLI the plugin drives —
    runtime/ never imports plugins/, so the commands are mirrored here."""
    import importlib.util
    if importlib.util.find_spec("graphify") is None:
        return ("graphify CLI not installed (pip package 'graphifyy') — "
                "install the plugin's dependencies to run this case")
    proj = Path(projects_root) / _EVAL_OWNER / pid
    files = proj / "files"
    graph = proj / "graphify-out" / "graph.json"
    base = str((cfg.get("orchestrator", {}) or {}).get("litellm_base")
               or "http://127.0.0.1:4000").rstrip("/")
    from runtime.tool_base import scrub_env
    env = scrub_env(dict(os.environ))   # same posture as the MCP stdio bridge
    env["OPENAI_BASE_URL"] = base + "/v1"
    env["OPENAI_API_KEY"] = os.environ.get("LITELLM_MASTER_KEY") or "sk-local"
    pcfg = (cfg.get("plugins") or {}).get("graphify") or {}
    env["OPENAI_MODEL"] = str(pcfg.get("model") or "local-specialist")
    token_budget = str(int(pcfg.get("token_budget") or 4000))
    steps = [
        ["extract", str(files), "--out", str(proj),
         "--token-budget", token_budget, "--max-concurrency", "2"],
        ["cluster-only", str(files), "--graph", str(graph), "--no-label"],
    ]
    for argv in steps:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "graphify", *argv,
            cwd=str(proj), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(),
                                            timeout=_GRAPH_BUILD_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"graphify {argv[0]} timed out"
        # cluster-only failure is tolerated (mirrors the plugin runner:
        # graph.json from extract stays usable); extract failure is fatal.
        if proc.returncode != 0 and argv[0] == "extract":
            tail = (out or b"").decode("utf-8", "replace")[-400:]
            return f"graphify extract exited {proc.returncode}: {tail}"
    if not graph.is_file():
        return "graphify build produced no graph.json"
    return None


def _project_prefix(case: EvalCase, files_root: Path) -> str:
    """Turn-1 context prefix for project-fixture cases, mirroring what the web
    layer prepends on project-bound runs (web/server.py _augment_with_project):
    the [Project:] banner, the file tree, and any plugin hint via the
    augment_project_context hook (e.g. graphify's '[Project graph] this
    project is mapped — prefer graph.query…'). Without it the agent gets the
    tools but zero nudge — the live judge failed exactly that."""
    pid = f"eval-{case.id}"
    tree = "\n".join(sorted(
        str(p.relative_to(files_root))
        for p in files_root.rglob("*") if p.is_file())) or "(empty)"
    prefix = (f"[Project: {case.name}]\n"
              "Your fs.* and code.* tools are already rooted in this "
              "project's files — write paths relative to it. Current files:\n"
              f"{tree}\n")
    from runtime import hooks as _hooks
    meta = {"id": pid, "name": case.name}
    for extra in _hooks.fire("augment_project_context",
                             _EVAL_OWNER, pid, meta, files_root):
        text = str(extra).strip()
        if text:
            prefix += text + "\n"
    return prefix


# ---- the runner ---------------------------------------------------------------

def _unattended_tools(runtime, extra_disabled: set[str] | None) -> list[str]:
    """Tools for eval runs: everything except eval.run itself (no recursive
    evals), globally disabled tools, and confirmation-gated tools — minus the
    sandbox-confined _CONFINED_GATED, which run auto-approved against the
    per-case sandbox so cases exercise real write flows.
    privacy.remote_llm_tools stay IN while the confirmation gate is enabled
    (the provider denies them, exercising the real privacy gate); if the gate
    is globally disabled it cannot deny — fall back to excluding them."""
    cfg = runtime.config
    gated = {t.name for t in runtime.registry.all()
             if getattr(t, "requires_confirmation", False)}
    gated -= _CONFINED_GATED
    confirm_on = (cfg.get("confirmation", {}) or {}).get("enabled", True)
    remote = (set() if confirm_on else
              set((cfg.get("privacy", {}) or {})
                  .get("remote_llm_tools", []) or []))
    disabled = set(extra_disabled or ()) | {"eval.run"}
    return [t.name for t in runtime.registry.all()
            if t.name not in gated and t.name not in remote
            and t.name not in disabled]


async def run_case(runtime, case: EvalCase, store: EvalStore, *,
                   disabled_tools: set[str] | None = None,
                   record: bool = True,
                   variant: dict | None = None) -> dict:
    """Execute one case end-to-end and (by default) record it. Returns the
    stored result row (or the would-be row when record=False).

    `variant` (benchmarks): {"label", "model", "sampling"} — the model alias
    and sampler overrides the run executes under; `label` is recorded as the
    result's brain so variants compare apples-to-apples in the stats."""
    ecfg = config(runtime.config)
    if disabled_tools is None and _DISABLED_HOOK is not None:
        disabled_tools = set(_DISABLED_HOOK())
    started = time.monotonic()
    total_cost = 0.0
    total_tokens = 0
    run_ids: list[str] = []
    transcript: list[dict] = []
    history: list[dict] = []
    tools = _unattended_tools(runtime, disabled_tools)
    missing = [t for t in (case.requires_tools or []) if t not in tools]
    if missing:
        # Skippable-by-design cases (e.g. plugin tools): an install without
        # the plugin can never satisfy the rubric — skip, don't fail.
        return {"test_id": case.id, "skipped": True, "cost_usd": 0.0,
                "note": "requires tools absent from this run's toolset: "
                        + ", ".join(missing)}
    if case.container:
        # Container cases (Terminal-Bench full mode) need podman + the
        # pre-built image — a capability gate like requires_tools: skip,
        # never fail, when the backend isn't there.
        note = _container_preflight(case)
        if note:
            return {"test_id": case.id, "skipped": True, "cost_usd": 0.0,
                    "note": note}
    budget = {"max_iterations": 0,
              "max_wall_clock_s": int(ecfg.get("turn_wall_clock_s") or 0),
              "max_cost_usd": float(ecfg["max_cost_usd"]), "max_total_tokens": 0}
    ask_reply = str((case.expect or {}).get("ask_reply") or "yes, proceed")

    with tempfile.TemporaryDirectory(prefix=f"eval-{case.id}-",
                                     ignore_cleanup_errors=True) as sandbox:
        # Persistent stores redirect into the sandbox (audit S2): eval runs
        # must not pollute the user's real memory/rag DBs, and real user
        # memories must not leak into a cloud-judged transcript.
        tools_patch = {"memory": {"db_path": str(Path(sandbox) / "memory.db")},
                       "rag": {"db_path": str(Path(sandbox) / "rag.db")}}
        run_overrides = {"tools_patch": tools_patch}
        project_id = None
        work_root = sandbox
        if case.project:
            project_id, work_root, cfg_patch = _seed_project(sandbox, case)
            run_overrides["config_patch"] = cfg_patch
            if case.project.get("graph"):
                err = await _prebuild_graph(
                    runtime.config, cfg_patch["web"]["projects_dir"], project_id)
                if err:
                    return {"test_id": case.id, "skipped": True,
                            "cost_usd": 0.0,
                            "note": f"graph prebuild failed: {err}"}
        if variant and variant.get("sampling"):
            run_overrides["sampling"] = dict(variant["sampling"])
            # Cross-model variants must still get the pinned sampling —
            # loop.py otherwise drops run-level sampling for non-brain models
            # (the chat-impersonation guard).
            run_overrides["sampling_force"] = True
        container = None
        if case.container:
            # Start the case container over the (already seeded) work_root and
            # route code.execute into it — ONLY via run_overrides tools_patch;
            # no config path turns container mode on for chats.
            ctr_workdir = str(case.container.get("workdir") or "/app")
            Path(work_root).mkdir(parents=True, exist_ok=True)
            cid, err = _container_start(str(case.container["image"]),
                                        ctr_workdir, work_root)
            if cid is None:
                return {"test_id": case.id, "skipped": True, "cost_usd": 0.0,
                        "note": f"container failed to start: {err}"}
            container = {"id": cid, "workdir": ctr_workdir, "python": "python3"}
            tools_patch["code"] = {"container": dict(container)}
        try:
            pending = list(case.turns)
            max_turns = (int(ecfg["adaptive_max_turns"]) if case.driver == "adaptive"
                         else len(pending))
            while pending and len(transcript) < max_turns:
                message = pending.pop(0)
                if case.project and not transcript:
                    # Mirror the web layer's project context (banner + file tree +
                    # plugin hints) on the first turn — otherwise the agent gets
                    # graph.* tools with zero nudge to use them.
                    message = (_project_prefix(case, Path(work_root))
                               + "\n" + message)
                remaining = float(ecfg["max_cost_usd"]) - total_cost
                if remaining <= 0:
                    transcript.append({"user": message, "status": "skipped",
                                       "answer": "", "trajectory": "", "tools": [],
                                       "budget": {}, "note": "case cost cap reached"})
                    break
                result = await runtime.run(
                    message,
                    share_private=False,
                    budget_overrides={**budget, "max_cost_usd": remaining},
                    tools=tools,
                    confirm_provider=_EvalConfirm(),
                    ask_provider=_ScriptedAsk(ask_reply),
                    history=history or None,
                    owner=_EVAL_OWNER,
                    work_root=work_root,
                    project_id=project_id,
                    run_overrides=run_overrides,
                    model=(variant or {}).get("model") or None,
                    think=True,
                    stream=False,
                )
                run_ids.append(result.get("run_id") or "")
                b = result.get("budget") or {}
                total_cost += float(b.get("cost_usd") or 0)
                tok = b.get("tokens") or {}
                total_tokens += int(tok.get("total") or 0)
                turn = {"user": message, "status": result.get("status"),
                        "answer": result.get("answer") or "",
                        "trajectory": result.get("trajectory") or "",
                        "tools": list(result.get("tools_used") or []),
                        "budget": b}
                transcript.append(turn)
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant",
                                "content": (result.get("answer") or "")[:4000]})
                if case.driver == "adaptive" and not pending:
                    probe = await _next_probe(runtime.config, ecfg, case, transcript)
                    total_cost += float(probe["cost_usd"])
                    total_tokens += int(probe["tokens"])
                    if probe["message"] is None:
                        break
                    pending.append(probe["message"])

            # The grading script runs INSIDE the sandbox lifetime — it grades the
            # fixture/work files, which are deleted when the block exits. For
            # container cases it also runs while the container is still up
            # (EVAL_CONTAINER_ID lets it podman cp/exec the grading tests in).
            checker_script = (case.expect or {}).get("checker")
            checker_failures = (_run_checker(checker_script, Path(work_root),
                                             transcript, container=container)
                                if checker_script else [])
        finally:
            if container:
                _container_stop(container["id"])

    check_failures = checker_failures + check_expectations(
        case, transcript, available=set(tools))
    exp = case.expect or {}
    relevant = (set(exp.get("must_use_tools") or [])
                | set(exp.get("must_not_use_tools") or [])
                | _called_tools(transcript))
    # State block for the judge: what the agent actually had. Without it the
    # judge is guessing — a state-blind judge misdiagnosed harness toolset
    # exclusions as prompt problems (fs.write/llm.call were never exposed).
    state = {
        "available_tools": tools,
        "system_prompt": getattr(runtime, "system_prompt", "") or "",
        "tool_descriptions": {
            t.name: (getattr(t, "description", "") or "")
            for t in runtime.registry.all() if t.name in relevant},
        "skill_bodies": _loaded_skill_bodies(
            transcript,
            skills_dir=(runtime.config.get("skills") or {}).get("dir")),
        "config": {
            "eval": ecfg,
            # budgets + architect.threshold are config-proposal targets —
            # the judge must see the current values to propose sane ones.
            "budgets": runtime.config.get("budgets") or {},
            "loop_guard": runtime.config.get("loop_guard") or {},
            "architect.threshold":
                (runtime.config.get("architect") or {}).get("threshold"),
            "privacy.remote_llm_tools":
                (runtime.config.get("privacy") or {})
                .get("remote_llm_tools", []) or [],
            "confirmation.enabled":
                (runtime.config.get("confirmation") or {})
                .get("enabled", True)},
    }
    judged = await _judge(runtime.config, ecfg, case, transcript,
                          check_failures, state)
    total_cost += float(judged["cost_usd"])
    total_tokens += int(judged["tokens"])
    passed = not check_failures and bool(judged["pass"])
    status = "ok" if all(t.get("status") == "ok" for t in transcript) else "mixed"

    row = {"test_id": case.id, "passed": passed, "score": judged["score"],
           "judge_notes": judged["notes"], "judge_model": judged["judge_model"],
           "cost_usd": round(total_cost, 6), "tokens": total_tokens,
           "elapsed_s": round(time.monotonic() - started, 2), "status": status,
           "run_ids": run_ids, "transcript": transcript}
    if record:
        stored = store.record_result(
            test_id=case.id, passed=passed, score=judged["score"],
            judge_notes=judged["notes"], judge_model=judged["judge_model"],
            cost_usd=total_cost, tokens=total_tokens,
            elapsed_s=row["elapsed_s"], status=status,
            run_ids=run_ids, transcript=transcript,
            brain=((variant or {}).get("label")
                   or getattr(runtime, "model", None)),
            benchmark=variant is not None)
        row = stored
        if not passed and judged["classification"] not in ("", "none", "bad-test"):
            proposal = store.add_proposal(
                test_id=case.id, result_id=stored.get("id"),
                classification=judged["classification"],
                target=judged["target"] or None,
                proposed_content=judged["proposed_content"] or None,
                what=judged["what"] or judged["notes"],
                cause=judged["cause"] or "unclear",
                fix=judged["fix"] or "unclear")
            if proposal:
                row["proposal_id"] = proposal["id"]
    row["check_failures"] = check_failures
    row["judge_error"] = judged["error"]
    return row


async def run_suite(runtime, cases: list[EvalCase], store: EvalStore, *,
                    disabled_tools: set[str] | None = None,
                    variant: dict | None = None,
                    progress=None, should_stop=None) -> dict:
    """Run cases sequentially under the suite cost cap. `progress` is an
    optional sync callable(case_id, row) after each case. `variant` (see
    run_case) makes the suite execute under a benchmark variant.
    `should_stop` is an optional sync callable polled BETWEEN cases (admin
    cancel): once true, the current case finishes but every later case is
    marked skipped-cancelled and the summary carries cancelled=True."""
    ecfg = config(runtime.config)
    cap = float(ecfg["suite_max_cost_usd"])
    spent = 0.0
    rows = []
    cancelled = False
    for case in cases:
        if cancelled or (should_stop is not None and should_stop()):
            cancelled = True
            rows.append({"test_id": case.id, "skipped": True,
                         "note": "cancelled by admin"})
            continue
        if spent >= cap:
            rows.append({"test_id": case.id, "skipped": True,
                         "note": f"suite cost cap ${cap:.2f} reached"})
            continue
        try:
            row = await run_case(runtime, case, store,
                                 disabled_tools=disabled_tools,
                                 variant=variant)
        except Exception as e:
            log.exception("eval case %s crashed", case.id)
            row = {"test_id": case.id, "passed": False, "score": None,
                   "judge_notes": f"runner crashed: {type(e).__name__}: {e}",
                   "cost_usd": 0.0}
        spent += float(row.get("cost_usd") or 0)
        rows.append(row)
        if progress:
            try:
                progress(case.id, row)
            except Exception:
                pass
    ran = [r for r in rows if not r.get("skipped")]
    return {"cases": len(rows), "ran": len(ran),
            "passed": sum(1 for r in ran if r.get("passed")),
            "failed": sum(1 for r in ran if not r.get("passed")),
            "cancelled": cancelled,
            "cost_usd": round(spent, 6), "results": rows}
