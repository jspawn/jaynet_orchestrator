"""specialist.delegate — hand a self-contained task to a specialist sub-agent.

Why this exists: the orchestrator brain is a small-active-param MoE tuned for fast
tool-routing, not heavy domain work, and everything it reads stays in ITS
context for the rest of the run. Offloading the actual work to a sub-agent that
runs on a dedicated specialist model (served on its own GPU slot and registered
as a LiteLLM alias) wins twice: a model strong at the task does it, and the
bulky working transcript — file reads, diffs, test logs, search dumps — stays
in the CHILD's context, never the parent's.

This is a thin, opinionated front door over agent.spawn: it picks the
specialist model (configured alias → live specialist slot whose preset carries
the requested strength → a stopped tagged LOCAL preset swapped onto its slot →
default brain) and the working tool-set by default, so the brain delegates
with one call instead of having to remember to pass model= and the right
tools= to agent.spawn. It inherits all of spawn's guarantees (child tools ⊆
parent tools, child's write/commit still prompts the human, spend counts
against the parent).

Routing: coding is the default route — the tool predates the strength system
and was coding-only, which the historical config path below still reflects.
The `strength` parameter routes to other specialists instead (research,
security, multi-step, allround, … — whatever preset strength tags are
configured; an exact tag beats the 'allround' catch-all). Falls back to the
default brain when no alias is configured and no matching specialist is live.

WHEN to use it: a self-contained, multi-step task (implement X, refactor Y,
fix a failing test, audit Z, research W). NOT for a single edit or lookup you
can do inline, and NOT a substitute for the coding-projects plan→unit
discipline on a large build — delegate one unit at a time.

Verification is ground-truth, not self-report: `verify` gates the child's "done"
on an executed check (hash-guarded against test tampering). When the call doesn't
set one and the config doesn't pin one, the workspace's standard test command is
auto-attached when detectable (pytest/npm/make/go/cargo — config
`tools.code.delegate.auto_verify`), and testable-smelling tasks that still go out
unchecked get a verify nudge in the result (`verify_nudge`).

The no-test-suite gap (agent.verify_delegate_authored_check, default on): when no
test command is detectable at all, the task gains a mandatory block telling the
specialist to FIRST author a small check encoding the task's examples/acceptance
criteria, run it, report its raw output, and end its report with a final
`CHECK: <command>` line. The harness then re-runs that command mechanically —
through the exact verify sandbox (runtime.verify.run_authored_check), never raw —
and attaches a deterministic `verified` flag to the delegation result, so the
brain reads one line instead of judging the specialist's self-report.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.git.status import _git


async def _progress(ctx: ToolContext, label: str, *, ok: bool | None = None) -> None:
    """Stage line into the chat's live activity feed (no-op without a sink).

    The delegate's slow phases — routing, model swap out/in, swap-back —
    otherwise sit silent for tens of seconds with just a spinner.
    """
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    try:
        await emit("progress", {"label": label, "type": "stage",
                                **({"ok": ok} if ok is not None else {})})
    except Exception:
        pass  # the feed is cosmetic — never break a delegation over it

# Sensible default tool-set for a coding child: navigate, edit, verify, checkpoint.
_DEFAULT_CODING_TOOLS = [
    "fs.read", "fs.list", "fs.grep", "fs.write", "fs.edit",
    "code.run", "code.symbols", "code.tree", "code.patch", "code.deps",
    "lint.run", "test.run",
    "git.status", "git.diff", "git.log", "git.show", "git.add", "git.commit",
    "git.branch", "git.stash", "git.restore", "git.worktree",
    "skill.load", "skill.list", "note.set", "context.pin",
]

# Research children need the web lane, not the git lane. Live evidence: a
# strength='research' task went out with the coding set — no web.search at
# all. The child loaded the web-research skill, found none of the tools it
# prescribed, could not widen (a delegate child's set is caller-fixed), and
# looped skill.load into the loop guard for 12 iterations. The strength
# routes the MODEL; it must route the TOOLSET too.
_DEFAULT_RESEARCH_TOOLS = [
    "web.search", "web.fetch", "web.request", "web.render", "browser.pdf",
    "rag.search",
    "fs.read", "fs.list", "fs.grep", "fs.write",
    "code.run",
    "skill.load", "skill.list", "note.set", "context.pin",
]

# Security and multi-step work are coding-first but routinely need a lookup
# lane (CVE/PoC references, external specs) — coding set plus web basics.
_DEFAULT_SECURITY_TOOLS = _DEFAULT_CODING_TOOLS + [
    "web.search", "web.fetch", "web.request"]

_STRENGTH_TOOLS = {
    "research": _DEFAULT_RESEARCH_TOOLS,
    "security": _DEFAULT_SECURITY_TOOLS,
    "multi-step": _DEFAULT_SECURITY_TOOLS,
    "allround": _DEFAULT_SECURITY_TOOLS,
}


def _default_tools_for(strength: str) -> list[str]:
    """The default child toolset follows the routing strength."""
    return _STRENGTH_TOOLS.get(strength, _DEFAULT_CODING_TOOLS)

# A child without at least one of these can read and lint but never produce
# a file — live eval evidence: the brain once passed tools=["lint.run"], the
# child came back empty-handed and the parent wrote the code inline (the
# exact failure mode delegation exists to prevent). Such overrides are
# rejected below instead of spawning a helpless child.
_MUTATION_TOOLS = frozenset({"fs.write", "fs.edit", "code.patch", "code.run"})


def _cfg(ctx: ToolContext) -> dict:
    # Historical config path, kept for compatibility: the tool was
    # code.delegate before the rename to specialist.delegate, and existing
    # runtime.yaml files still configure it under tools.code.delegate.*.
    return (ctx.config.get("tools", {}).get("code", {}) or {}).get("delegate", {}) or {}


async def _make_worktree(ctx: ToolContext) -> dict:
    """Create a throwaway worktree for an isolated delegate run. Returns
    {"path", "branch", "repo"} or {"error": ...}. Placed inside the workspace
    (.jaynet-worktrees/) so the child's confinement stays exactly the same
    shape as the parent's."""
    if not getattr(ctx, "work_root", None):
        return {"error": "isolated=true needs a workspace (work_root), "
                         "which this run doesn't have"}
    repo = Path(ctx.work_root).resolve()
    rc, out, err = await _git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return {"error": "isolated=true needs the workspace to be a git "
                         f"repository ({err.strip() or 'not a repo'})"}
    # Base SHA, so the report can tell "child committed its work" apart from
    # "child produced nothing" — diff/status only see UNCOMMITTED state.
    rc, out, _ = await _git(repo, "rev-parse", "HEAD")
    base = out.strip() if rc == 0 else None
    # Per-call suffix: a second isolated delegate in the same run (or a
    # leftover from a crashed one) must not collide on branch/path.
    short = f"{(ctx.request_id or 'run')[:8]}-{uuid.uuid4().hex[:6]}"
    branch = f"jaynet/{short}"
    dest = repo / ".jaynet-worktrees" / short
    rc, out, err = await _git(repo, "worktree", "add", "-b", branch,
                              str(dest), timeout=60)
    if rc != 0:
        return {"error": f"could not create the worktree: {err.strip() or out.strip()}"}
    # Keep the scratch dir out of the user's git status: .git/info/exclude is
    # per-repo and never committed (we must not touch the repo's .gitignore).
    gitdir = repo / ".git"
    if gitdir.is_dir():
        try:
            exclude = gitdir / "info" / "exclude"
            cur = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if ".jaynet-worktrees/" not in cur:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as fh:
                    fh.write(".jaynet-worktrees/\n")
        except OSError:
            pass
    return {"path": str(dest), "branch": branch, "repo": str(repo),
            "base": base}


def _detect_verify_command(work_root, *, local_venv: bool = True) -> str | None:
    """Best-effort ground-truth check command for a workspace (auto-verify):
    the standard test entry point per ecosystem, or None when the workspace
    advertises none. `local_venv=False` for isolated worktrees — a git
    worktree carries tracked files only, so an untracked .venv is absent
    there and the uv/system fallback must be picked instead."""
    if not work_root:
        return None
    root = Path(work_root)
    try:
        if ((root / "pyproject.toml").exists() or (root / "pytest.ini").exists()
                or (root / "setup.cfg").exists()):
            if (root / "tests").is_dir() or list(root.glob("test_*.py")):
                if local_venv and (root / ".venv/bin/python").exists():
                    return ".venv/bin/python -m pytest -q"
                if (root / "uv.lock").exists():
                    return "uv run --no-sync pytest -q"
                return "python3 -m pytest -q"
        pkg = root / "package.json"
        if pkg.exists():
            import json as _json
            scripts = (_json.loads(pkg.read_text()) or {}).get("scripts") or {}
            if "test" in scripts:
                if (root / "pnpm-lock.yaml").exists():
                    return "pnpm test"
                if (root / "yarn.lock").exists():
                    return "yarn test"
                return "npm test"
        mk = root / "Makefile"
        if mk.exists() and re.search(r"^test:", mk.read_text(), re.M):
            return "make test"
        if (root / "go.mod").exists():
            return "go test ./..."
        if (root / "Cargo.toml").exists():
            return "cargo test"
    except Exception:
        return None
    return None


# Task texts that smell testable — used for the one-shot verify nudge when a
# delegation goes out with no ground-truth check attached.
_VERIFY_SMELL = ("failing test", "tests fail", "test fails", "fix the bug",
                 "fix the failing", "make the test", "passes the test",
                 "write tests", "add tests", "with tests", "test suite")


# ---- specialist-authored checks (agent.verify_delegate_authored_check) --------
# The no-test-suite gap: auto_verify can only attach a command the workspace
# advertises. When it can't, the specialist (the strong model) authors the
# ground truth instead of the brain (the weak one) having to judge a report.
# Contract: the specialist writes a check into the workspace and ends its
# report with a final `CHECK: <command>` line; the harness re-runs that line
# mechanically through the verify sandbox and the exit code is the verdict.
_AUTHORED_CHECK_INSTRUCTION = (
    "VERIFICATION (mandatory): no project test command exists for this "
    "workspace, so you must create the ground truth yourself.\n"
    "1. FIRST write a small check (script or test file) into the workspace "
    "that encodes the task's examples / acceptance criteria.\n"
    "2. Run it, and iterate until it passes for the right reason — never "
    "weaken the check to make it pass.\n"
    "3. Report its RAW output verbatim in your final report: the command, "
    "its exit code, and its output.\n"
    "4. End your final report with ONE final line — nothing after it — "
    "naming the check command for the harness to re-run mechanically:\n"
    "CHECK: <command>\n"
    "The harness executes this command sandboxed in the workspace; exit 0 is "
    "the only accepted proof of done."
)

# Strictly the LAST non-empty line of the report: a `CHECK:` mention anywhere
# earlier (quoting the instruction, explaining the plan) must never execute.
_CHECK_LINE_RE = re.compile(r"^CHECK:\s*(\S.*?)\s*$")


def _authored_check_enabled(config: dict) -> bool:
    return bool((config.get("agent") or {}).get(
        "verify_delegate_authored_check", True))


def _parse_check_command(answer: str) -> str | None:
    """The authored-check command from a specialist report, or None when the
    report doesn't end with a well-formed `CHECK: <command>` line (absent or
    malformed = no execution, pre-feature behavior)."""
    for line in reversed((answer or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _CHECK_LINE_RE.match(line)
        return m.group(1) if m else None
    return None


async def _worktree_report(wt: dict) -> dict:
    """What changed in the isolated worktree: commits on its branch, diff
    stat, untracked files. Cleans up automatically only when the child
    produced NOTHING (no commits, no diff, no untracked) — and never when
    inspection itself failed: a git error must not cascade into deleting
    work."""
    wtp = Path(wt["path"])
    rc1, stat, _ = await _git(wtp, "diff", "HEAD", "--stat")
    rc2, porcelain, _ = await _git(wtp, "status", "--porcelain")
    commits, rc3 = "0", 0
    if wt.get("base"):
        rc3, commits, _ = await _git(wtp, "rev-list", "--count",
                                     f"{wt['base']}..HEAD")
    if rc1 != 0 or rc2 != 0 or rc3 != 0:
        return {"worktree": wt["path"], "branch": wt["branch"],
                "warning": "could not inspect the worktree — left in place; "
                           "review it with the git tools, then merge or discard"}
    untracked = sorted(l[3:] for l in porcelain.splitlines()
                       if l.startswith("?? "))
    n_commits = int(commits.strip() or 0)
    changed = bool(n_commits or stat.strip() or untracked
                   or any(not l.startswith("?? ") for l in porcelain.splitlines()))
    if not changed:
        await _git(Path(wt["repo"]), "worktree", "remove", "--force", str(wtp))
        await _git(Path(wt["repo"]), "branch", "-D", wt["branch"])
        return {"cleaned_up": True}
    return {
        "worktree": wt["path"], "branch": wt["branch"],
        "commits": n_commits,
        "diff_stat": stat.strip()[-2000:],
        "untracked": untracked[:50],
        "next": ("Review with git.diff/git.show on the worktree, then merge or "
                 "cherry-pick with the git tools (confirmation-gated) — or "
                 "discard with git.worktree remove (force) and git.branch -D."),
    }


class SpecialistDelegate(Tool):
    name = "specialist.delegate"
    description = (
        "Delegate a self-contained task to a specialist sub-agent (keeps the "
        "heavy working transcript — file reads, diffs, test logs — out of your "
        "context and puts the work on the model strong at it). Coding is the "
        "default route; pass strength='<tag>' (e.g. 'security', 'research', "
        "'multi-step', 'allround') to route to the specialist carrying that "
        "preset tag instead. Give a COMPLETE, standalone task — the child sees "
        "none of this conversation, so include the context/paths, what to do, "
        "and the done-check. Prefer passing verify='<test command>' — when you "
        "don't and the workspace advertises a standard test setup, it is "
        "attached automatically (auto_verify). Use for multi-step work; do a "
        "one-line edit yourself. Returns only the child's final summary."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Complete, standalone instruction. Include the "
                               "project path or context, the work to do, and how "
                               "to verify it (the command/test that must pass).",
            },
            "tools": {
                "type": "array", "items": {"type": "string"},
                "description": "Override the tool-set given to the child. The "
                               "default follows the strength: coding gets "
                               "fs/code/git/lint/test, research gets the web "
                               "lane (web.search/fetch/request/render, "
                               "browser.pdf, rag.search) + fs + code.run, "
                               "security/multi-step/allround get coding + web "
                               "basics. Overrides can only "
                               "narrow your own tools, never exceed them.",
            },
            "model": {
                "type": "string",
                "description": "Override the specialist model alias (default: the "
                               "configured delegate model → the live specialist "
                               "whose preset carries the requested strength → "
                               "the default brain).",
            },
            "strength": {
                "type": "string",
                "description": "Route to the specialist carrying this preset strength "
                               "tag (e.g. 'security') instead of the default coding "
                               "route. A tagged LOCAL preset that isn't live is "
                               "swapped onto its slot automatically (the occupant is "
                               "stopped) — no manual model.use needed. With no tagged "
                               "preset at all, the allround specialist takes it.",
            },
            "budget": {
                "type": "object",
                "description": "Optional sub-budget caps (max_cost_usd, "
                               "max_iterations, max_total_tokens, max_wall_clock_s).",
            },
            "verify": {
                "description": "Ground-truth done-check the coder must satisfy before "
                               "returning — a command string ('pytest -q', 'ruff check "
                               ".', 'npm test') or {command, protect, max_checks}. Until "
                               "it exits 0 the child keeps iterating on the failure "
                               "output, and it cannot edit the tests to pass them "
                               "(they're hash-guarded). Strongly prefer setting this: a "
                               "coding loop gated on real tests is the difference between "
                               "'looks done' and 'is done'.",
            },
            "isolated": {
                "type": "boolean",
                "description": "Run the coder in a throwaway git worktree "
                               "(<workspace>/.jaynet-worktrees/<id>, own branch) "
                               "instead of the live workspace. The real tree stays "
                               "untouched; you review the diff afterwards and merge "
                               "or discard it with the git tools. Requires the "
                               "workspace to be a git repository.",
            },
            "fresh": {
                "type": "boolean",
                "description": "De-anchored retry: skip the orientation pack (repo "
                               "map, project instructions) so the child sees ONLY "
                               "your task text. Use for a deliberate from-scratch "
                               "retry when an earlier attempt at the same task "
                               "failed — the pack can anchor the child on the dead "
                               "approach. The harness sets this automatically after "
                               "repeated failures of the same task.",
            },
        },
        "required": ["task"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.spawn is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="sub-agents are not available in this runtime")
        task = (args.get("task") or "").strip()
        if not task:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="task is required")
        task_smells_testable = any(k in task.lower() for k in _VERIFY_SMELL)

        cfg = _cfg(ctx)
        model = args.get("model") or cfg.get("model")  # explicit wins
        routed = False
        swap_note = None
        evicted: list[dict] = []        # what the swap stopped — restored below
        wanted = str(args.get("strength") or cfg.get("strength") or "coding")
        if model is None:
            # Model priority by strengths (preset tags): work belongs on the
            # model strong at it, not the default brain. A tagged-but-stopped
            # LOCAL preset is swapped onto its slot (freeing the occupant,
            # e.g. the coder on GPU 1) instead of settling for the allround
            # model; no tag preset at all → the allround specialist.
            from tools.model.catalog import (
                ModelUse,
                route_strength,
                route_strength_exact,
                strength_route,
            )
            plan = await strength_route(ctx.config, wanted)
            if plan.get("mode") == "swap":
                await _progress(ctx, f"route: {wanted} → swapping in "
                                     f"'{plan['preset']}' (loading takes a "
                                     "moment)…")
                # Brain eviction is allowed HERE (internal ctx flag, not a
                # model-facing argument): the incoming specialist may need
                # GPUs the brain sits on (e.g. a 2-card brain), and the
                # swap-back below restores it before the parent's next turn.
                ctx._allow_brain_evict = True
                try:
                    res = await ModelUse().execute(
                        {"preset": plan["preset"], "swap": True,
                         "include_brain": True}, ctx)
                finally:
                    ctx._allow_brain_evict = False
                ok = (res.status == "ok"
                      and not (res.result or {}).get("hint"))
                if ok:
                    evicted = list((res.result or {}).get("evicted") or [])
                    # ServeStart accepted the launch, but the model loads for
                    # tens of seconds — a single immediate confirm probe sees
                    # a still-empty port and would fall back to the brain
                    # with the specialist already stopped (live evidence:
                    # first v1.7.3 swap). Poll until the exact holder answers.
                    try:
                        wait_s = float(cfg.get("swap_wait_s", 120))
                    except (TypeError, ValueError):
                        wait_s = 120.0
                    deadline = time.monotonic() + max(0.0, wait_s)
                    confirm = None
                    while time.monotonic() < deadline:
                        confirm = await route_strength_exact(ctx.config, wanted)
                        if confirm:
                            break
                        await asyncio.sleep(2.0)
                    ok = bool(confirm)
                if ok:
                    model, routed = plan["alias"], True
                    swap_note = (f"'{plan['preset']}' was swapped onto its "
                                 f"slot for this {wanted} task (the slot's "
                                 "previous model was stopped)")
                    await _progress(ctx, f"'{plan['preset']}' loaded — "
                                         f"running the {wanted} task", ok=True)
                else:
                    swap_note = (f"could not swap in '{plan['preset']}' "
                                 f"({res.error or (res.result or {}).get('status')})"
                                 " — fell back to the allround route")
                    await _progress(ctx, f"swap of '{plan['preset']}' failed — "
                                         "falling back to the allround route",
                                    ok=False)
                    plan = {"mode": "allround",
                            "alias": await route_strength(ctx.config, wanted)}
            if model is None and plan.get("alias"):
                model, routed = plan["alias"], True
            if routed and not swap_note:
                await _progress(ctx, f"route: {wanted} → {model}")
        tools = (args.get("tools") or cfg.get("tools")
                 or _default_tools_for(wanted))
        if not (set(map(str, tools)) & _MUTATION_TOOLS):
            return ToolResult(
                status="error", result=None, tool_name=self.name,
                error="the child tool-set has no way to produce files (needs "
                      "at least one of fs.write / fs.edit / code.patch / "
                      "code.run) — pass a broader `tools` set or drop the "
                      "override for the default coding set")
        budget = args.get("budget") or cfg.get("budget")
        if budget is None:
            # Coding-appropriate default: implement + test + fix + verify
            # doesn't fit in the fleet-wide fan-out default (8) — a capped-out
            # child returns half-done work and the brain re-does it inline,
            # paying the wall-clock twice. Only iterations; cost/tokens/wall
            # still inherit the parent's remaining allowance.
            budget = {"max_iterations": int(cfg.get("default_iterations") or 24)}
        verify = args.get("verify") or cfg.get("verify")   # gate on tests when given

        # Orientation pack: repo map + project instructions (AGENTS.md & co) —
        # the child starts with an empty context and would otherwise burn its
        # first iterations rediscovering the layout (runtime/context_pack.py).
        # Skipped on fresh=True: a de-anchored retry must see ONLY the task —
        # the pack carries the framing of the approach that already failed.
        if not args.get("fresh"):
            from runtime.context_pack import coding_context
            pack = coding_context(getattr(ctx, "work_root", None), ctx.config)
            if pack:
                task = pack + "\n\nTASK:\n" + task

        # Isolated mode: the coder works in a throwaway git worktree, the live
        # tree stays untouched, and the caller reviews/merges/discards the diff
        # afterwards with the (confirmation-gated) git tools.
        isolated = args.get("isolated")
        if isolated is None:
            isolated = bool(cfg.get("isolated", False))
        wt = None
        if isolated:
            wt = await _make_worktree(ctx)
            if wt.get("error"):
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=wt["error"])

        # Auto-verify (tools.code.delegate.auto_verify, default on): when no
        # explicit check was passed or configured, attach the workspace's
        # standard test command if it advertises one — executed tests as the
        # ground-truth done-check instead of the child's self-report. In
        # isolated mode the worktree has no untracked .venv, so detection
        # skips the local-venv command variant.
        auto_verify = None
        if verify is None and bool(cfg.get("auto_verify", True)):
            auto_verify = _detect_verify_command(
                getattr(ctx, "work_root", None), local_venv=not wt)
            if auto_verify:
                # A full project suite outlives the verifier's default 180s
                # check timeout (ours takes minutes) — auto-attached checks
                # get their own, larger one. Note the baseline pre-run also
                # runs the command once before the child starts (pre-existing
                # red detection); on huge suites pin a focused command via
                # tools.code.delegate.verify instead.
                try:
                    _vto = int(cfg.get("verify_timeout_s", 600))
                except (TypeError, ValueError):
                    _vto = 600
                verify = {"command": auto_verify, "timeout_s": _vto}

        # Specialist-authored check (agent.verify_delegate_authored_check,
        # default on): still no ground-truth command (nothing passed,
        # configured, or auto-detected) → the task gains a mandatory block:
        # the specialist FIRST writes a small check encoding the task's
        # examples/acceptance criteria, then ends its report with a final
        # `CHECK: <command>` line the harness re-runs mechanically below.
        # Needs a workspace — the contract is "write the check INTO the
        # workspace" and the re-run is confined to it.
        want_authored_check = (
            verify is None
            and bool(getattr(ctx, "work_root", None))
            and _authored_check_enabled(ctx.config))
        if want_authored_check:
            task = task + "\n\n" + _AUTHORED_CHECK_INSTRUCTION

        # Swap-back: return the hardware to whatever the swap evicted (the
        # brain first) before the parent's next turn — opt out with
        # models.swap_back: false. Runs even when the child raises; restore
        # failures surface in the result, never silently.
        swap_back_note = None
        # Worker mode (agent.worker_prompt, shipped on): the child gets the
        # lean worker prompt (prompts/worker.md + the tag module for `wanted`)
        # as its base system prompt instead of the full orchestrator gate
        # prompt, whose routing doctrine a worker must never follow. None →
        # pre-flag behavior (gate prompt).
        _base = None
        if bool((ctx.config.get("agent") or {}).get("worker_prompt", False)):
            from runtime import worker_prompt as _worker_prompt
            _base = _worker_prompt.resolve(wanted, ctx.config)
        try:
            from runtime.tool_base import role_sampling
            # sampling only when a role temperature applies — older custom
            # spawn wrappers must keep working without the kwarg.
            _rs = role_sampling(ctx.config, wanted)
            child = await ctx.spawn(task, tools=tools, model=model,
                                    name="coder", budget=budget, verify=verify,
                                    **({"sampling": _rs} if _rs else {}),
                                    **({"base_system": _base}
                                       if _base else {}),
                                    work_root_path=(wt["path"] if wt else None))
        finally:
            if evicted and bool((ctx.config.get("models") or {}).get(
                    "swap_back", True)):
                await _progress(ctx, "handing the hardware back — restoring "
                                     "the evicted models…")
                from tools.model.catalog import restore_evicted
                notes = await restore_evicted(ctx, evicted)
                failed = [n for n in notes if n.startswith("FAILED")]
                swap_back_note = "; ".join(notes)
                if failed:
                    swap_back_note += (" — the brain/specialist may be DOWN; "
                                       "check Admin → Processes before the "
                                       "next prompt")
                    await _progress(ctx, "restore FAILED — the brain may be "
                                         "down, check Admin → Processes",
                                    ok=False)

        from runtime.tool_base import cutoff_child_answer
        answer, cutoff_hint = cutoff_child_answer(child)
        # Authored-check re-run: the specialist's report ends with
        # `CHECK: <command>` → execute it mechanically through the SAME
        # sandbox as an auto_verify command (runtime.verify.run_authored_check
        # — fail-closed, scrubbed env, confined to the work dir; the command
        # is model-written, never run raw). The exit code becomes the
        # delegation's deterministic `verified` verdict — the brain reads one
        # line instead of judging the specialist's self-report. Parsed from
        # the RAW answer (the cutoff envelope may have truncated the tail).
        authored_check = None
        if want_authored_check:
            check_cmd = _parse_check_command(str(child.get("answer") or ""))
            if check_cmd:
                from runtime.verify import run_authored_check
                authored_check = await run_authored_check(
                    check_cmd,
                    (wt["path"] if wt else getattr(ctx, "work_root", None)),
                    ctx.config)
        result = {
            "agent": "coder",
            "model": model or "(default brain)",
            "status": child.get("status"),
            "verified": (authored_check["verified"] if authored_check
                         else child.get("verified")),   # True/False/None
            "verify_command": child.get("verify_command"),
            "files_changed": child.get("files_changed") or [],
            "answer": answer,
            "sub_run_id": child.get("run_id"),
            "budget": child.get("budget"),
        }
        if authored_check:
            result["authored_check"] = authored_check
        if cutoff_hint:
            result["hint"] = cutoff_hint
        if auto_verify:
            result["verify_auto"] = (
                f"no verify given — auto-attached the workspace's standard "
                f"check `{auto_verify}` (tools.code.delegate.auto_verify)")
        elif (authored_check is None and verify is None
              and task_smells_testable
              and bool(cfg.get("verify_nudge", True))):
            if want_authored_check:
                result["verify_hint"] = (
                    "the specialist was asked to author a check and end its "
                    "report with a `CHECK: <command>` line but didn't — this "
                    "report is unverified self-report. Re-delegate (the "
                    "contract is mandatory), or verify the acceptance "
                    "criteria yourself before trusting the result.")
            else:
                result["verify_hint"] = (
                    "this task smells testable but went out WITHOUT a "
                    "ground-truth check — if it has a pass/fail command, "
                    "re-delegate with verify='<command>' (or pin "
                    "tools.code.delegate.verify). A coding loop gated on real "
                    "tests is the difference between 'looks done' and 'is done'.")
        if routed:
            result["routed"] = (f"picked by preset strengths — the "
                                f"{wanted}-strong specialist, not the "
                                "default brain")
        if _base:
            # Observability for the worker-prompt A/B: the eval trace shows
            # which prompt the child actually ran on.
            result["worker_prompt"] = "lean worker prompt (agent.worker_prompt is on)"
        if swap_note:
            result["swap"] = swap_note
        if swap_back_note:
            result["swap_back"] = swap_back_note
        if wt:
            result["isolation"] = await _worktree_report(wt)
        if not model:
            result["note"] = ("no coder alias configured (tools.code.delegate.model) "
                              "and no coding-strong specialist live; ran on the "
                              "default brain. Serve a coder and tag its preset "
                              "'coding' (or set the alias) to get the offload benefit.")
        # Warn (never block) when the run landed on the brain AND the live
        # specialist isn't a coding model either — the slot is swappable, so a
        # delegate may have landed on e.g. the research preset. A strength-routed
        # pick IS the coding model — no warning. Resolution failure → no note.
        if not routed:
            try:
                from tools.model.catalog import live_slot as _live_slot
                slot = await _live_slot(ctx.config)
            except Exception:
                slot = None
            if slot and "coding" not in (slot.get("strengths") or []):
                _str = ", ".join(slot.get("strengths") or []) or "unknown"
                note = (f"note: the specialist is currently {slot['serving']} "
                        f"(strengths: {_str}) — review this output critically; it is "
                        "not the coding model.")
                result["note"] = f"{result['note']} {note}" if result.get("note") else note
        if child.get("error"):
            result["error"] = child["error"]
        ok = child.get("status") == "ok"
        return ToolResult(status="ok" if ok else "error", result=result,
                          tool_name=self.name,
                          error=None if ok else (child.get("error") or child.get("status")))
