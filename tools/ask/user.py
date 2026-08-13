"""ask.user — pause the run to ask the human structured questions.

The model calls this when a request is ambiguous or needs a decision. Each
question is rendered in the web console as its own card with selectable options
and/or a free-text box; the run blocks until the user submits (or a timeout).
On the CLI / token path (no UI), the tool returns a note telling the model to
proceed with stated assumptions rather than hanging.
"""

from __future__ import annotations

from runtime.tool_base import Tool, ToolContext, ToolResult

_TYPES = {"single_select", "multi_select", "free_text"}


class AskUserTool(Tool):
    name = "ask.user"
    description = (
        "Ask the human one or more clarifying questions and wait for their "
        "answers before continuing. Use when the task is ambiguous, under-specified, "
        "or needs a decision only the user can make. Each question can offer options "
        "(user picks one or several) and/or accept free text. Prefer this over "
        "guessing. Returns the user's answers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "1-5 questions to ask the user.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string",
                               "description": "Short stable id you choose; echoed back with the answer."},
                        "text": {"type": "string", "description": "The question to show."},
                        "type": {"type": "string", "enum": sorted(_TYPES),
                                 "description": "single_select: pick one option; "
                                                "multi_select: pick any number; "
                                                "free_text: typed answer. Defaults to "
                                                "single_select when options are given, "
                                                "else free_text."},
                        "options": {"type": "array", "items": {"type": "string"},
                                    "description": "Choices for single_select / multi_select."},
                        "allow_text": {"type": "boolean",
                                       "description": "For selects, also allow a typed "
                                                      "'other' answer. Default true."},
                    },
                    "required": ["text"],
                },
            },
        },
        "required": ["questions"],
    }

    async def execute(self, args: dict, context: ToolContext) -> ToolResult:
        questions = args.get("questions") or []
        if not questions:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="No questions provided.")

        ask = getattr(context, "ask_user", None)
        if ask is None:
            return ToolResult(
                status="ok", tool_name=self.name,
                result=("Interactive questions aren't available in this run (no UI "
                        "attached). Proceed with the most reasonable assumptions and "
                        "state them explicitly to the user."))

        # Normalize: stable ids, valid types, default allow_text for selects.
        norm = []
        for i, q in enumerate(questions):
            qq = dict(q)
            qq["id"] = str(qq.get("id") or f"q{i + 1}")
            qq["text"] = str(qq.get("text") or "").strip()
            opts = qq.get("options") or []
            qq["options"] = [str(o) for o in opts]
            qt = qq.get("type") or ("single_select" if qq["options"] else "free_text")
            qq["type"] = qt if qt in _TYPES else "free_text"
            if qq["options"]:
                qq["allow_text"] = bool(qq.get("allow_text", True))
            norm.append(qq)
        norm = [q for q in norm if q["text"]][:5]
        if not norm:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="No valid questions (each needs 'text').")

        answers = await ask(norm)
        if not answers:
            return ToolResult(
                status="ok", tool_name=self.name,
                result=("The user didn't answer in time. Proceed with the most "
                        "reasonable assumptions and state them explicitly."))

        amap = answers.get("answers", answers) if isinstance(answers, dict) else {}
        lines, structured = [], {}
        for q in norm:
            a = amap.get(q["id"], {}) or {}
            if not isinstance(a, dict):
                a = {"value": a}
            val, txt = a.get("value"), (a.get("text") or "").strip()
            parts = []
            if isinstance(val, list) and val:
                parts.append(", ".join(str(v) for v in val))
            elif val not in (None, "", []):
                parts.append(str(val))
            if txt:
                parts.append(txt)
            lines.append(f"- {q['text']}\n  → {' / '.join(parts) if parts else '(no answer)'}")
            structured[q["id"]] = {"question": q["text"], "value": val, "text": txt or None}

        return ToolResult(
            status="ok", tool_name=self.name,
            result={"summary": "The user answered:\n" + "\n".join(lines),
                    "answers": structured})
