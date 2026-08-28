"""Goal supervisor — drives a user's /goal across multiple agent runs.

One message to /api/chat runs ONE agent run; a goal is the exception: after
each run ends without the completion criterion met, this loop launches the
next run with a synthetic continuation message, until the model declares
`goal.complete` (double-checked by a tool-free judge call on the brain),
declares `goal.blocked`, or a goal-wide ceiling (turns / tokens / wall clock)
stops it. The record lives user-bound in users.db (UserStore.get_goal /
set_goal); progress turns are appended to the owner's current-chat snapshot so
every device sees them, and `active_run` lets any open browser attach live.

The supervisor is launched by web/routes_run.py (`_goal_kick`) and gets everything
it needs through the `deps` namespace — it never imports the server:

  deps.runtime   the AgentRuntime (brain access for the judge)
  deps.users     UserStore
  deps.chats     ChatStore (current-chat snapshot)
  deps.launch    async callable(**kw) -> (run_id, task) — the shared
                 _launch_agent_run helper; kw = username, message, history,
                 conversation_id, extra_system, run_overrides_extra
  deps.state_root  callable(owner, goal) -> path — the run's workspace
                 root (project files dir or chat scratch); /loop reads
                 STATE.md from there between iterations

/loop is the fresh-context sibling of /goal (the "Ralph" pattern): every
iteration launches with EMPTY history, so context never degrades — the
workspace files are the loop's only memory, with STATE.md as the harness-
injected state spine. /goal keeps its accumulated-history behaviour.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {"max_turns": 10, "max_total_tokens": 2_000_000,
            "max_wall_clock_s": 3600, "judge": True}

MAX_LOG = 20            # users.set_goal also caps; keep the two in line
_TAIL = 600             # chars of the previous answer folded into the next turn
_STATE_CAP = 2000       # chars of STATE.md injected into the next iteration


def config(runtime) -> dict:
    cfg = dict(DEFAULTS)
    raw = (runtime.config.get("goal") or {})
    for k in cfg:
        if raw.get(k) is not None:
            cfg[k] = raw[k]
    cfg["max_turns"] = int(cfg["max_turns"] or 0)
    cfg["max_total_tokens"] = int(cfg["max_total_tokens"] or 0)
    cfg["max_wall_clock_s"] = float(cfg["max_wall_clock_s"] or 0)
    cfg["judge"] = bool(cfg["judge"])
    return cfg


def parse(text: str) -> dict:
    """/goal + /loop grammar: '' -> status; stop|pause|resume; anything else
    starts a goal, with an optional `| done when: <criterion>` split. /loop
    marks the goal fresh-context (every iteration starts with empty history).
    """
    rest = text.strip()
    fresh = rest.startswith("/loop")
    rest = rest[len("/loop") if fresh else len("/goal"):].strip()
    if not rest:
        return {"action": "status"}
    low = rest.lower()
    if low in ("stop", "off", "cancel"):
        return {"action": "stop"}
    if low == "pause":
        return {"action": "pause"}
    if low == "resume":
        return {"action": "resume"}
    objective, criterion = rest, ""
    for sep in ("| done when:", "| done:"):
        i = low.find(sep)
        if i >= 0:
            objective, criterion = rest[:i].strip(), rest[i + len(sep):].strip()
            break
    if not objective:
        return {"action": "error", "error": "empty objective — /goal <what to "
                                            "achieve> [| done when: <criterion>]"}
    return {"action": "start", "objective": objective,
            "criterion": criterion or objective, "fresh": fresh}


def directive(goal: dict, turn: int, max_turns: int) -> str:
    """The per-run system block while a goal drives the run (extra_system)."""
    project = (f"The run is rooted in project '{goal['project_id']}' — all "
               f"file work lands there.\n" if goal.get("project_id") else "")
    if goal.get("fresh"):
        return (
            f"\n\n— Loop mode (iteration {turn}/{max_turns}) —\n"
            "You are one FRESH-CONTEXT iteration of a standing loop. You have "
            "no memory of earlier iterations — the workspace files are the "
            "loop's only memory.\n"
            f"OBJECTIVE: {goal['objective']}\n"
            f"DONE WHEN: {goal['criterion']}\n"
            f"{project}"
            "STATE.md in the workspace is the loop's memory: read it first "
            "(on iteration 1 create it — the plan), and update it before you "
            "finish: plan, status, next step, gotchas, kept short. Do the "
            "NEXT concrete unit of work, not the whole objective at once. "
            "When the 'done when' criterion is VERIFIABLY met, call "
            "goal.complete with the evidence (a judge double-checks). If you "
            "cannot make real progress — missing input only the user has, "
            "persistent external failure — call goal.blocked instead of "
            "spinning.")
    return (
        f"\n\n— Goal mode (turn {turn}/{max_turns}) —\n"
        f"You are working a standing objective across multiple runs.\n"
        f"OBJECTIVE: {goal['objective']}\n"
        f"DONE WHEN: {goal['criterion']}\n"
        f"{project}"
        "Work this turn like any other, but pace yourself for the turns left: "
        "don't start sub-tasks you can't finish or hand off cleanly. Local "
        "first — cloud models (llm.call) only when local can't do it. When the "
        "'done when' criterion is VERIFIABLY met, call goal.complete with the "
        "evidence (a judge double-checks). If you cannot make real progress — "
        "missing input only the user has, persistent external failure — call "
        "goal.blocked instead of spinning.")


def _continuation(goal: dict, turn: int, max_turns: int) -> str:
    if goal.get("fresh"):
        state = (goal.get("state") or "").strip()
        return (
            f"Loop iteration {turn}/{max_turns} — fresh context: no memory "
            "of earlier iterations, the workspace is your only memory.\n"
            "STATE.md as the last iteration left it:\n---\n"
            f"{state or '(no STATE.md yet — read the workspace and create it)'}\n"
            "---\n"
            "Files may have moved on since — verify against the workspace "
            "before trusting the note. Do the next concrete unit of work; "
            "update STATE.md before you finish. Call goal.complete only when "
            "the 'done when' criterion is verifiably met; goal.blocked if "
            "you're stuck.")
    log_entries = goal.get("log") or []
    prev = log_entries[-1] if log_entries else {}
    note = prev.get("note") or "(no record)"
    return (
        f"Goal turn {turn}/{max_turns}. Continue working the objective in the "
        f"system directive — do NOT restate it, do NOT start over.\n"
        f"Where the last turn left off: {note}\n"
        "Do the next concrete step. Call goal.complete only when the 'done "
        "when' criterion is verifiably met; goal.blocked if you're stuck.")


async def _judge(runtime, goal: dict, answer: str) -> tuple[bool, str]:
    """Tool-free brain call: is the criterion met, given the final answer?
    Fails OPEN (accepts) when the brain errors — the model's own declaration
    plus its evidence stands, and a broken judge must not trap a goal."""
    messages = [
        {"role": "system", "content":
         "You verify goal completion. Reply YES or NO on the first line, then "
         "one sentence why. Be strict: partial progress is a NO."},
        {"role": "user", "content":
         f"OBJECTIVE: {goal['objective']}\nDONE WHEN: {goal['criterion']}\n\n"
         f"The agent declared completion with this final answer:\n"
         f"{answer[:4000]}\n\nIs the 'done when' criterion verifiably met?"},
    ]
    try:
        r = await runtime.complete(messages, think=False)
        content = (r.get("content") or "").strip()
    except Exception as e:
        log.warning("goal judge failed open: %s", e)
        return True, f"(judge unavailable: {type(e).__name__})"
    yes = content.upper().startswith("YES")
    reason = content.split("\n", 1)[-1].strip()[:300] if content else ""
    return yes, reason or content[:300]


def _snapshot_turns(deps, owner: str) -> tuple[dict | None, list]:
    row = deps.chats.get_current(owner)
    chat = (row or {}).get("chat")
    if not isinstance(chat, dict) or not isinstance(chat.get("turns"), list):
        return None, []
    return chat, chat["turns"]


def _publish(deps, owner: str, *, active_run: str | None,
             user_message: str | None = None, answer: str = "",
             run_id: str | None = None, status: str = "ok") -> None:
    """Append a goal turn to the owner's current-chat snapshot (so all devices
    see it) and set/clear active_run (so any open browser can attach live)."""
    chat, turns = _snapshot_turns(deps, owner)
    if chat is None:
        return
    if user_message is not None:
        turns.append({"user_message": user_message, "answer": answer,
                      "run_id": run_id, "status": status, "events": []})
    try:
        deps.chats.set_current(owner, chat, active_run=active_run)
    except Exception:
        log.exception("goal snapshot publish failed")


def _save(deps, username: str, goal: dict) -> None:
    goal["log"] = (goal.get("log") or [])[-MAX_LOG:]
    deps.users.set_goal(username, goal)


def _read_state(deps, goal: dict, owner: str) -> str:
    """STATE.md from the loop's workspace — the fresh-context state spine.
    Missing/unreadable is fine: the next iteration's note says so."""
    root_cb = getattr(deps, "state_root", None)
    if root_cb is None:
        return ""
    try:
        root = root_cb(owner, goal)
        p = Path(root) / "STATE.md" if root else None
        if p is not None and p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")[:_STATE_CAP]
    except OSError:
        pass
    return ""


async def supervise(deps, username: str) -> None:
    """The goal loop. One instance per user (server enforces); exits as soon
    as the record is no longer `active`."""
    cfg = config(deps.runtime)
    consecutive_failures = 0
    while True:
        goal = deps.users.get_goal(username)
        if goal.get("status") != "active":
            return
        turn = int(goal.get("turn") or 0) + 1
        tokens = int(goal.get("tokens_total") or 0)
        elapsed = time.time() - _started(goal)

        # Goal-wide ceilings — the hard stop for an unattended self-loop.
        stop = None
        if cfg["max_turns"] and turn > cfg["max_turns"]:
            stop = f"turn ceiling ({cfg['max_turns']})"
        elif cfg["max_total_tokens"] and tokens >= cfg["max_total_tokens"]:
            stop = f"token ceiling ({cfg['max_total_tokens']})"
        elif cfg["max_wall_clock_s"] and elapsed > cfg["max_wall_clock_s"]:
            stop = f"wall-clock ceiling ({int(cfg['max_wall_clock_s'])}s)"
        if stop:
            _finish(deps, username, goal, "blocked",
                    f"goal stopped: {stop} reached without completion")
            return

        msg = goal["objective"] if turn == 1 else _continuation(goal, turn,
                                                                cfg["max_turns"])
        # /loop's defining trait: every iteration starts with an EMPTY context
        # window — no accumulated history to degrade. /goal keeps the chat's.
        history = [] if goal.get("fresh") else _history(deps, username)
        _, conv_id = _snapshot_cid(deps, username)
        sink: list[dict] = []
        goal["turn"] = turn
        _save(deps, username, goal)
        try:
            run_id, task = await deps.launch(
                username=username, message=msg, history=history,
                conversation_id=conv_id,
                project_id=goal.get("project_id"),
                extra_system=directive(goal, turn, cfg["max_turns"]),
                run_overrides_extra={"goal": {"declarations": sink}})
        except Exception as e:
            log.exception("goal launch failed")
            _finish(deps, username, goal, "blocked",
                    f"couldn't launch turn {turn}: {type(e).__name__}: {e}")
            return
        # Record the live run id — but on a FRESH read: a pause/stop landing
        # during launch must not be overwritten by the stale pre-launch object.
        goal = deps.users.get_goal(username)
        goal["current_run"] = run_id
        _save(deps, username, goal)
        _publish(deps, username, active_run=run_id)
        try:
            result = await task
        except Exception as e:
            result = {"status": "error", "answer": "",
                      "error": f"{type(e).__name__}: {e}", "budget": {}}

        # Account the turn. RE-READ the record first: anything saved between
        # launch and now (a pause, a stop) must survive — writing the stale
        # pre-run object back would silently resurrect the goal.
        goal = deps.users.get_goal(username)
        goal["current_run"] = None
        b = (result.get("budget") or {}).get("tokens") or {}
        goal["tokens_total"] = int(goal.get("tokens_total") or 0) + int(
            b.get("total") or 0)
        if goal.get("status") != "active":         # paused/stopped mid-run
            _save(deps, username, goal)
            _publish(deps, username, active_run=None)
            return
        decl = sink[-1] if sink else None
        tail = (result.get("answer") or "")[-_TAIL:]
        (goal.setdefault("log", [])).append({
            "turn": turn, "status": result.get("status", "?"),
            "note": tail.replace("\n", " ")[:300]})
        if goal.get("fresh"):
            # Capture the state spine the iteration left behind — injected
            # into the next continuation (deterministic transfer, no reliance
            # on the model remembering to re-read the file).
            goal["state"] = _read_state(deps, goal, username)
        _kind = "loop" if goal.get("fresh") else "goal"
        _publish(deps, username, active_run=None,
                 user_message=f"🔄 {_kind} turn {turn}" if goal.get("fresh")
                 else f"🎯 {_kind} turn {turn}",
                 answer=result.get("answer") or "", run_id=run_id,
                 status=result.get("status", "error"))

        if decl and decl["status"] == "complete":
            if cfg["judge"]:
                ok, why = await _judge(deps.runtime, goal,
                                       result.get("answer") or "")
                if not ok:
                    (goal.setdefault("log", [])).append(
                        {"turn": turn, "status": "judge",
                         "note": f"completion rejected: {why}"})
                    _save(deps, username, goal)
                    continue                       # another turn, objection logged
            _finish(deps, username, goal, "done", decl["text"])
            return
        if decl and decl["status"] == "blocked":
            _finish(deps, username, goal, "blocked", decl["text"])
            return
        if result.get("status") in ("error", "stuck", "stalled",
                                    "budget_exceeded"):
            consecutive_failures += 1
            if consecutive_failures >= 2:
                _finish(deps, username, goal, "blocked",
                        f"two turns in a row ended '{result.get('status')}' "
                        f"({(result.get('error') or '')[:200]})")
                return
        else:
            consecutive_failures = 0
        _save(deps, username, goal)


def _started(goal: dict) -> float:
    try:
        return time.mktime(time.strptime(goal.get("started_at", ""),
                                         "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return time.time()


def _history(deps, username: str) -> list[dict]:
    """Prior chat turns as [{role, content}] for run continuity."""
    _, turns = _snapshot_turns(deps, username)
    h: list[dict] = []
    for t in turns[-20:]:
        if t.get("user_message"):
            h.append({"role": "user", "content": t["user_message"]})
        if t.get("answer"):
            h.append({"role": "assistant", "content": t["answer"]})
    return h[-40:]


def _snapshot_cid(deps, username: str) -> tuple[dict | None, str | None]:
    chat, _ = _snapshot_turns(deps, username)
    return chat, (chat or {}).get("cid")


def _finish(deps, username: str, goal: dict, status: str, note: str) -> None:
    """Terminal transition: record + a visible wrap-up turn in the chat."""
    goal["status"] = status
    goal["current_run"] = None
    (goal.setdefault("log", [])).append(
        {"turn": int(goal.get("turn") or 0), "status": status, "note": note[:300]})
    _save(deps, username, goal)
    icon = "✅" if status == "done" else "⛔"
    _publish(deps, username, active_run=None,
             user_message=f"{icon} goal {status}",
             answer=note, run_id=None, status="ok" if status == "done" else "error")


def format_status(goal: dict, max_turns: int) -> str:
    """The bare-`/goal` status card."""
    if not goal.get("objective"):
        return ("no goal set. `/goal <objective> [| done when: <criterion>]` "
                "starts one — it runs turn by turn until the criterion is met, "
                "it's blocked, or a ceiling stops it. `/loop <objective>` does "
                "the same with a fresh context every iteration.")
    lines = [f"**{'loop' if goal.get('fresh') else 'goal'}** — "
             f"{goal['objective']}",
             f"done when: {goal['criterion']}",
             f"status: {goal.get('status', '?')} · turn "
             f"{goal.get('turn', 0)}/{max_turns} · "
             f"{goal.get('tokens_total', 0):,} tokens spent"]
    log_entries = goal.get("log") or []
    if log_entries:
        last = log_entries[-1]
        lines.append(f"last: [{last.get('status')}] {last.get('note', '')}")
    if goal.get("status") == "paused":
        lines.append("paused — `/goal resume` continues.")
    return "\n".join(lines)
