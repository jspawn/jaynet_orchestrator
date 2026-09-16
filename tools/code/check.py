"""code.check — verification-grade execution: prove code works, don't build it.

A policy-narrowed sibling of code.run, exposed to the BRAIN in place of the
write/run coding tools when tools.code.brain_mode is "verify" and a coding
specialist is present (see _brain_code_gate in runtime/loop.py). The brain
uses it to CHECK specialist output — run the tests, the linter, the build —
while implementation itself routes through code.delegate.

Policy deltas vs code.run (enforced here, not requested):
- network is always off
- timeout_s capped at 120 (verification is fast; long builds are delegation)
- max_output_lines capped at 200

Everything else — sandbox backends (eval container / devbox / host firejail),
cwd confinement, output contract — is code.run's, unchanged. The narrowing
shapes model behavior; the sandbox stays the real security boundary.
"""

from __future__ import annotations

from runtime.tool_base import ToolContext, ToolResult
from tools.code.run import CodeRun

_CHECK_TIMEOUT_CAP = 120
_CHECK_LINES_CAP = 200


class CodeCheck(CodeRun):
    name = "code.check"
    description = (
        "Verify code that already exists: run the test suite (pytest "
        "path::test), a build/type/lint check (make, ruff, mypy, cargo "
        "check), or a short python assertion snippet — and report whether it "
        "passes. Read-only intent: do NOT implement or fix code with this "
        "tool; when a check fails, describe the failure and delegate the fix "
        "(code.delegate), then re-check. Compared to the full execution "
        "tool: network is off, timeout caps at 120s, output caps at 200 "
        "lines. Same sandbox and workspace confinement as code.run — the cwd "
        "IS the project root, so tests run exactly where fs.* shows files. "
        "A non-zero exit (failing test) is a normal ok result — read "
        "stdout/stderr."
    )
    private = True
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command (language=bash, via bash -c) or "
                               "Python source (language=python; print() "
                               "returns values).",
            },
            "language": {
                "type": "string", "enum": ["bash", "python"], "default": "bash",
                "description": "bash: shell command. python: sandboxed snippet.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (must be under the allowed "
                               "roots). Defaults to the run's work root.",
            },
            "timeout_s": {
                "type": "integer", "default": 120, "minimum": 1, "maximum": 120,
                "description": "Hard wall-clock timeout (capped at 120s).",
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        forced = dict(args)
        forced["network"] = False
        forced["timeout_s"] = min(int(args.get("timeout_s") or 120),
                                  _CHECK_TIMEOUT_CAP)
        forced["max_output_lines"] = min(int(args.get("max_output_lines")
                                             or 200), _CHECK_LINES_CAP)
        return await super().execute(forced, ctx)
