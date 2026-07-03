"""code.delegate — hand a coding task to a sub-agent running on a dedicated coder.

Why this exists: the orchestrator brain is a small-active-param MoE tuned for fast
tool-routing, not heavy code synthesis, and every file it reads stays in ITS
context for the rest of the run. Offloading the actual coding to a sub-agent that
runs on a dedicated dense coder model (served on the second GPU and registered as
a LiteLLM alias) wins twice: stronger code, and the bulky working transcript —
file reads, diffs, test logs — stays in the CHILD's context, never the parent's.

This is a thin, opinionated front door over agent.spawn: it picks the configured
coder model and the coding tool-set by default, so the brain delegates with one
call instead of having to remember to pass model= and the right tools= to
agent.spawn. It inherits all of spawn's guarantees (child tools ⊆ parent tools,
child's write/commit still prompts the human, spend counts against the parent).

WHEN to use it: a self-contained, multi-step code change (implement X, refactor Y,
fix a failing test). NOT for a single edit you can do inline, and NOT a substitute
for the coding-projects plan→unit discipline on a large build — delegate one unit
at a time. Falls back to the default brain if no coder alias is configured.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

# Sensible default tool-set for a coding child: navigate, edit, verify, checkpoint.
_DEFAULT_CODING_TOOLS = [
    "fs.read", "fs.list", "fs.grep", "fs.write", "fs.edit",
    "code.run", "code.symbols", "code.tree", "code.patch", "code.deps",
    "lint.run", "test.run",
    "git.status", "git.diff", "git.log", "git.show", "git.add", "git.commit",
    "git.branch", "git.stash", "git.restore", "git.worktree",
    "skill.load", "skill.list", "note.set", "context.pin",
]


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}).get("code", {}) or {}).get("delegate", {}) or {}


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
                               "configured coder, else the default brain).",
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
        model = args.get("model") or cfg.get("model")  # None -> default brain
        tools = args.get("tools") or cfg.get("tools") or _DEFAULT_CODING_TOOLS
        budget = args.get("budget") or cfg.get("budget")
        verify = args.get("verify") or cfg.get("verify")   # gate on tests when given

        child = await ctx.spawn(task, tools=tools, model=model,
                                name="coder", budget=budget, verify=verify)

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
        if not model:
            result["note"] = ("no coder alias configured (tools.code.delegate.model); "
                              "ran on the default brain. Serve a coder on GPU 1 and set "
                              "the alias to get the offload benefit.")
        if child.get("error"):
            result["error"] = child["error"]
        ok = child.get("status") == "ok"
        return ToolResult(status="ok" if ok else "error", result=result,
                          tool_name=self.name,
                          error=None if ok else (child.get("error") or child.get("status")))
