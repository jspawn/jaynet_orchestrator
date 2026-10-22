"""Managed processes (brain, embed, rerank), the scheduled-prompts ticker and
the startup/shutdown hooks (split out of web/server.py)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
from fastapi import HTTPException

from runtime.proc import spawn_background
from web import projects as PJ
from web.routes_run import _schedule_tool_gate


def _sched_runs_db(s, chats) -> str:
    """The chats.db path the ScheduleRunStore shares with Chat/Flag/Report —
    web_cfg first (server wiring), then the store's own path (test fakes)."""
    from runtime import paths as _paths
    return ((getattr(s, "web_cfg", {}) or {}).get("chats_db")
            or getattr(chats, "db_path", None) or str(_paths.CHATS_DB))


def _schedule_owner_revoked(users, owner: str | None) -> bool:
    """A non-admin owner whose schedules_allowed flag was pulled stops
    firing. Deleted accounts (unknown owner) stay the orphan path's job."""
    u = users.get(owner) if owner else None
    return (u is not None and not u.get("is_admin")
            and not users.get_schedules_allowed(owner))


def _sched_work_root(entry: dict, owner: str | None, chat_id: str | None,
                     projects_dir, scratch_root) -> tuple:
    """(work_root, note) for a firing: the bound project's files root, or the
    per-chat scratch root (with a history note when the project is gone)."""
    pid = entry.get("project_id")
    if pid and owner and projects_dir is not None:
        wr = PJ.files_root(projects_dir, owner, os.path.basename(str(pid)))
        if wr is not None:
            return wr, ""
        return scratch_root(owner, chat_id), "project missing, used scratch — "
    return scratch_root(owner, chat_id), ""


def _run_outcome(out) -> tuple[str, str, int | None]:
    """(status, answer, tokens) from a runtime.run result dict."""
    if not isinstance(out, dict):
        return "error", "", None
    return ((out.get("status") or "error"), str(out.get("answer", "")),
            out.get("total_tokens"))


def _record_run(sched_runs, sched_store, entry: dict, run_id: str | None,
                status: str, duration_s: float | None, excerpt: str,
                tokens: int | None) -> None:
    """History row + last_status on the entry. Best-effort: a recording
    failure must not swallow the run's own error path."""
    sid = str(entry.get("id") or "?")
    try:
        sched_runs.add(sid, entry.get("owner"), run_id, status,
                       duration_s, excerpt[:500], tokens)
        sched_store.set_status(sid, status)
    except Exception as e:
        print(f"[scheduler] history record for {sid} failed: {e}")


class _DenyConfirm:
    """Refuse every confirmation: a scheduled run has no one at the
    screen, and without a provider the loop's built-in gate falls back
    to a TTY input() prompt — on a server started from an interactive
    shell that hangs the run forever (code audit 2026-10-14). Admins'
    auto_confirm still applies; it bypasses the provider entirely."""

    async def confirm(self, run_id, name, args, emit=None, reason=None):
        return False


def _drop_revoked(sched_runs, sched_store, users, entries: list) -> list:
    """Split off due entries whose non-admin owner lost the
    schedules_allowed flag: disable each (it stops being due — no spin)
    with one history row + log line per revocation, return the rest."""
    out = []
    for e in entries:
        # Admin-planted entries (created_by someone other than the owner —
        # the admin Schedules tab) are the admin's responsibility: they keep
        # firing when the owner's flag is off/pulled, and the admin removes
        # them there. Self-created entries (no created_by, or the owner
        # themselves) die with the flag (code audit 2026-10-14, claude L3).
        planted = e.get("created_by")
        if planted and planted != e.get("owner"):
            out.append(e)
            continue
        if not _schedule_owner_revoked(users, e.get("owner")):
            out.append(e)
            continue
        sid = str(e.get("id"))
        sched_store.set_enabled(sid, False)
        _record_run(sched_runs, sched_store, e, None,
                    "disabled: schedules revoked", None, "", None)
        print(f"[scheduler] {sid}: owner '{e.get('owner')}' lost schedules "
              f"access — entry disabled")
    return out


async def _add_server_metrics(st: dict, port: int) -> None:
    """Fill the process-card tok/s + token counters from the server's own
    /metrics (uptime window). llama.cpp speaks llamacpp:* counters with
    cumulative seconds; vllm/radiance speak labelled vllm:* histograms
    (mapping in web.server._vllm_stats_from_metrics). A busy/down server
    keeps the log-parsed stats only."""
    from web import server as _srv  # late: the parsers live there
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"http://127.0.0.1:{port}/metrics")
    except Exception:
        return
    m = _srv._parse_llama_metrics(r.text)
    if not m:
        st.update(_srv._vllm_stats_from_metrics(
            _srv._parse_prom_metrics(r.text, ("vllm:", "radiance:"))))
        return
    gen_tok = m.get("tokens_predicted_total", 0.0)
    gen_s = m.get("tokens_predicted_seconds_total", 0.0)
    pp_tok = m.get("prompt_tokens_total", 0.0)
    pp_s = m.get("prompt_seconds_total", 0.0)
    if gen_s > 0:
        st["gen_tps_avg"] = round(gen_tok / gen_s, 1)
    if pp_s > 0:
        st["prompt_tps_avg"] = round(pp_tok / pp_s, 1)
    st["tokens_generated"] = int(gen_tok)
    st["tokens_prompt"] = int(pp_tok)


def register(app, s):
    runtime = s.runtime
    bus = s.bus
    tasks = s.tasks
    run_owner = s.run_owner
    users = s.users
    chats = s.chats
    _scratch_root = s._scratch_root
    _goal_kick = s.goal_kick   # set by web/routes_run.py (registered earlier)

    async def _apply_boot_posture() -> None:
        from runtime.boot_posture import apply_boot_posture
        spawn_background(apply_boot_posture(runtime), name="boot-posture")

    async def _resume_active_goals() -> None:
        # A restart kills supervisor tasks; records still marked active resume.
        try:
            for row in users.list():
                un = row.get("username")
                if un and users.get_goal(un).get("status") == "active":
                    _goal_kick(un)
        except Exception:
            pass

    s.startup_hooks += [_apply_boot_posture, _resume_active_goals]

    # ---- managed processes (brain, embed, rerank) ----
    from runtime import process_manager
    from runtime.process_manager import ProcessManager
    proc_mgr = ProcessManager()
    process_manager.CURRENT = proc_mgr
    _proc_cfg = runtime.config.get("processes") or {}
    for pname, pcfg in _proc_cfg.items():
        if not pcfg.get("command"):
            continue
        proc_mgr.add(
            pname, pcfg["command"],
            env=pcfg.get("env") or {},
            cwd=pcfg.get("cwd"),
            restart=pcfg.get("restart", True),
            restart_delay=pcfg.get("restart_delay", 10),
            start_delay=pcfg.get("start_delay", 0),
            kill_signal=pcfg.get("kill_signal", 9),
            max_log_lines=pcfg.get("max_log_lines", 2000),
        )

    async def _start_managed_processes() -> None:
        for name in proc_mgr.names():
            if _slot_disabled(name):
                print(f"[process-manager] {name}: slot empty — not starting "
                      f"(assign a preset in admin → Models → Presets to enable)")
                continue
            if (remote := _slot_remote(name)):
                print(f"[process-manager] {name}: remote slot ({remote}) — "
                      f"probe only, nothing to launch locally")
                continue
            await proc_mgr.start_one(name)
        if proc_mgr.names():
            print(f"[process-manager] {len(proc_mgr.names())} managed processes: "
                  f"{', '.join(proc_mgr.names())}")

    async def _stop_managed_processes() -> None:
        if proc_mgr.names():
            print("[process-manager] stopping all managed processes")
            await proc_mgr.stop_all()

    s.startup_hooks.append(_start_managed_processes)
    s.shutdown_hooks.append(_stop_managed_processes)

    # ---- scheduled prompts (schedule.* tools) ----
    from runtime import paths as _paths
    from runtime.scheduler import ScheduleStore
    from web.store import ScheduleRunStore
    sched_cfg = (runtime.config.get("tools") or {}).get("schedule", {}) or {}
    sched_store = ScheduleStore(
        sched_cfg.get("store", str(_paths.DATA / "schedules.json")))
    # Firing history: one row per scheduled run, same SQLite file as the
    # chats/flags/reports tables (see web/store.py ScheduleRunStore).
    sched_runs = ScheduleRunStore(_sched_runs_db(s, chats))
    _projects_dir = getattr(s, "projects_dir", None)

    def _scheduled_chat_turns(owner: str) -> tuple[str | None, list[dict]]:
        """(chat_id, turns) of the owner's '⏰ Scheduled runs' saved chat."""
        for c in chats.list(owner):
            if c["title"] == "⏰ Scheduled runs":
                full = chats.get(c["id"], owner) or {}
                return c["id"], full.get("turns", [])
        return None, []

    # Schedule ids whose owner no longer exists — logged once per entry, then
    # skipped silently (defence in depth for rows written before
    # admin_delete_user cleaned up schedules; the entry stays due but never
    # fires again).
    _sched_orphan_logged: set[str] = set()

    async def _fire_scheduled(entry: dict) -> None:
        owner = entry.get("owner")
        # Audit 2026-10-12: a schedule outliving its account must not run.
        if owner and users.get(owner) is None:
            sid = str(entry.get("id") or "?")
            if sid not in _sched_orphan_logged:
                _sched_orphan_logged.add(sid)
                print(f"[scheduler] skipping {sid}: owner '{owner}' no longer "
                      f"exists — delete the schedule entry")
            return
        prompt = entry.get("prompt", "")
        run_id = uuid.uuid4().hex
        t0 = time.time()
        chat_id, _ = _scheduled_chat_turns(owner)
        # A schedule bound to a project runs with that project's files root as
        # work_root; a deleted project falls back to the per-chat scratch root
        # (the history excerpt says so).
        wr, fire_note = _sched_work_root(entry, owner, chat_id,
                                         _projects_dir, _scratch_root)
        # Role policy: the run carries the scheduling account's role — a
        # non-admin's scheduled run keeps the admin-only tool boundary and
        # can't inherit auto-confirm (audit #1).
        is_admin = bool((users.get(owner) or {}).get("is_admin")) if owner else False

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        task = asyncio.create_task(runtime.run(
            "This is a SCHEDULED run the user set up earlier. Do what the task "
            "says, then give a short, direct report — it lands in the user's "
            "'⏰ Scheduled runs' chat.\n\nTASK:\n" + prompt,
            run_id=run_id, on_event=on_event, owner=owner,
            is_admin=is_admin,
            work_root=str(wr) if wr else None,
            # Per-run scratch key: without it the depth-0 start-of-run wipe
            # cleans .tmp/scratch itself — rmtree-ing the keyed scratch dirs
            # of every other run sharing this root (audit #24 C1: a scheduled
            # run starting mid-chat deleted the chat's temp files).
            scratch_key=run_id,
            auto_confirm=bool(sched_cfg.get("auto_confirm", True)) and is_admin,
            # No human at the screen: never let the built-in TTY prompt
            # trigger (see _DenyConfirm above).
            confirm_provider=_DenyConfirm(),
            budget_overrides=sched_cfg.get("budget") or None,
            # The unattended path must respect the same governance layer as
            # every other launcher (readiness audit AI-3): a globally
            # disabled tool stays disabled for scheduled runs too, and the
            # per-user schedule gate follows the owner's account (a
            # non-allowed owner's run can't plant new schedules).
            disabled_tools=(set(users.get_global_disabled_tools())
                            | _schedule_tool_gate(users, owner or "", is_admin)),
            stream=True))
        tasks[run_id] = task
        run_owner[run_id] = owner
        try:
            out = await task
        finally:
            tasks.pop(run_id, None)
            run_owner.pop(run_id, None)
            # Retire the event-bus replay buffer like every other launcher
            # (BE-4: scheduled runs used to keep up to 500 events carrying
            # tool args + result previews for the process lifetime).
            async def _forget() -> None:
                from web import server as _srv
                await asyncio.sleep(_srv._FORGET_AFTER_S)
                bus.forget(run_id)
            spawn_background(_forget(), name=f"bus-forget-{run_id[:8]}")
        chat_id, turns = _scheduled_chat_turns(owner)
        status, answer, tokens = _run_outcome(out)
        _record_run(sched_runs, sched_store, entry, run_id, status,
                    round(time.time() - t0, 1), fire_note + answer, tokens)
        turns.append({"user_message": f"⏰ {prompt}",
                      "answer": answer,
                      "run_id": run_id,
                      "status": status,
                      "events": []})
        chats.upsert(chat_id, "⏰ Scheduled runs", turns, owner=owner)

    # Schedule ids with a run currently in flight — the tick never fires an
    # entry again while its previous run is still running (a long run would
    # otherwise be re-fired every tick once its next interval elapses).
    _sched_in_flight: set[str] = set()

    async def _run_scheduled(entry: dict) -> None:
        sid = entry.get("id")
        try:
            await _fire_scheduled(entry)
        except Exception as e:
            print(f"[scheduler] run {sid} failed: {e}")
            _record_run(sched_runs, sched_store, entry, None, "error", None,
                        f"run failed: {e}", None)
        finally:
            _sched_in_flight.discard(sid)

    async def _scheduler_tick() -> None:
        due = sched_store.due()[: int(sched_cfg.get("max_per_tick", 2))]
        # A non-admin owner whose schedules_allowed flag was pulled stops
        # firing — _drop_revoked disables those entries instead.
        for entry in _drop_revoked(sched_runs, sched_store, users, due):
            sid = entry.get("id")
            if sid in _sched_in_flight:
                continue                    # previous run still going
            # mark BEFORE firing: crash => at-most-once (never a double-fire)
            sched_store.mark_fired(sid)
            _sched_in_flight.add(sid)
            # Fire-and-track, like chat runs: the tick launches each due entry
            # as its own task and returns without awaiting run completion.
            spawn_background(_run_scheduled(entry), name=f"scheduled-{sid[:8]}")

    s.scheduler_tick = _scheduler_tick   # tests drive the tick without the loop

    async def _start_scheduler() -> None:
        if not sched_cfg.get("enabled", True):
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(int(sched_cfg.get("tick_s", 30)))
                try:
                    await _scheduler_tick()
                except Exception as e:
                    print(f"[scheduler] tick failed: {e}")

        # Hold a reference — asyncio only weak-refs tasks, so a bare
        # create_task can be GC'd mid-flight and the heartbeat silently stops
        # (the eval scheduler already does this; audit B1).
        s.prompt_sched_task = asyncio.create_task(loop())
        print(f"[scheduler] enabled (tick {int(sched_cfg.get('tick_s', 30))}s, "
              f"store {sched_store.path})")

    s.startup_hooks.append(_start_scheduler)

    # ---- admin: process management ----
    def _slot_disabled(name: str) -> bool:
        """A managed process whose boot SLOT is explicitly empty stays down.
        Only slot-named processes are affected; custom process entries and
        slots with no row at all (fallback to the like-named preset) run."""
        from runtime.preset_store import SLOTS
        if name not in SLOTS:
            return False
        slots = (runtime.config.get("models") or {}).get("slots") or {}
        return name in slots and not slots[name]

    def _slot_remote(name: str) -> str:
        """The remote host if the slot's preset is served by another LAN box
        ("" otherwise). Remote slots never launch locally — probe-only."""
        from runtime.preset_store import SLOTS, resolve_slot
        if name not in SLOTS:
            return ""
        try:
            return (resolve_slot(runtime.config, name).get("remote_host")
                    or "").strip()
        except Exception:
            return ""

    def _metrics_port(name: str) -> int | None:
        """The llama-server port for a managed process, resolved through the
        slot mapping (process names mirror slot names: brain/specialist/...)."""
        from runtime.preset_store import resolve_slot
        try:
            port = resolve_slot(runtime.config, name).get("port")
            return int(port) if port else None
        except (TypeError, ValueError):
            return None

    def _serve_state_dir() -> str:
        """The serve.* registry dir (already config-load-resolved to absolute)."""
        from runtime.paths import SERVE_DIR
        cfg = (runtime.config.get("tools") or {}).get("serve") or {}
        return cfg.get("state_dir") or str(SERVE_DIR)

    def _slot_occupant(name: str) -> dict | None:
        """A serve-managed server (model.use strength swap) holding this slot's
        port while the boot process is down — the swap the Models → Servers tab
        otherwise can't see. None when the port is free or the occupant is
        unmanaged (systemd/foreign — never touched here)."""
        from runtime import serving as S
        port = _metrics_port(name)
        if not port:
            return None
        try:
            for e in S.list_servers(_serve_state_dir()):
                if int(e.get("port") or 0) == port and S.pid_alive(e.get("pid")):
                    return e
        except Exception:
            pass
        return None

    async def _stop_occupant(name: str) -> str:
        """Stop a serve-managed occupant of the slot's port (swap teardown so
        the boot preset can retake the slot). Returns the server name or ''.
        A live occupant that refuses the stop (pid identity mismatch) is a
        409 — silently starting the boot preset onto a held port would
        crash-loop it."""
        from runtime import serving as S
        e = _slot_occupant(name)
        if not e:
            return ""
        stopped = await asyncio.to_thread(S.stop_server, e)
        if not stopped:
            raise HTTPException(
                409, f"the swapped-in server '{e.get('name')}' (pid "
                     f"{e.get('pid')}) refused to stop — stop it manually "
                     f"(kill {e.get('pid')}) and retry")
        S.delete_server(_serve_state_dir(), e.get("name"))
        return e.get("name") or ""

    async def _wait_port_free(port: int, timeout_s: float = 10.0) -> None:
        """Bounded wait for a port to close after a swap teardown — starting
        the boot preset onto a still-bound port would crash-loop it."""
        import time
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            closed = await asyncio.to_thread(_port_closed, port)
            if closed:
                return
            await asyncio.sleep(0.5)

    def _port_closed(port: int) -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return False
        except OSError:
            return True

    async def _proc_stats(name: str, alive: bool) -> dict:
        """Stats for the process card: MTP acceptance parsed from the ring-buffer
        logs, plus cumulative counters from the server's own /metrics endpoint
        (uptime window — everything since the process booted)."""
        st = proc_mgr.stats(name)
        port = _metrics_port(name)
        if not port or not alive:
            return st
        await _add_server_metrics(st, port)
        return st

    @app.get("/api/admin/processes")
    async def admin_processes():
        data = proc_mgr.status()
        async def _enrich(item):
            name, st = item
            # A strength swap (model.use) stops the boot process through the
            # manager and serves the tagged preset via the serve registry —
            # surface that occupant or the slot looks dead while it serves.
            occ = None if st.get("alive") else _slot_occupant(name)
            swap = None
            if occ:
                swap = {"server": occ.get("name"),
                        "model": occ.get("served_model_id") or occ.get("model"),
                        "pid": occ.get("pid"),
                        "started_at": occ.get("started_at")}
            serving = bool(st.get("alive")) or bool(occ)
            return name, {**st, "disabled": _slot_disabled(name),
                          "remote": _slot_remote(name), "swap": swap,
                          "stats": await _proc_stats(name, serving)}
        pairs = await asyncio.gather(*(_enrich(i) for i in data.items()))
        return dict(pairs)

    @app.get("/api/admin/processes/{name}/logs")
    async def admin_process_logs(name: str, lines: int = 200):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        # Swapped slot: the boot process's ring buffer is stale — show the
        # serve-managed occupant's log instead (that's what's running).
        if not (proc_mgr.status().get(name) or {}).get("alive"):
            occ = _slot_occupant(name)
            if occ and occ.get("log_dir"):
                from pathlib import Path

                from runtime import serving as S
                tail = S.tail(Path(occ["log_dir"]) / "stderr.log", lines)
                return {"name": name, "lines": tail.splitlines(),
                        "swap": occ.get("name")}
        return {"name": name, "lines": proc_mgr.logs(name, lines)}

    @app.post("/api/admin/processes/{name}/restart")
    async def admin_process_restart(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        if _slot_disabled(name):
            raise HTTPException(
                409, f"{name}: its boot slot is empty — assign a preset in "
                     f"admin → Models → Presets (Boot model slots) first")
        if (remote := _slot_remote(name)):
            raise HTTPException(
                409, f"{name}: remote slot — served by {remote}, probe only. "
                     f"Start llama-server on {remote}, not here.")
        swap_stopped = await _stop_occupant(name)   # 409 before touching the boot proc
        await proc_mgr.stop_one(name)
        await asyncio.sleep(1)
        port = _metrics_port(name)
        if swap_stopped and port:
            await _wait_port_free(port)
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name, "swap_stopped": swap_stopped or None}

    @app.post("/api/admin/processes/{name}/stop")
    async def admin_process_stop(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        if (remote := _slot_remote(name)):
            raise HTTPException(
                409, f"{name}: remote slot — served by {remote}, probe only. "
                     f"Stop llama-server on {remote}, not here.")
        await proc_mgr.stop_one(name)
        swap_stopped = await _stop_occupant(name)
        return {"ok": True, "name": name, "swap_stopped": swap_stopped or None}

    @app.post("/api/admin/processes/{name}/start")
    async def admin_process_start(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        if _slot_disabled(name):
            raise HTTPException(
                409, f"{name}: its boot slot is empty — assign a preset in "
                     f"admin → Models → Presets (Boot model slots) first")
        if (remote := _slot_remote(name)):
            raise HTTPException(
                409, f"{name}: remote slot — served by {remote}, probe only. "
                     f"Start llama-server on {remote}, not here.")
        # A swapped-in (serve-managed) model holds the slot's port — free it
        # first or the boot preset crash-loops on the bind.
        swap_stopped = await _stop_occupant(name)
        port = _metrics_port(name)
        if swap_stopped and port:
            await _wait_port_free(port)
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name, "swap_stopped": swap_stopped or None}
