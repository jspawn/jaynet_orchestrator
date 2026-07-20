"""docs.summarize — summarize a tree of document folders, one folder at a time.

Built for the failure mode where the orchestrator reads every .md file across a
whole course/module tree into ONE context and blows the window. This walks the
tree and processes each folder in its OWN isolated sub-agent, so the raw files
never accumulate in the caller's context:

  1. SURVEY  list the tree (fs.list only) → the course folders and their module
             subfolders that contain .md files. No file contents read.
  2. MODULES for each module folder, an isolated sub-agent reads that folder's
             .md files and writes summary.md there. One folder's files stay in
             one sub-agent.
  3. COURSES for each course folder, a sub-agent reads only the MODULE summaries
             (small) and writes a course-level summary.md (+ optional marketing
             statement). It never re-reads the raw files.

The caller only ever holds the folder list and per-folder status — never the
documents — so the tree can be arbitrarily large.
"""

from __future__ import annotations

import json
import re

from runtime.tool_base import Tool, ToolContext, ToolResult

_DEFAULT_INSTR = ("general information about it, what you learn, and the benefits")
_MODULE_CAP = 200


def _json(text: str):
    """Pull the first JSON object/array out of a model answer (tolerates fences/prose)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    m = re.search(r"[\{\[].*[\}\]]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


class DocsSummarize(Tool):
    name = "docs.summarize"
    description = (
        "Summarize a tree of document folders (e.g. courses → modules → .md files) "
        "WITHOUT blowing the context window. It surveys the tree, then processes "
        "each folder in its own isolated sub-agent: writes a summary.md in every "
        "module folder (from that folder's .md files) and a course-level summary.md "
        "in every course folder (from the module summaries), optionally with a "
        "marketing statement. Use this for 'summarize every module/course' over many "
        "files — never read a whole tree of documents into your own context. Params: "
        "`root` (base folder), `instructions` (what each summary should contain), "
        "`summary_name`, `marketing` (course-level marketing statement, default on)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "root": {"type": "string",
                     "description": "Base folder containing the course folders (default '.')."},
            "instructions": {"type": "string",
                             "description": "What each summary should cover (default: general "
                                            "info, what you learn, and the benefits)."},
            "summary_name": {"type": "string",
                             "description": "Filename to write in each folder (default 'summary.md')."},
            "marketing": {"type": "boolean",
                          "description": "Also write a short marketing statement at course level (default true)."},
        },
        "required": [],
    }
    private = True
    read_only = True

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if getattr(ctx, "spawn", None) is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="docs.summarize needs sub-agent spawning, unavailable here")
        root = (args.get("root") or ".").strip()
        instr = (args.get("instructions") or _DEFAULT_INSTR).strip()
        summary_name = (args.get("summary_name") or "summary.md").strip()
        marketing = args.get("marketing", True)

        async def progress(label):
            # ctx.emit is the 2-arg tool seam (etype, data); emit a generic
            # 'progress' event the UI renders live under the running tool call.
            try:
                if getattr(ctx, "emit", None):
                    await ctx.emit("progress", {"label": label})
            except Exception:
                pass

        # ---- 1. SURVEY (structure only, no file contents) ----
        await progress("Surveying folder tree …")
        survey = await ctx.spawn(
            f"Survey the folder tree under `{root}` for summarization. Identify every "
            "COURSE folder (a folder under the root that has subfolders) and, within "
            "each, every MODULE folder (a subfolder that directly contains .md files). "
            "Use fs.list only — do NOT read any file contents.\n"
            "Output ONLY JSON, no prose and no code fences:\n"
            '{"courses":[{"name":"...","path":"...","modules":['
            '{"name":"...","path":"...","md":<number of .md files>}]}]}',
            tools=["fs.list"], name="survey")
        data = _json(survey.get("answer", "")) or {}
        courses = data.get("courses") or []
        if not courses:
            return ToolResult(status="error", result={"survey": survey.get("answer")},
                              tool_name=self.name,
                              error=f"could not map any course/module folders under '{root}'")

        modules = [(c, m) for c in courses for m in (c.get("modules") or []) if m.get("path")]
        if len(modules) > _MODULE_CAP:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"{len(modules)} module folders exceeds the safety cap "
                                    f"of {_MODULE_CAP}; narrow the root")

        results = {"modules": [], "courses": [], "failed": []}

        # ---- 2. MODULES (each folder isolated) ----
        total_m = len(modules)
        for idx, (_course, mod) in enumerate(modules, 1):
            path = mod["path"]
            await progress(f"Summarizing module {idx}/{total_m} · {path}")
            r = await ctx.spawn(
                f"Summarize ONE folder of course material. Read every .md file in the "
                f"folder `{path}` (fs.list that folder, then fs.read each .md — read "
                f"ONLY this folder's files). Write `{path}/{summary_name}` containing: "
                f"{instr}. Base it strictly on those files; do not invent. Structure it "
                "clearly (headings). Report only the file written and one line — do NOT "
                "paste the summary back.",
                tools=["fs.list", "fs.read", "fs.write"], name="module")
            ok = r.get("status") in ("ok", None)
            (results["modules"] if ok else results["failed"]).append(path)

        # ---- 3. COURSES (from the module summaries, not the raw files) ----
        total_c = len(courses)
        for ci, course in enumerate(courses, 1):
            cpath = course.get("path")
            if not cpath:
                continue
            mod_paths = [f"{m['path']}/{summary_name}" for m in (course.get("modules") or []) if m.get("path")]
            if not mod_paths:
                continue
            await progress(f"Course summary {ci}/{total_c} · {cpath}")
            mk = (" Then, at the end of the file, add a short MARKETING STATEMENT "
                  "(2-4 sentences): why this course matters for future students."
                  if marketing else "")
            r = await ctx.spawn(
                f"Write a course-level summary. Read the module summaries of course "
                f"`{cpath}` (fs.read each):\n" + "\n".join(mod_paths) + "\n\n"
                f"Write `{cpath}/{summary_name}` with a COURSE-LEVEL overview: {instr}, "
                f"synthesizing across the modules (not a list of them).{mk} Base it on "
                "the module summaries; keep it concise and well-structured. Report only "
                "the file written and one line.",
                tools=["fs.list", "fs.read", "fs.write"], name="course")
            ok = r.get("status") in ("ok", None)
            (results["courses"] if ok else results["failed"]).append(cpath)

        n_ok = len(results["modules"]) + len(results["courses"])
        report = (f"Summarized {len(results['modules'])} module folder(s) and "
                  f"{len(results['courses'])} course(s); wrote {summary_name} in each."
                  + (f" {len(results['failed'])} folder(s) failed: "
                     + ", ".join(results['failed']) if results["failed"] else ""))
        return ToolResult(
            status="ok" if n_ok and not results["failed"] else ("error" if not n_ok else "ok"),
            tool_name=self.name,
            result={"report": report, **results},
            error=None if n_ok else "no folders were summarized")
