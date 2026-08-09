"""Harness todo list — the per-run structured plan behind the ToDos panel.

The agent creates and maintains the list through the `todos` tool; the loop
owns the state (one TodoList per run), emits a full-snapshot `todos` event on
every change (idempotent and reconnect-safe — no diff protocol at this size),
and re-injects a compact rendering each turn so the list survives compaction
of the transcript it was born in.

Status model: pending → working → done | failed | skipped. At most one item is
`working` at a time (setting one clears the others). `failed` is terminal for
the ITEM, not the run — the agent retries, re-plans, or marks it `skipped`
with a reason in `info`.
"""

from __future__ import annotations

MAX_ITEMS = 20
MAX_TITLE = 140
MAX_DESC = 500
MAX_NOTES = 8
STATUSES = ("pending", "working", "done", "failed", "skipped")
_MARK = {"pending": "○", "working": "◐", "done": "✓", "failed": "✗", "skipped": "↷"}


def _err(msg: str) -> dict:
    return {"status": "error", "error": msg}


class TodoList:
    def __init__(self) -> None:
        self.items: list[dict] = []

    # ---- actions ----------------------------------------------------------
    def apply(self, payload: dict) -> dict:
        """Apply one todos-tool call. Returns {"status": "ok", "items": [...]}
        or {"status": "error", "error": ...}. Never raises on bad input."""
        action = str((payload or {}).get("action") or "").strip().lower()
        handler = {"set": self._set, "update": self._update, "add": self._add,
                   "remove": self._remove, "clear": self._clear}.get(action)
        if handler is None:
            return _err(f"unknown action {action!r} — use set, update, add, "
                        "remove or clear")
        res = handler(payload)
        if res is None:                      # success path: return the snapshot
            return {"status": "ok", "items": self.snapshot()}
        return res                           # an _err() dict

    def _mk_item(self, i: int, raw: dict) -> dict | None:
        title = str((raw or {}).get("title") or "").strip()[:MAX_TITLE]
        if not title:
            return None
        return {"id": i, "title": title,
                "desc": str((raw or {}).get("desc") or "").strip()[:MAX_DESC],
                "status": "pending", "info": []}

    def _set(self, payload: dict) -> dict | None:
        raws = payload.get("items")
        if not isinstance(raws, list) or not raws:
            return _err("set needs a non-empty items list: "
                        '[{"title": "…", "desc": "…"}, …]')
        items = []
        for raw in raws[:MAX_ITEMS]:
            if isinstance(raw, str):          # tolerate ["do x", "do y"]
                raw = {"title": raw}
            it = self._mk_item(len(items) + 1, raw if isinstance(raw, dict) else {})
            if it:
                items.append(it)
        if not items:
            return _err("no usable items — every item needs a title")
        self.items = items
        if len(raws) > MAX_ITEMS:             # tell the model, don't silently drop
            return {"status": "ok", "items": self.snapshot(),
                    "note": f"plan capped: kept the first {MAX_ITEMS} of "
                            f"{len(raws)} items — use add/remove as items complete"}
        return None

    def _find(self, payload: dict) -> dict | None:
        try:
            want = int(payload.get("id"))
        except (TypeError, ValueError):
            return None
        return next((it for it in self.items if it["id"] == want), None)

    def _update(self, payload: dict) -> dict | None:
        it = self._find(payload)
        if it is None:
            return _err("update needs the id of an existing item "
                        f"(have ids {[i['id'] for i in self.items]})")
        status = payload.get("status")
        if status is not None:
            status = str(status).strip().lower()
            if status not in STATUSES:
                return _err(f"unknown status {status!r} — use "
                            + ", ".join(STATUSES))
            if status == "working":           # at most one
                for other in self.items:
                    if other["status"] == "working":
                        other["status"] = "pending"
            it["status"] = status
        note = str(payload.get("note") or "").strip()
        if note:
            it["info"].append(note[:MAX_DESC])
            del it["info"][:-MAX_NOTES]
        desc = payload.get("desc")
        if desc is not None:
            it["desc"] = str(desc).strip()[:MAX_DESC]
        if status is None and not note and desc is None:
            return _err("update changes nothing — pass status, note and/or desc")
        return None

    def _add(self, payload: dict) -> dict | None:
        if len(self.items) >= MAX_ITEMS:
            return _err(f"list is full ({MAX_ITEMS} items) — remove or finish "
                        "items before adding more")
        it = self._mk_item(len(self.items) + 1, payload)
        if it is None:
            return _err("add needs a title")
        self.items.append(it)
        return None

    def _remove(self, payload: dict) -> dict | None:
        it = self._find(payload)
        if it is None:
            return _err("remove needs the id of an existing item")
        self.items = [x for x in self.items if x["id"] != it["id"]]
        for n, x in enumerate(self.items, 1):   # re-number: ids stay 1..n
            x["id"] = n
        return None

    def _clear(self, payload: dict) -> dict | None:
        self.items = []
        return None

    def replace(self, raws: list) -> None:
        """Wholesale replace from a trusted-but-unverified snapshot (the
        child-todo sync). Enforces the same caps and status vocabulary as the
        tool path but PRESERVES status/info — the model-facing _set() forces
        pending, which would erase a synced list's progress."""
        items = []
        for raw in (raws if isinstance(raws, list) else [])[:MAX_ITEMS]:
            if not isinstance(raw, dict):
                continue
            it = self._mk_item(len(items) + 1, raw)
            if it is None:
                continue
            status = str(raw.get("status") or "pending").strip().lower()
            it["status"] = status if status in STATUSES else "pending"
            it["info"] = [str(x)[:MAX_DESC]
                          for x in (raw.get("info") or [])][:MAX_NOTES]
            items.append(it)
        seen_working = False                 # invariant: at most one working
        for it in items:
            if it["status"] == "working":
                if seen_working:
                    it["status"] = "pending"
                seen_working = True
        self.items = items

    # ---- views ------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        return [dict(it, info=list(it["info"])) for it in self.items]

    def progress(self) -> tuple[int, int]:
        done = sum(1 for it in self.items if it["status"] in ("done", "skipped"))
        return done, len(self.items)

    def render(self) -> str:
        """Compact per-turn re-injection (rides the working anchor / its own
        trailing system message) so compaction can't take the list away."""
        if not self.items:
            return ""
        lines = []
        for it in self.items:
            line = f"{it['id']} [{it['status']}] {it['title']}"
            if it["status"] == "working" and it["desc"]:
                line += f" — {it['desc'][:200]}"
            lines.append(line)
        done, total = self.progress()
        return (f"TODO LIST ({done}/{total} done — keep it current with the "
                "todos tool: one item 'working', mark done/failed/skipped with "
                "a short note as you go):\n" + "\n".join(lines))
