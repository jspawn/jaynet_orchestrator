"""Deterministic, evidence-preserving compaction for agent transcripts.

Only old tool results are changed. Message count and indices stay stable so
privacy taint and salience pins remain valid; the trace retains the full result.
"""
from __future__ import annotations

import json
import os
from typing import Any

_LOG_TOOLS = frozenset({
    "code.run", "code.check", "test.run", "lint.run", "ops.run", "job.logs",
})
_WRITE_TOOLS = frozenset({"fs.write", "fs.edit"})
_HEAD_CHARS = 500
_TAIL_CHARS = 1000
_RECOVERY_NOTE = ("full result elided to save context; retrieve with trace.query "
                  "view=events run_id=<this run> if needed")


def _call_args(messages: list[dict]) -> dict[str, dict]:
    """Map tool-call IDs to parsed arguments; malformed calls simply opt out."""
    calls = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            function = call.get("function") or {}
            raw = function.get("arguments") if isinstance(function, dict) else None
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if call_id and isinstance(args, dict):
                calls[str(call_id)] = args
    return calls


def _json_prefix(content: str) -> tuple[Any, str] | None:
    """Decode a JSON result without losing any guard hint appended after it."""
    try:
        value, end = json.JSONDecoder().raw_decode(content)
    except (TypeError, ValueError):
        return None
    return value, content[end:]


def _ok(content: str) -> bool:
    decoded = _json_prefix(content)
    if decoded and isinstance(decoded[0], dict):
        return decoded[0].get("status") != "error"
    return not content.lstrip().startswith('{"status": "error"')


def _path(args: dict) -> str:
    raw = args.get("path")
    return os.path.normpath(str(raw)) if isinstance(raw, str) and raw else ""


def _read_key(args: dict) -> tuple | None:
    path = _path(args)
    if not path:
        return None
    # Different slices and byte caps are complementary evidence, not duplicates.
    return (path, args.get("start_line", 1), args.get("end_line"),
            args.get("max_bytes", 100000))


def _replace(message: dict, payload: dict, suffix: str = "") -> None:
    message["content"] = json.dumps(payload, ensure_ascii=False) + suffix


def _dedupe_reads(messages: list[dict], protect: set[int], calls: dict,
                  stats: dict) -> None:
    reads, writes = [], []
    for i, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if '"__compacted__"' in content:
            continue
        name = message.get("name")
        args = calls.get(str(message.get("tool_call_id")), {})
        if name == "fs.read" and _ok(content):
            key = _read_key(args)
            if key is not None:
                reads.append((i, key))
        elif name in _WRITE_TOOLS and _ok(content) and _path(args):
            writes.append((i, _path(args)))

    for i, key in reads:
        if i in protect:
            continue
        path = key[0]
        newer_read = any(j > i and other == key for j, other in reads)
        modified = any(j > i and other == path for j, other in writes)
        if not newer_read and not modified:
            continue
        message = messages[i]
        before = len(message.get("content") or "")
        reason = "newer read of the same file slice" if newer_read else "later file write"
        _replace(message, {
            "status": "ok", "__compacted__": True, "strategy": "stale_read",
            "path": path, "reason": reason,
            "note": f"stale file content removed ({reason}); re-read the file if needed",
        })
        stats["compacted"] += 1
        stats["deduplicated"] += 1
        stats["saved_chars"] += before - len(message["content"])


def _head_tail(text: str) -> str:
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
    return (text[:_HEAD_CHARS] + f"\n… [{omitted} chars pruned] …\n"
            + text[-_TAIL_CHARS:])


def _prune_code_run(message: dict, args: dict, decoded: tuple[Any, str]) -> bool:
    payload, suffix = decoded
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        return False
    result = dict(payload["result"])
    if not any(isinstance(result.get(stream), str) and result[stream]
               for stream in ("stdout", "stderr")):
        return False
    for stream in ("stdout", "stderr"):
        text = result.get(stream)
        if isinstance(text, str) and len(text) > _HEAD_CHARS + _TAIL_CHARS:
            result[stream] = _head_tail(text)
            result[f"{stream}_compacted"] = True
    if args.get("command"):
        result.setdefault("command", args["command"])
    compacted = dict(payload)
    compacted["result"] = result
    compacted["__compacted__"] = True
    compacted["strategy"] = "log_evidence"
    compacted["note"] = _RECOVERY_NOTE
    _replace(message, compacted, suffix)
    return True


def _prune_log(message: dict, args: dict) -> None:
    content = message.get("content") or ""
    decoded = _json_prefix(content)
    if message.get("name") == "code.run" and decoded \
            and _prune_code_run(message, args, decoded):
        return
    status = "error" if '"status": "error"' in content[:60] else "ok"
    stub = {
        "status": status, "__compacted__": True, "strategy": "log_evidence",
        "tool": message.get("name"), "head_tail": _head_tail(content),
        "note": _RECOVERY_NOTE,
    }
    if args.get("command"):
        stub["command"] = args["command"]
    if decoded and isinstance(decoded[0], dict):
        result = decoded[0].get("result")
        if isinstance(result, dict) and "exit_code" in result:
            stub["exit_code"] = result["exit_code"]
    _replace(message, stub)


def compact_messages(messages: list[dict], cfg: dict,
                     pinned: set | None = None) -> dict:
    """Compact eligible tool results and return exact character telemetry.

    The latest ``keep_last`` results and pinned results stay verbatim. Identical
    old file slices are deduplicated even when small; other results must exceed
    ``max_result_chars``. Log compaction preserves both ends, including errors.
    """
    stats = {"compacted": 0, "deduplicated": 0, "log_pruned": 0,
             "saved_chars": 0, "estimated_saved_tokens": 0}
    if not cfg or not cfg.get("enabled"):
        return stats
    max_chars = int(cfg.get("max_result_chars", 2000))
    keep_last = int(cfg.get("keep_last", 3))
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protect = set(tool_idx[-keep_last:]) if keep_last else set()
    if pinned:
        protect |= {i for i in pinned if 0 <= i < len(messages)}
    calls = _call_args(messages)

    if cfg.get("dedupe_reads", True):
        _dedupe_reads(messages, protect, calls, stats)

    for i in tool_idx:
        if i in protect:
            continue
        message = messages[i]
        content = message.get("content") or ""
        if len(content) <= max_chars or '"__compacted__"' in content:
            continue
        before = len(content)
        if message.get("name") in _LOG_TOOLS:
            _prune_log(message, calls.get(str(message.get("tool_call_id")), {}))
            stats["log_pruned"] += 1
        else:
            _replace(message, {
                "status": "error" if '"status": "error"' in content[:60] else "ok",
                "__compacted__": True,
                "head": content[:300].replace("\n", " "), "note": _RECOVERY_NOTE,
            })
        stats["compacted"] += 1
        stats["saved_chars"] += before - len(message["content"])

    # Image hygiene: keep only the newest image-bearing message intact.
    img_idx = [i for i, m in enumerate(messages)
               if isinstance(m.get("content"), list)
               and any(isinstance(b, dict) and b.get("type") == "image_url"
                       for b in m["content"])]
    for i in img_idx[:-1]:
        message = messages[i]
        before = len(json.dumps(message["content"], ensure_ascii=False))
        message["content"] = [
            {"type": "text",
             "text": "[image elided to save context — re-capture if needed]"}
            if isinstance(b, dict) and b.get("type") == "image_url" else b
            for b in message["content"]]
        after = len(json.dumps(message["content"], ensure_ascii=False))
        stats["compacted"] += 1
        stats["saved_chars"] += before - after
    stats["estimated_saved_tokens"] = stats["saved_chars"] // 4
    return stats
