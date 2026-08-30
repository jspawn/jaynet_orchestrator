"""code.execute — legacy alias of code.run (kept for older prompts and skills).

The two code tools were merged: code.run is the one execution verb, with a
`language` param (bash default, python for snippets). This alias keeps the
old name working — identical behavior, same backends (eval case container /
devbox / host firejail), same ORCH_EXEC_OUT/WORK channels — with two
compatibility shims:

- the old `code` argument is accepted and mapped to `command`;
- `language` defaults to python here (the old code.execute default).

All the logic lives in tools/code/run.py. New prompts and skills should say
code.run; this name exists so saved chats, imported eval cases and older
skills don't break.
"""

from __future__ import annotations

from tools.code.run import CodeRun


class CodeExecute(CodeRun):
    name = "code.execute"
    description = (
        "Legacy alias of code.run with language=python as the default "
        "(kept for older prompts and skills — identical sandbox, identical "
        "behavior; prefer code.run). Execute a short Python snippet and "
        "return stdout: math, JSON manipulation, regex tests, quick "
        "computations, small plots. numpy/matplotlib available (Agg "
        "backend): save files to the ORCH_EXEC_OUT dir "
        "(os.environ['ORCH_EXEC_OUT']) — they come back as written_files, "
        "hand them to the user with deliver.files. Relative file ops land "
        "in the persistent ORCH_EXEC_WORK workspace and SURVIVE across "
        "calls within the run. When ORCH_SUBCALL_SOCK is set, "
        "llm_query(prompt, ...) and llm_query_batched(prompts, ...) are "
        "pre-defined: mediated sub-LLM calls billed to this run — map LLM "
        "work over SLICES of a large file instead of reading it whole. "
        "Never import subprocess/os.system to dodge the sandbox; for "
        "shell commands pass language=bash or use code.run."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source (bash when language=bash). Use "
                               "print() to return values; save files into "
                               "ORCH_EXEC_OUT to keep them.",
            },
            "language": {
                "type": "string", "enum": ["python", "bash"], "default": "python",
                "description": "python: sandboxed snippet (default). bash: run "
                               "the source as a shell command instead.",
            },
            "timeout_s": {
                "type": "integer", "default": 30, "minimum": 1, "maximum": 600,
            },
        },
        "required": ["code"],
    }

    async def execute(self, args, ctx):
        args = dict(args)
        if "command" not in args and "code" in args:
            args["command"] = args.pop("code")
        args.setdefault("language", "python")
        return await super().execute(args, ctx)

    def needs_confirmation(self, args, ctx):
        # The alias's raw args carry `code`, not `language` — normalize
        # BEFORE gating so the python branch (tools.code.sandbox) decides,
        # not the bash one. Pre-merge code.execute gated a disabled python
        # sandbox; the merge must not silently ungate bare host python
        # (audit #11 D2 — "bare execution is never silent").
        args = dict(args)
        args.setdefault("language", "python")
        return super().needs_confirmation(args, ctx)
