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
  work_root: code.run/code.execute are routed INSIDE it via a run_overrides tools_patch
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
import yaml

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
    "wall_clock_grace_s": 120,     # liveness ping: an expiring wall clock
                                   # extends by this while the run is still
                                   # cycling (a zombie never reaches the ping)
    "wall_clock_max_extensions": 5,  # up to +10 min per case turn
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

# "brain" benchmark variants drop the delegation verbs: code.delegate (the
# strength-routed specialist front door), architect (plan-first gate) and
# agent.spawn (raw sub-agents). What remains is what the brain alone can do —
# the honest A/B against "full", which is JayNet's whole model-routing story.
_BRAIN_VARIANT_EXCLUDED = frozenset({"code.delegate", "architect", "agent.spawn"})


class BackendDownError(Exception):
    """The model backend was unreachable mid-suite (llama-server crash,
    litellm restart). Raised by run_case, caught by run_suite — without this
    brake a dead backend burns the whole queue in seconds (each case fails
    instantly on ConnectError), poisoning every result."""


async def _backend_recovered(config: dict, attempts: int = 6,
                             delay_s: float = 15.0) -> bool:
    """Transient-outage grace: a litellm restart (cloud-catalog edit, deploy)
    drops connections for ~10-30s. Probe the proxy with backoff before the
    suite declares the backend dead. ANY HTTP response (even 401/404) counts
    as recovered — the brake is about connectivity, not auth."""
    import httpx
    base = ((config.get("orchestrator") or {})
            .get("litellm_base") or "").rstrip("/")
    if not base:
        return False
    for i in range(attempts):
        if i:
            await asyncio.sleep(delay_s)
        try:
            async with httpx.AsyncClient(timeout=5) as cl:
                await cl.get(f"{base}/health/liveness")
            return True
        except Exception:
            continue
    return False


# Substrings that mark a backend-connectivity failure in a run's error
# answer ("[Internal error: ConnectError: All connection attempts failed]").
_BACKEND_DOWN_NEEDLES = ("ConnectError", "Connection refused",
                         "All connection attempts failed")

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


# ---- podman-compose (multi-service container cases) ---------------------------

_COMPOSE_FILE_NAMES = ("docker-compose.yaml", "docker-compose.yml")
_COMPOSE_UP_TIMEOUT_S = 300        # `up -d` can pull sibling images
_COMPOSE_READY_TIMEOUT_S = 90      # bounded wait for the client container


def _compose(project: str, compose_files: list, env: dict, *args: str,
             timeout: int = 60) -> tuple[int, bytes]:
    """One `podman compose` call (delegates to podman-compose): (exit_code,
    combined_output). Same never-raises posture as _podman. The env carries
    ONLY the interpolation variables the task compose templates reference —
    never the process env, so no host secret can leak into a container via a
    passthrough `environment:` entry."""
    import subprocess
    argv = ["podman", "compose", "-p", project]
    for f in compose_files:
        argv += ["-f", str(f)]
    try:
        proc = subprocess.run([*argv, *args], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout or b""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e).encode()


def _compose_project_name(case_id: str) -> str:
    """Unique per-run compose project: lowercase alnum/dash only (compose is
    picky), pid + random suffix so parallel suites and re-runs never clash.
    Project scoping is also what isolates the stack's named volumes."""
    import secrets
    slug = re.sub(r"[^a-z0-9]+", "", case_id.lower())[:16] or "case"
    return f"ev{slug}-{os.getpid()}{secrets.token_hex(2)}"


def _container_start_compose(case: EvalCase, workdir: str,
                             work_root) -> tuple[dict | None, str]:
    """Start a multi-service case: the task's OWN docker-compose.yaml
    verbatim via podman-compose, plus a generated override that bind-mounts
    the case work_root at the client workdir (same sharing semantics as
    single-container mode). The prebuilt client image is injected via
    T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME so compose never rebuilds it;
    depends_on/init-container ordering is compose's job. The image's workdir
    content is still materialized into the work_root first (fixtures baked
    into the image that no compose mount covers). Returns (container_dict,
    error) — the dict matches _container_start's shape plus the compose
    teardown data (project, files, env, tmp dir)."""
    compose_dir = Path(str(case.container["compose"]))
    client = str(case.container.get("client_service") or "client")
    base = next((compose_dir / n for n in _COMPOSE_FILE_NAMES
                 if (compose_dir / n).is_file()), None)
    if base is None:
        return None, f"no docker-compose.yaml in {compose_dir}"
    image = str(case.container["image"])
    rc, out = _podman("create", image)
    if rc == 0:
        tmp = out.decode("utf-8", "replace").strip().splitlines()[-1]
        _podman("cp", f"{tmp}:{workdir}/.", f"{work_root}/")
        _podman("rm", tmp)
    project = _compose_project_name(case.id)
    aux = Path(tempfile.mkdtemp(prefix=f"eval-{case.id}-compose-"))
    logs_dir = aux / "logs"
    agent_logs_dir = aux / "agent-logs"
    logs_dir.mkdir()
    agent_logs_dir.mkdir()
    override = aux / "compose-override.yaml"
    override.write_text(yaml.safe_dump(
        {"services": {client: {"volumes":
                               [f"{work_root}:{workdir}:rw"]}}},
        sort_keys=False), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", ""),
           "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": image,
           "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": f"{project}_client",
           "T_BENCH_TEST_DIR": "/tests",
           "T_BENCH_TASK_LOGS_PATH": str(logs_dir),
           "T_BENCH_TASK_AGENT_LOGS_PATH": str(agent_logs_dir),
           "T_BENCH_CONTAINER_LOGS_PATH": "/logs",
           "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/agent-logs"}
    if os.environ.get("XDG_RUNTIME_DIR"):      # rootless podman socket
        env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    files = [str(base), str(override)]
    rc, out = _compose(project, files, env, "up", "-d",
                       timeout=_COMPOSE_UP_TIMEOUT_S)
    if rc != 0:
        shutil.rmtree(aux, ignore_errors=True)
        return None, (out.decode("utf-8", "replace").strip()[-400:]
                      or f"podman compose up exited {rc}")
    cid = f"{project}_client"
    deadline = time.monotonic() + _COMPOSE_READY_TIMEOUT_S
    while True:
        rc, out = _podman("inspect", "-f", "{{.State.Running}}", cid)
        if rc == 0 and out.decode("utf-8", "replace").strip() == "true":
            break
        if time.monotonic() >= deadline:
            _compose(project, files, env, "down", "-v", "-t", "10")
            shutil.rmtree(aux, ignore_errors=True)
            return None, (f"client container '{cid}' did not reach running "
                          f"state within {_COMPOSE_READY_TIMEOUT_S}s")
        time.sleep(2)
    return {"id": cid, "workdir": workdir, "python": "python3",
            "compose_project": project, "compose_files": files,
            "compose_env": env, "compose_aux": str(aux)}, ""


def _compose_stack_down(container: dict) -> None:
    """Tear a compose case's stack down: `down -v` also removes the per-run
    project-scoped named volumes (shared_db/deletion_logs et al), so runs
    are isolated. Tolerates an already-gone stack — cleanup must never mask
    the real result."""
    rc, out = _compose(str(container["compose_project"]),
                       list(container["compose_files"]),
                       dict(container["compose_env"]),
                       "down", "-v", "-t", "10", timeout=60)
    if rc != 0:
        log.info("eval compose stack %s down: %s",
                 container["compose_project"],
                 out.decode("utf-8", "replace").strip()[-200:])
    shutil.rmtree(str(container.get("compose_aux") or ""), ignore_errors=True)


def _container_preflight(case: EvalCase) -> str | None:
    """Why a container case cannot run here (None = it can). Container mode
    is a capability like requires_tools: unavailable backend → skip, never
    fail. The image is built by bench.import (full mode) — a missing image
    means the import never ran (or the data dir moved), not a case bug."""
    if shutil.which("podman") is None:
        return "container case but podman is not installed on this host"
    if case.container.get("compose"):
        if shutil.which("podman-compose") is None:
            return ("multi-service container case but podman-compose is not "
                    "installed on this host")
        cdir = Path(str(case.container["compose"]))
        if not any((cdir / n).is_file() for n in _COMPOSE_FILE_NAMES):
            return (f"container compose dir '{cdir}' has no "
                    f"docker-compose.yaml — re-run bench.import (mode: full)")
    rc, _ = _podman("image", "exists", str(case.container["image"]))
    if rc != 0:
        return (f"container image '{case.container['image']}' is not present "
                f"locally — re-run bench.import (mode: full) to build it")
    return None


def _container_start(image: str, workdir: str, work_root,
                     network: bool = False) -> tuple[str | None, str]:
    """Start the case container: bounded resources, the case work_root
    bind-mounted at the container workdir (that mount is what makes
    code.run/code.execute calls share state like a real terminal). --rm + `sleep
    infinity`: the container exists only to be exec'd into and disappears on
    stop. Returns (container_id, error).

    network=false (default) starts with --network none; network=true gives
    outbound access for tasks that must download (official Terminal-Bench
    allows it — the container is throwaway and holds no credentials).

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
    argv = ["run", "-d", "--rm"]
    if not network:
        argv += ["--network", "none"]
    argv += ["--memory", "2g", "--cpus", "2",
             "-v", f"{work_root}:{workdir}:rw",
             image, "sleep", "infinity"]
    rc, out = _podman(*argv)
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


def _scrub_work_root(work_root: str) -> None:
    """Empty a container case's work_root from inside the user namespace.
    Case processes create files as container uids that map to subuids the
    host user can neither unlink in a read-only dir nor chmod (lean4's
    .lake is the poster case) — the TemporaryDirectory cleanup then dies
    with PermissionError and the whole CASE crashes unrecorded.
    `podman unshare` runs with capabilities over the subuid range, so it
    removes what the plain host user cannot. Best-effort: the host rmtree
    afterwards only has to handle an empty, host-owned root."""
    rc, out = _podman("unshare", "sh", "-c",
                      'rm -rf -- "$1"/* "$1"/.[!.]* "$1"/..?* 2>/dev/null; '
                      'exit 0', "_", work_root, timeout=120)
    if rc != 0:
        log.info("eval work_root scrub failed (%s): %s", work_root,
                 out.decode("utf-8", "replace").strip()[-200:])


# ---- deterministic expectation checks ---------------------------------------

_SKILL_LOAD_RE = re.compile(r"skill\.load\(([^)…\s]+)")
_SKILL_BODY_CAP = 1500      # chars per skill body handed to the judge


def _skill_loads_from_trace(run_ids: list[str]) -> set[str]:
    """Skill names loaded during the given runs, read from the eval runs' own
    trace rows. The trajectory display string keeps only the most recent 14
    tool entries, so a skill.load in iteration 1 of a long run is truncated
    away before the judge (or _SKILL_LOAD_RE) ever sees it — the judge then
    wrongly fails the case on "skill never loaded". Trace tool_result rows
    carry the call args; anything odd (missing db, schema drift) degrades to
    the trajectory regex rather than failing the eval."""
    ids = [r for r in run_ids if r]
    if not ids:
        return set()
    try:
        import sqlite3

        from runtime import paths
        con = sqlite3.connect(f"file:{paths.TRACE_DB}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(ids))
            rows = con.execute(
                f"SELECT payload_json FROM events WHERE kind='tool_result' "
                f"AND run_id IN ({marks})", ids).fetchall()
        finally:
            con.close()
    except Exception:
        return set()
    names: set[str] = set()
    for (pj,) in rows:
        try:
            d = json.loads(pj)
        except Exception:
            continue
        if d.get("tool") == "skill.load" and d.get("status") == "ok":
            name = (d.get("args") or {}).get("name")
            if name:
                names.add(str(name))
    return names


def _loaded_skill_bodies(turns: list[dict],
                         skills_dir: str | Path | None = None,
                         extra_names: set[str] | None = None) -> dict[str, str]:
    """Bodies of the skills the agent actually loaded, so the judge can ground
    skill-tweak proposals in the instructions the agent really followed.
    Layered: custom wins. Names come from the trajectory's skill.load(<name>)
    hints plus `extra_names` (trace-derived — survives trajectory truncation).
    `skills_dir` honours the runtime's skills.dir override (audit A3)."""
    names: set[str] = set(extra_names or ())
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


# Host-side cap for a case's grading script. Container cases may run the
# task's official run-tests.sh at grade time (apt/pip toolchain installs
# before pytest), so this must cover minutes, not seconds; the inner podman
# exec timeouts bound each step, this is the outer backstop.
_CHECKER_TIMEOUT_S = 480


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
- The "tools called" line is the COMPLETE list of tools invoked that turn — treat it as authoritative for WHETHER a tool ran. Trajectory lines (tool(arg)→status) show only the most recent calls with their arguments: a tool missing from the trajectory but present in "tools called" DID run. A →ok call DID execute and return output even though you cannot see it — never infer fabricated results from that absence.
- The LIVE SYSTEM PROMPT is what the agent actually saw: do not propose wording it already contains.
- tool-description proposals must come with a complete replacement description in proposed_content (not a diff, not advice) and target set to the tool name.
- RELEVANT CONFIG shows "case_budget": this case's OWN per-case budget, which overrides the global budgets for this run. Never propose global budget, wall-clock, timeout, LiteLLM/proxy, or network/sandbox changes — those are operator settings that one case's failure must never move. Wall-clock/iteration exhaustion under an adequate case budget is a model limitation: classification "none" (say so in notes), or "bad-test" only when the case is under-budgeted or impossible BY DESIGN.
- config proposals may ONLY target: budgets.max_iterations, budgets.max_cost_usd, budgets.max_total_tokens, loop_guard.max_rejections, architect.threshold, eval.max_cost_usd, eval.suite_max_cost_usd, eval.adaptive_max_turns. A fix needing anything else is bug-for-dev (harness code) or bad-test (case design), never config.
- pass and score MUST agree: pass=true requires score ≥ 7, pass=false requires score < 7. A high score with pass=false (or a low score with pass=true) is a contradictory verdict — re-decide before replying.
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
        tools = t.get("tools") or []
        if tools:
            lines.append(f"tools called ({len(tools)}, complete): "
                         + ", ".join(tools))
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
    used_fallback = False
    if not parsed and str(ecfg["judge_model"]) != _FALLBACK_ALIAS:
        # Garbage content (HTTP 200, no JSON) never raises, so the alias
        # fallback inside _model_text doesn't fire — try the local judge
        # explicitly before giving up. Found live: OpenRouter autoroutes
        # glm-5.2 across upstream providers and some return junk for
        # json_object calls; simple-shuffle keeps both attempts on the same
        # bad route, so the retry above can't rescue it either.
        r = await _model_text(
            cfg, _FALLBACK_ALIAS,
            [{"role": "system", "content": _JUDGE_SYSTEM},
             {"role": "user", "content": "\n".join(lines)}],
            temperature=float(ecfg["judge_temperature"]), want_json=True,
            max_tokens=_JUDGE_MAX_TOKENS)
        out["cost_usd"] += r["cost_usd"]
        out["tokens"] += r["tokens"]
        out["judge_model"] = r["model_name"]
        truncated = truncated or r.get("finish_reason") == "length"
        parsed = _parse_json(r["content"])
        used_fallback = bool(parsed)
    if not parsed:
        head = (r["content"] or "").strip()[:200]
        out["notes"] = (("judge verdict truncated at the token cap"
                         if truncated else "judge returned unparseable JSON")
                        + (f" — content head: {head!r}" if head
                           else " — empty content"))
        out["error"] = "bad judge json"
        return out
    out["pass"] = bool(parsed.get("pass"))
    try:
        out["score"] = max(0.0, min(10.0, float(parsed.get("score"))))
    except (TypeError, ValueError):
        out["score"] = None
    for k in ("notes", "classification", "what", "cause", "fix"):
        out[k] = str(parsed.get(k) or "")[:2000]
    if used_fallback:
        out["notes"] = (out["notes"] + " [graded by the fallback judge — the "
                                       "primary returned unparseable content]").strip()
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
    # The seed travels on stdin, never argv: a single argv string is
    # kernel-capped (MAX_ARG_STRLEN, 128 KiB) and GAIA attachment seeds
    # sail past it — the case then dies with E2BIG before turn one.
    proc = subprocess.run(
        [sys.executable, "-"], input=seed.encode("utf-8"),
        cwd=str(files_dir),
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

    `variant` (benchmarks): {"label", "model", "sampling", "harness"} — the
    model alias and sampler overrides the run executes under; `label` is
    recorded as the result's brain so variants compare apples-to-apples in
    the stats. harness "brain" strips the delegation verbs
    (_BRAIN_VARIANT_EXCLUDED) for a brain-only A/B against full routing."""
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
    if (variant or {}).get("harness") == "brain":
        tools = [t for t in tools if t not in _BRAIN_VARIANT_EXCLUDED]
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
    # Per-case budget override wins over the global eval.turn_wall_clock_s —
    # marathon benchmark cases (Terminal-Bench full) carry their own cap so one
    # stuck task can't eat the suite's whole evening.
    twc = (case.budget or {}).get("turn_wall_clock_s")
    if twc is None:
        twc = int(ecfg.get("turn_wall_clock_s") or 0)
    budget = {"max_iterations": 0,
              "max_wall_clock_s": int(twc),
              "wall_clock_grace_s": int(ecfg.get("wall_clock_grace_s") or 0),
              "wall_clock_max_extensions": int(
                  ecfg.get("wall_clock_max_extensions") or 0),
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
            # route code.run/code.execute into it — ONLY via run_overrides
            # tools_patch; no config path turns container mode on for chats.
            # Network defaults ON: official Terminal-Bench allows downloads
            # (throwaway container, no credentials); a case can opt out with
            # container.network: false.
            ctr_workdir = str(case.container.get("workdir") or "/app")
            Path(work_root).mkdir(parents=True, exist_ok=True)
            if case.container.get("compose"):
                # Multi-service task: the whole compose stack comes up
                # project-scoped (siblings DNS-reachable from the client);
                # the returned dict carries the teardown data.
                container, err = _container_start_compose(case, ctr_workdir,
                                                          work_root)
                cid = (container or {}).get("id")
            else:
                cid, err = _container_start(str(case.container["image"]),
                                            ctr_workdir, work_root,
                                            network=bool(
                                                case.container.get(
                                                    "network", True)))
            if cid is None:
                return {"test_id": case.id, "skipped": True, "cost_usd": 0.0,
                        "note": f"container failed to start: {err}"}
            if container is None:
                container = {"id": cid, "workdir": ctr_workdir,
                             "python": "python3"}
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
                # Outage brake: a dead model backend fails every later case
                # instantly — stop the suite instead of poisoning the queue.
                # But first give a TRANSIENT outage (litellm restart on a
                # cloud-catalog edit or deploy, ~10-30s) a chance to recover:
                # one failed case must not sacrifice the whole queue.
                if result.get("status") == "error":
                    ans = str(result.get("answer") or "")
                    if any(n in ans for n in _BACKEND_DOWN_NEEDLES):
                        if not await _backend_recovered(runtime.config):
                            raise BackendDownError(ans[:200])
                        log.warning("eval case %s: backend outage recovered, "
                                    "suite continues (this case still failed "
                                    "its turn)", case.id)
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
                if container.get("compose_project"):
                    _compose_stack_down(container)
                else:
                    _container_stop(container["id"])
                # The container ran as other uids — scrub the mount from
                # inside the userns or the sandbox rmtree can crash the
                # whole case AFTER grading (unrecorded result).
                _scrub_work_root(str(work_root))

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
            skills_dir=(runtime.config.get("skills") or {}).get("dir"),
            extra_names=_skill_loads_from_trace(run_ids)),
        "config": {
            "eval": ecfg,
            # The case's own budget wins over the globals at run time — the
            # judge must see it, or it proposes global budget changes for
            # per-case marathons (inbox noise, and dangerous to accept).
            "case_budget": case.budget or {},
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
    abort_note = None
    for case in cases:
        if cancelled or (should_stop is not None and should_stop()):
            cancelled = True
            rows.append({"test_id": case.id, "skipped": True,
                         "note": abort_note or "cancelled by admin"})
            continue
        if spent >= cap:
            rows.append({"test_id": case.id, "skipped": True,
                         "note": f"suite cost cap ${cap:.2f} reached"})
            continue
        try:
            row = await run_case(runtime, case, store,
                                 disabled_tools=disabled_tools,
                                 variant=variant)
        except BackendDownError as e:
            # Backend died mid-suite: every later case would fail instantly
            # and pollute the statistics. Record the abort and skip the rest.
            log.error("eval suite aborted: model backend unreachable (%s)", e)
            row = {"test_id": case.id, "skipped": True, "cost_usd": 0.0,
                   "note": f"suite aborted: model backend unreachable ({e})"}
            cancelled = True
            abort_note = row["note"]
        except Exception as e:
            log.exception("eval case %s crashed", case.id)
            row = {"test_id": case.id, "passed": False, "score": None,
                   "judge_notes": f"runner crashed: {type(e).__name__}: {e}",
                   "cost_usd": 0.0}
            # A crashed case must still leave a ledger row — an invisible
            # failure reads as "the suite never ran it" (and did: seed_code
            # E2BIG and container-uid cleanup crashes looked exactly like
            # that). The record itself is best-effort: a store hiccup must
            # not take the rest of the suite down.
            try:
                row = store.record_result(
                    test_id=case.id, passed=False, score=None,
                    judge_notes=row["judge_notes"], judge_model=None,
                    cost_usd=0.0, tokens=0, elapsed_s=0.0,
                    status="crashed", run_ids=[], transcript=[],
                    brain=((variant or {}).get("label")
                           or getattr(runtime, "model", None)),
                    benchmark=variant is not None)
            except Exception:
                log.exception("eval case %s: crash row failed to record",
                              case.id)
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
            # Compact skip ledger — run-status drops the full results for
            # size, and without this a skipped case vanishes silently
            # ("50 evals but only 38 ran" with no way to see why).
            "skipped": [{"test_id": r["test_id"],
                         "note": str(r.get("note") or "")}
                        for r in rows if r.get("skipped")],
            "cost_usd": round(spent, 6), "results": rows}
