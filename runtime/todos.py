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

Acceptance ledger (harness lessons §3): any item or requirement can carry a
machine CHECK — a shell command that proves the increment (`| check: <cmd>`
suffix, or the structured `check` field on items). The state container stays
synchronous: apply() only REPORTS due checks (`checks_due` — a done-transition
on a checked item, or an explicit `verify` action); the async caller
(dispatch_guards.todos_update) runs them through the verify sandbox and calls
record_check() with the outcome. `verified` is harness-set, never model-set.
The final-answer requirements gate reads open_must(): a [must] entry with a
declared check must be VERIFIED, not just done/dropped.

The list writes through to <work_root>/ledger.json on every change (the
notes.md pattern) so /goal iterations — fresh run, same workspace — can
rehydrate the plan; load_ledger only accepts a ledger whose goal criterion
matches the current run's, so an unrelated run never inherits a stale plan.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ITEMS = 20
MAX_TITLE = 140
MAX_DESC = 500
MAX_NOTES = 8
MAX_REQS = 10
MAX_REQ = 200
MAX_CHECK = 300
STATUSES = ("pending", "working", "done", "failed", "skipped")
# What models actually emit — map instead of rejecting (live: 'in_progress'
# is the most common wrong status by far).
_STATUS_ALIASES = {"in_progress": "working", "in-progress": "working",
                   "completed": "done", "complete": "done",
                   "cancelled": "skipped", "canceled": "skipped",
                   "todo": "pending", "open": "pending"}
_MARK = {"pending": "○", "working": "◐", "done": "✓", "failed": "✗", "skipped": "↷"}

LEDGER_FILENAME = "ledger.json"

# The check declaration: "<title or requirement> | check: <shell command>"
# (the /goal `| check:` convention, reused). Suffix form keeps the model-
# facing surface one string; the structured `check` field on items wins when
# both are present.
_CHECK_SUFFIX_RE = re.compile(r"\s*\|\s*check:\s*(\S.*)$", re.IGNORECASE)


def _split_check(text: str) -> tuple[str, str | None]:
    """Split a `| check: <cmd>` suffix off a title/requirement string."""
    m = _CHECK_SUFFIX_RE.search(text or "")
    if not m:
        return text, None
    return text[:m.start()].rstrip(), m.group(1).strip()[:MAX_CHECK]


def _err(msg: str) -> dict:
    return {"status": "error", "error": msg}


class TodoList:
    def __init__(self) -> None:
        self.items: list[dict] = []
        # The request's EXPLICIT requirements (format, spelling, delivery,
        # must-contain) — a flat list of "[must]/[should]/[nice] …" strings,
        # kept SEPARATE from the plan so a pure output requirement never
        # becomes an artificial todo. Replace semantics: the model re-sends
        # the whole list, dropping each requirement once verified against the
        # final answer. Open [must]s bounce the finish (loop requirements
        # gate); [should]/[nice] are informational.
        self.requirements: list[str] = []
        # Machine checks declared on requirements, keyed by requirement TEXT
        # (the list stays plain strings for every existing consumer):
        # {"<text>": {"check": <cmd>, "verified": True|False}}. Pruned to
        # live entries whenever the list is replaced. `verified` is written
        # only by record_check() — never from model input.
        self.req_checks: dict[str, dict] = {}

    # ---- actions ----------------------------------------------------------
    def apply(self, payload: dict) -> dict:
        """Apply one todos-tool call. Returns {"status": "ok", "items": [...],
        "requirements": [...]} or {"status": "error", ...}. Never raises.

        Ledger (harness lessons §3): the response may carry "checks_due" —
        declared checks to run sandboxed (an item that just transitioned to
        done, or an explicit `verify` action). This container stays sync;
        the async caller runs the checks and reports back via record_check().
        """
        payload = payload or {}
        before = {it["id"]: it["status"] for it in self.items}
        if "requirements" in payload:
            err = self._set_requirements(payload.get("requirements"))
            if err:
                return err
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            if "requirements" in payload:    # requirements-only update
                return {"status": "ok", "items": self.snapshot(),
                        "requirements": list(self.requirements)}
            return _err("unknown action '' — use set, update, add, "
                        "remove, clear or verify")
        due: list[dict] = []
        if action == "verify":
            due, err = self._verify_target(payload)
            if err:
                return err
            res = None
        else:
            handler = {"set": self._set, "update": self._update,
                       "add": self._add, "remove": self._remove,
                       "clear": self._clear}.get(action)
            if handler is None:
                return _err(f"unknown action {action!r} — use set, update, "
                            "add, remove, clear or verify")
            res = handler(payload)
            due += self._done_transition_checks(before)
        if res is None:                      # success path: return the snapshot
            out = {"status": "ok", "items": self.snapshot(),
                   "requirements": list(self.requirements)}
            if due:
                out["checks_due"] = due
            return out
        if res.get("status") == "ok":        # capped-plan note path
            res["requirements"] = list(self.requirements)
        return res                           # an _err() dict

    def _set_requirements(self, raw) -> dict | None:
        if not isinstance(raw, list):
            return _err("requirements must be a list of strings, e.g. "
                        '["[must] answer spells numbers in words"]')
        reqs = []
        for r in raw[:MAX_REQS]:
            s = str(r or "").strip()[:MAX_REQ]
            if not s:
                continue
            s, check = _split_check(s)
            if s:
                reqs.append(s)
                if check:
                    # Re-declaring a check keeps any previous verified flag
                    # for the SAME command; a changed command re-verifies.
                    old = self.req_checks.get(s)
                    self.req_checks[s] = {
                        "check": check,
                        **({"verified": old["verified"]}
                           if old and old.get("check") == check
                           and "verified" in old else {})}
        self.requirements = reqs
        live = set(reqs)
        self.req_checks = {t: c for t, c in self.req_checks.items()
                           if t in live}
        return None

    def _mk_item(self, i: int, raw: dict) -> dict | None:
        raw = raw or {}
        title = str(raw.get("title") or "").strip()[:MAX_TITLE]
        if not title:
            return None
        title, suffix_check = _split_check(title)
        check = (str(raw.get("check") or "").strip()[:MAX_CHECK]
                 or suffix_check)
        it = {"id": i, "title": title,
              "desc": str(raw.get("desc") or "").strip()[:MAX_DESC],
              "status": "pending", "info": []}
        if check:
            it["check"] = check
        return it

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
        raw = payload.get("id")
        if raw is None:
            return None
        try:
            want = int(raw)
        except (TypeError, ValueError):
            return None
        return next((it for it in self.items if it["id"] == want), None)

    def _find_by_title(self, payload: dict) -> dict | None:
        """Fallback for models that address items by title, not id — only an
        exact, UNAMBIGUOUS title match (else the caller's error path lists
        the id=title mapping so the next call can recover)."""
        want = str(payload.get("title") or "").strip().lower()
        if not want:
            return None
        hits = [it for it in self.items
                if it["title"].strip().lower() == want]
        return hits[0] if len(hits) == 1 else None

    def _update(self, payload: dict) -> dict | None:
        # Batch set-shape: small models re-send the WHOLE list under "update"
        # ({items: [{title, status…}, …]}) — merge each entry by id or exact
        # title instead of erroring on the missing top-level id (live:
        # code-bugfix looped on this for turns on end).
        entries = payload.get("items")
        if isinstance(entries, list) and entries \
                and all(isinstance(e, dict) for e in entries):
            errs = [m for e in entries
                    if (m := self._update_one(e)) is not None]
            if errs:
                return _err("; ".join(errs[:3])
                            + (" …" if len(errs) > 3 else ""))
            return None
        msg = self._update_one(payload)
        return _err(msg) if msg else None

    def _update_one(self, payload: dict) -> str | None:
        """Apply one update entry. Returns None on success, an error message
        (not a dict — the batch path joins several) on failure."""
        it = self._find(payload) or self._find_by_title(payload)
        if it is None:
            ref = payload.get("title") or payload.get("id") or "?"
            if not self.items:
                return ("no todos yet — create them first (action 'add' or a "
                        "full-set update with items), then update by id")
            return (f"{str(ref)[:40]}: needs the id or exact title of an "
                    "existing item — current: "
                    + ", ".join(f"{i['id']}={i['title'][:30]}"
                                for i in self.items))
        status = payload.get("status")
        if status is not None:
            status = str(status).strip().lower()
            status = _STATUS_ALIASES.get(status, status)
            if status not in STATUSES:
                return (f"unknown status {status!r} — use "
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
        check = payload.get("check")
        if check is not None:
            check = str(check).strip()[:MAX_CHECK]
            if check:
                it["check"] = check      # attach/replace; verified resets —
            else:                        # a new command must prove itself
                it.pop("check", None)    # explicit "" clears the declaration
            it.pop("verified", None)
        if status is None and not note and desc is None and check is None:
            return "update changes nothing — pass status, note, desc and/or check"
        return None

    def _add(self, payload: dict) -> dict | None:
        # Batch/unwrapped add: tolerate {items: [{…}, …]} under "add" too.
        entries = payload.get("items")
        if isinstance(entries, list) and entries \
                and all(isinstance(e, dict) for e in entries):
            for e in entries:
                err = self._add(e)
                if err:
                    return err
            return None
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

    # ---- ledger: declared checks -------------------------------------------
    def _done_transition_checks(self, before: dict) -> list[dict]:
        """Checks due because an item just transitioned TO done with a
        declared, not-yet-verified check (harness lessons §3: 'marking a
        todo done triggers its check'). A failed item re-verifies by going
        working → done again, or via the explicit verify action."""
        return [{"kind": "item", "id": it["id"], "title": it["title"],
                 "check": it["check"]}
                for it in self.items
                if it.get("status") == "done" and it.get("check")
                and it.get("verified") is not True
                and before.get(it["id"]) != "done"]

    def _find_requirement(self, want: str) -> str | None:
        """Exact match, else a UNIQUE substring match (same recoverability
        rule as _find_by_title)."""
        if want in self.requirements:
            return want
        hits = [r for r in self.requirements
                if want.lower() in r.lower()]
        return hits[0] if len(hits) == 1 else None

    def _verify_target(self, payload: dict) -> tuple[list[dict], dict | None]:
        """The explicit `verify` action: re-run an entry's declared check on
        demand (fix → re-verify without a status flip-flop). Returns
        (checks_due, error-dict|None)."""
        req = str(payload.get("requirement") or "").strip()
        if req:
            text = self._find_requirement(req)
            if text is None:
                return [], _err(
                    f"no requirement matches {req[:60]!r} — current: "
                    + "; ".join(r[:40] for r in self.requirements))
            rc = self.req_checks.get(text)
            if not rc or not rc.get("check"):
                return [], _err(f"requirement {text[:60]!r} has no check — "
                                "declare one with '| check: <cmd>'")
            return [{"kind": "requirement", "text": text,
                     "check": rc["check"]}], None
        it = self._find(payload) or self._find_by_title(payload)
        if it is None:
            return [], _err("verify needs the id or exact title of an item, "
                            "or requirement='<text>'")
        if not it.get("check"):
            return [], _err(f"item {it['id']} has no check — attach one with "
                            "update + check='<cmd>'")
        return [{"kind": "item", "id": it["id"], "title": it["title"],
                 "check": it["check"]}], None

    def record_check(self, kind: str, key, *, verified: bool,
                     note: str = "") -> None:
        """Write a check OUTCOME back onto its entry (async caller's report —
        see apply()). `verified` is harness-set only: the model can declare
        checks, never mark them passed."""
        if kind == "item":
            it = next((x for x in self.items if x["id"] == key), None)
            if it is None:
                return
            it["verified"] = bool(verified)
            if note:
                it["info"].append(note[:MAX_DESC])
                del it["info"][:-MAX_NOTES]
        else:
            rc = self.req_checks.get(str(key))
            if rc is not None:
                rc["verified"] = bool(verified)

    def open_must(self, checks_enabled: bool = True) -> list[str]:
        """The [must] entries still blocking the finish — the requirements
        gate and the run result's open_must field share this. Pending /
        working [must] items and present [must] requirements block as
        before; NEW (ledger): a done [must] item with a declared check that
        was never verified (or failed) blocks too — 'done' is the model's
        say-so, the check is the proof."""
        open_ = [t["title"] for t in self.items
                 if t.get("status") in ("pending", "working")
                 and str(t.get("title") or "").lower().startswith("[must]")]
        open_ += [r for r in self.requirements
                  if r.lower().startswith("[must]")]
        if checks_enabled:
            open_ += [t["title"] + " (check not verified)"
                      for t in self.items
                      if t.get("status") == "done" and t.get("check")
                      and t.get("verified") is not True
                      and str(t.get("title") or "")
                      .lower().startswith("[must]")]
        return open_

    def ensure_requirement(self, text: str) -> None:
        """Idempotent seed (the /goal DONE WHEN entry): append unless
        already present — unlike a wholesale replace, this preserves a
        hydrated ledger's other requirements."""
        if text not in self.requirements and len(self.requirements) < MAX_REQS:
            self.requirements.append(text)

    def is_empty(self) -> bool:
        return not self.items and not self.requirements

    # ---- ledger: write-through persistence ----------------------------------
    def save_ledger(self, work_root, goal_criterion: str | None = None
                    ) -> str | None:
        """Write-through to <work_root>/ledger.json (the notes.md pattern):
        the plan survives a fresh run in the same workspace. Best-effort —
        a read-only or missing workspace must not fail the update itself."""
        if not work_root:
            return None
        data = {"v": 1, "goal_criterion": goal_criterion,
                "items": self.snapshot(),
                "requirements": list(self.requirements),
                "req_checks": {t: dict(c) for t, c in
                               self.req_checks.items()}}
        try:
            p = Path(work_root) / LEDGER_FILENAME
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
            return str(p)
        except OSError:
            return None

    def load_ledger(self, work_root, criterion: str | None) -> bool:
        """Rehydrate from <work_root>/ledger.json — ONLY into an empty list
        and ONLY when the ledger's goal criterion matches this run's (a
        /goal iteration continuing in its workspace). Anything else is a
        stale plan from unrelated work and stays untouched."""
        if not work_root or not self.is_empty():
            return False
        try:
            data = json.loads((Path(work_root) / LEDGER_FILENAME)
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) \
                or (data.get("goal_criterion") or None) != (criterion or None):
            return False
        self.replace(data.get("items") or [])
        self.requirements = [str(r).strip()[:MAX_REQ]
                             for r in (data.get("requirements") or [])
                             if str(r).strip()][:MAX_REQS]
        live = set(self.requirements)
        for t, c in (data.get("req_checks") or {}).items():
            if t in live and isinstance(c, dict) and c.get("check"):
                entry: dict = {"check": str(c["check"])[:MAX_CHECK]}
                if "verified" in c:
                    entry["verified"] = bool(c["verified"])
                self.req_checks[t] = entry
        return not self.is_empty()

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
            if "verified" in raw:        # ledger state survives the sync
                it["verified"] = bool(raw["verified"])
            if raw.get("baseline_ref"):
                it["baseline_ref"] = str(raw["baseline_ref"])
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
        parts = []
        if self.items:
            lines = []
            for it in self.items:
                line = f"{it['id']} [{it['status']}] {it['title']}"
                if it.get("check"):
                    line += " [check " + ("✓" if it.get("verified") is True
                            else "✗ FAILED" if it.get("verified") is False
                            else "— runs when marked done") + "]"
                if it["status"] == "working" and it["desc"]:
                    line += f" — {it['desc'][:200]}"
                lines.append(line)
            done, total = self.progress()
            parts.append(f"TODO LIST ({done}/{total} done — keep it current with the "
                         "todos tool: one item 'working', mark done/failed/skipped with "
                         "a short note as you go):\n" + "\n".join(lines))
        if self.requirements:
            reqs = []
            for r in self.requirements:
                rc = self.req_checks.get(r)
                if rc and rc.get("check"):
                    r += " [check " + ("✓ verified" if rc.get("verified") is True
                         else "✗ FAILED — fix, then todos verify"
                         if rc.get("verified") is False
                         else "— verify with todos action=verify before dropping") + "]"
                reqs.append(r)
            parts.append(
                "REQUIREMENTS (explicit requirements from the request — verify "
                "your final answer against every [must] and drop each one with "
                "the todos tool once met):\n"
                + "\n".join("- " + r for r in reqs))
        return "\n".join(parts)
