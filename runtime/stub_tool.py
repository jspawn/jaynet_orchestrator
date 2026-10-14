"""Recording stub tools — eval-harness stand-ins for external functions.

A stub presents a real function schema to the model but its execute() only
returns a canned result; the call itself (name + args) is already recorded
in trace.db by the loop's normal tool_result logging, so argument-level
grading (expect.tool_calls) needs no extra recording machinery. BFCL-style
eval cases declare these via the `stub_tools:` case key; the runner
registers them for the case's duration and unregisters afterwards.
"""

from __future__ import annotations

from typing import Any

from runtime.tool_base import Tool, ToolContext, ToolResult


class RecordingStubTool(Tool):
    """A schema-only tool: records its call (via the loop's trace) and
    returns a canned result. read_only because nothing observable changes.

    The default result ECHOES the args as a plausible success payload
    ({"status": "success", "result": <args>}): an explicit "no real function
    was executed" note invited the brain to retry the call with varied args
    (bfcl-live-multiple scored 5/15 largely on that artifact, 2026-10-14) —
    real tools return data, and the graded surface is the CALL, not the
    answer. Pass `result=` to override with a case-specific payload."""

    read_only = True

    def __init__(self, name: str, description: str,
                 parameters: dict[str, Any], result: Any = None):
        self.name = name
        self.description = description
        self.parameters = parameters or {
            "type": "object", "properties": {}, "required": []}
        self._canned = result

    async def execute(self, args: dict[str, Any],
                      context: ToolContext) -> ToolResult:
        canned = self._canned if self._canned is not None else {
            "status": "success", "result": args}
        return ToolResult(status="ok", result=canned, tool_name=self.name)
