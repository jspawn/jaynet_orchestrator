"""code.delegate — hand a coding task to a sub-agent running on a dedicated coder.

Why this exists: the orchestrator brain is a small-active-param MoE tuned for fast
tool-routing, not heavy code synthesis, and every file it reads stays in ITS
context for the rest of the run. Offloading the actual coding to a sub-agent that
runs on a dedicated dense coder model (served on the second GPU and registered as
a LiteLLM alias) wins twice: stronger code, and the bulky working transcript —
file reads, diffs, test logs — stays in the CHILD's context, never the parent's.

This is a thin, opinionated front door over agent.spawn: it picks the coder
model (configured alias → live specialist slot whose preset carries the
'coding' strength → a stopped tagged LOCAL preset swapped onto its slot →
default brain) and the coding tool-set by default, so the brain delegates
with one call instead of having to remember to pass model= and the right
tools= to agent.spawn. It inherits all of spawn's guarantees (child tools ⊆
parent tools, child's write/commit still prompts the human, spend counts
against the parent).

WHEN to use it: a self-contained, multi-step code change (implement X, refactor Y,
fix a failing test). NOT for a single edit you can do inline, and NOT a substitute
for the coding-projects plan→unit discipline on a large build — delegate one unit
at a time. Falls back to the default brain when no coder alias is configured and
no coding-strong specialist is live.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.git.status import _git

# Sensible default tool-set for a coding child: navigate, edit, verify, checkpoint.
_DEFAULT_CODING_TOOLS = [
    "fs.read", "fs.list", "fs.grep", "fs.write", "fs.edit",
    "code.run", "code.symbols", "code.tree", "code.patch", "code.deps",
    "lint.run", "test.run",
    "git.status", "git.diff", "git.log", "git.show", "git.add", "git.commit",
    "git.branch", "git.stash", "git.restore", "git.worktree",
    "skill.load", "skill.list", "note.set", "context.pin",
]

# A child without at least one of these can read and lint but never produce
# a file — live eval evidence: the brain once passed tools=["lint.run"], the
# child came back empty-handed and the parent wrote the code inline (the
# exact failure mode delegation exists to prevent). Such overrides are
# rejected below instead of spawning a helpless child.
_MUTATION_TOOLS = frozenset({"fs.write", "fs.edit", "code.patch", "code.run"})


def _cfg(ctx: ToolContext) -> dict:
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


class CodeDelegate(Tool):
    name = "code.delegate"
    description = (
        "Delegate a self-contained coding task to a sub-agent running on the "
        "dedicated coder model (keeps the heavy file/diff/test transcript out of "
        "your context and uses a stronger code model). Give a COMPLETE, standalone "
        "task — the child sees none of this conversation, so include the repo/path, "
        "what to change, and the done-check. Use for multi-step changes; do a "
        "one-line edit yourself. Returns only the child's final summary."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Complete, standalone coding instruction. Include the "
                               "project path, the change, and how to verify it (the "
                               "command/test that must pass).",
            },
            "tools": {
                "type": "array", "items": {"type": "string"},
                "description": "Override the coding tool-set given to the child "
                               "(default covers fs/code/git/lint/test). Can only "
                               "narrow your own tools, never exceed them.",
            },
            "model": {
                "type": "string",
                "description": "Override the coder model alias (default: the "
                               "configured coder → the live specialist whose preset "
                               "carries the coding strength → the default brain).",
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

        cfg = _cfg(ctx)
        model = args.get("model") or cfg.get("model")  # explicit wins
        routed = False
        swap_note = None
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
                res = await ModelUse().execute(
                    {"preset": plan["preset"], "swap": True}, ctx)
                ok = (res.status == "ok" and not (res.result or {}).get("hint")
                      and await route_strength_exact(ctx.config, wanted))
                if ok:
                    model, routed = plan["alias"], True
                    swap_note = (f"'{plan['preset']}' was swapped onto its "
                                 f"slot for this {wanted} task (the slot's "
                                 "previous model was stopped)")
                else:
                    swap_note = (f"could not swap in '{plan['preset']}' "
                                 f"({res.error or (res.result or {}).get('status')})"
                                 " — fell back to the allround route")
                    plan = {"mode": "allround",
                            "alias": await route_strength(ctx.config, wanted)}
            if model is None and plan.get("alias"):
                model, routed = plan["alias"], True
        tools = args.get("tools") or cfg.get("tools") or _DEFAULT_CODING_TOOLS
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

        child = await ctx.spawn(task, tools=tools, model=model,
                                name="coder", budget=budget, verify=verify,
                                work_root_path=(wt["path"] if wt else None))

        result = {
            "agent": "coder",
            "model": model or "(default brain)",
            "status": child.get("status"),
            "verified": child.get("verified"),          # True/False/None (no check)
            "verify_command": child.get("verify_command"),
            "files_changed": child.get("files_changed") or [],
            "answer": child.get("answer"),
            "sub_run_id": child.get("run_id"),
            "budget": child.get("budget"),
        }
        if routed:
            result["routed"] = (f"picked by preset strengths — the "
                                f"{wanted}-strong specialist, not the "
                                "default brain")
        if swap_note:
            result["swap"] = swap_note
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
