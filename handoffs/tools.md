# Handoff: add a new tool

**Goal:** give the agent a new capability that skills/chains can't express —
real logic, state, parsing, an API wrapper.

## Pick the right kind first

| Kind | When | Where it lives |
|---|---|---|
| **Connector** (declarative YAML HTTP) | wrapping a REST API, no logic | Studio → Connectors; secrets only as env-var references from `~/.config/jaynet.env` |
| **MCP bridge** | the capability already exists as an MCP server | `tools/mcp` — configure, don't code |
| **Python tool** | parsing, sandboxing, state — anything declarative can't do | `tools/<namespace>/<verb>.py` (repo) or Studio → Tools (custom layer) |

If a connector or MCP covers it, stop there — Python tools run with
orchestrator privileges and are admin-trusted code by design.

## A Python tool

```python
# tools/<namespace>/<verb>.py
from runtime.tool_base import Tool, ToolResult, ToolContext

class MyTool(Tool):
    name = "<namespace>.<verb>"          # must match the file location
    description = (                       # the model reads THIS to decide
        "What it does, when to use it, what it returns. Write it for the "
        "model, not for humans."
    )
    parameters = { ... }                  # JSON Schema for the arguments

    def execute(self, args, ctx: ToolContext) -> ToolResult:
        ...
```

- The registry (`runtime/registry.py`) auto-discovers `tools/*/*.py` at
  startup — **a new tool needs a service restart**. The custom layer
  (`$JAYNET_DATA/custom/tools/`) is loaded by file path and refuses to
  overwrite a registered name.
- Read `runtime/tool_base.py` for the `Tool`/`ToolResult`/`ToolContext`
  contract, and a simple shipped tool (`tools/ask/user.py`) for the house
  style: module docstring explaining behavior, exact error strings, no
  silent catches.
- `ctx` carries the run's context — budget, spawn capability, confirmation
  routing. Sub-agent spawns must respect the parent's ceilings (an audit
  fixed children running unlimited; keep that invariant).

## The rules that matter

- **Privacy namespaces.** Tools under namespaces marked private in
  `config/runtime.yaml` (`privacy:`) *taint* the conversation — cloud calls
  get refused afterwards unless the user shares. If your tool touches
  private data, its namespace belongs on that list.
- **Confirmation gates.** Destructive or outward-facing actions go through
  the confirmation flow (`confirmation:` in runtime.yaml) — don't build
  side-effecting tools that bypass it.
- **Timeouts + budgets are enforced around you.** Hard per-tool timeouts,
  run-level cost/token/wall-clock ceilings. Keep tools non-interactive and
  return structured errors the model can act on.
- **No network in tests.** Tests use monkeypatching; add a test file in
  `tests/` for your tool (no cross-test imports — copy helpers).

## Verify

- `scripts/orch --list-tools` shows registration without any model server.
- Exercise it in a chat run; check Admin → Status → the run's trace for the
  exact call/result.
- `python -m pytest tests/ -q -k <namespace>` then the full suite.
