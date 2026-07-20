"""Conversation compaction — the /compact command.

Distinct from runtime.loop._compact_messages, which stubs large tool results
*inside one agent run*: this operates on the whole chat history. The web client
owns that history and sends it with every /api/chat request, so /compact works
by contract with the client: the server summarizes the older exchanges with the
local brain (one tool-free call, outside the agent loop) and returns the brief
plus a kept-tail count in the run_finish `compact` payload; the client swaps
its turn list for [summary turn, ...kept tail turns] and the next request's
history starts from the summary.
"""

from __future__ import annotations

# Exchanges kept verbatim at the tail — the summary only covers what precedes
# them, so the model keeps the immediate conversational flow.
KEEP_LAST_EXCHANGES = 2

# Cap on the transcript shipped to the brain; oldest content is dropped first
# (least load-bearing, and a pathological history must not fill the brain's
# context before it can even summarize).
MAX_TRANSCRIPT_CHARS = 60_000

SUMMARY_SYSTEM = """You are the compaction module of a local-first agent orchestrator. Condense the conversation transcript below into a continuity brief for the assistant that will continue this chat — it will see your brief INSTEAD of the transcript.

Write in first person, present tense, as working notes to yourself. Cover, in order:
- The user's current goal or task, plus every explicit constraint, preference, or standing instruction still in force.
- Decisions already made, with the one-line reason for each.
- Concrete work done: files created or edited (exact paths), commands run, results that mattered (tests passed, errors hit, IDs issued).
- Facts, names, values, and numbers established along the way that later turns rely on.
- What is still open, and the immediate next step.

Be dense and specific: keep exact paths, values, and error messages that matter; drop greetings, pleasantries, and dead ends that taught nothing. Roughly 300-500 words unless the transcript is very long."""


def slice_history(history: list[dict],
                  keep_exchanges: int = KEEP_LAST_EXCHANGES
                  ) -> tuple[list[dict], list[dict]]:
    """Split flat chat history into (older, kept): `kept` is the last
    keep_exchanges*2 messages, held back verbatim; `older` is what a summary
    must cover. older == [] means there is nothing worth summarizing yet
    (history fits inside the keep window plus one exchange)."""
    msgs = [m for m in (history or [])
            if m.get("role") in ("user", "assistant") and m.get("content")]
    keep_n = max(0, keep_exchanges) * 2
    if len(msgs) <= keep_n + 2:
        return [], msgs
    if keep_n == 0:
        return msgs, []
    return msgs[:-keep_n], msgs[-keep_n:]


def _render_transcript(older: list[dict]) -> str:
    lines = []
    for m in older:
        role = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{role}: {m['content']}")
    text = "\n\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = ("[…oldest exchanges dropped to fit the context…]\n\n"
                + text[-MAX_TRANSCRIPT_CHARS:])
    return text


def build_summary_messages(older: list[dict], instruction: str = "") -> list[dict]:
    """The one-shot prompt for the brain: transcript + optional focus request."""
    user = "Transcript to compact:\n\n" + _render_transcript(older)
    if instruction:
        user += (f"\n\nFocus request from the user: {instruction}\n"
                 "Weight the brief toward it, without dropping critical state.")
    return [{"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": user}]


def nothing_note(n_messages: int) -> str:
    return (f"Nothing to compact — this chat has {n_messages} message"
            f"{'s' if n_messages != 1 else ''} and the last "
            f"{KEEP_LAST_EXCHANGES} exchanges stay verbatim anyway. "
            "Compact once the conversation has grown further.")


def result_footer(dropped: int, kept: int, usage: dict | None = None) -> str:
    """Display-only line appended under the summary bubble (not part of the
    summary that re-seeds the history)."""
    tok = (usage or {}).get("total_tokens")
    tok_s = f" · {tok:,} tokens" if tok else ""
    n = kept // 2
    return (f"\n\n---\n*🗜️ compacted {dropped} messages into this summary · "
            f"last {n} exchange{'s' if n != 1 else ''} kept verbatim{tok_s}*")
