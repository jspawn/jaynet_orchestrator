"""Managed processes (brain, embed, rerank), the scheduled-prompts ticker and
the startup/shutdown hooks (split out of web/server.py)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import HTTPException


def register(app, s):
    runtime = s.runtime
    bus = s.bus
    tasks = s.tasks
    run_owner = s.run_owner
    users = s.users
    chats = s.chats
    _scratch_root = s._scratch_root
    _goal_kick = s.goal_kick   # set by web/routes_run.py (registered earlier)

    @app.on_event("startup")
    async def _apply_boot_posture() -> None:
        from runtime.boot_posture import apply_boot_posture
        asyncio.create_task(apply_boot_posture(runtime))

    @app.on_event("startup")
    async def _resume_active_goals() -> None:
        # A restart kills supervisor tasks; records still marked active resume.
        try:
            for row in users.list():
                un = row.get("username")
                if un and users.get_goal(un).get("status") == "active":
                    _goal_kick(un)
        except Exception:
            pass

    # ---- managed processes (brain, embed, rerank) ----
    from runtime.process_manager import ProcessManager
    proc_mgr = ProcessManager()
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

    @app.on_event("startup")
    async def _start_managed_processes() -> None:
        if proc_mgr.names():
            print(f"[process-manager] starting {len(proc_mgr.names())} processes: {', '.join(proc_mgr.names())}")
            await proc_mgr.start_all()

    @app.on_event("shutdown")
    async def _stop_managed_processes() -> None:
        if proc_mgr.names():
            print(f"[process-manager] stopping all managed processes")
            await proc_mgr.stop_all()

    # ---- scheduled prompts (schedule.* tools) ----
    from runtime.scheduler import ScheduleStore
    sched_cfg = (runtime.config.get("tools") or {}).get("schedule", {}) or {}
    sched_store = ScheduleStore(sched_cfg.get("store", "/srv/data/schedules.json"))

    def _scheduled_chat_turns(owner: str) -> tuple[str | None, list[dict]]:
        """(chat_id, turns) of the owner's '⏰ Scheduled runs' saved chat."""
        for c in chats.list(owner):
            if c["title"] == "⏰ Scheduled runs":
                full = chats.get(c["id"], owner) or {}
                return c["id"], full.get("turns", [])
        return None, []

    async def _fire_scheduled(entry: dict) -> None:
        owner = entry.get("owner")
        prompt = entry.get("prompt", "")
        run_id = uuid.uuid4().hex
        chat_id, _ = _scheduled_chat_turns(owner)
        wr = _scratch_root(owner, chat_id)

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        task = asyncio.create_task(runtime.run(
            "This is a SCHEDULED run the user set up earlier. Do what the task "
            "says, then give a short, direct report — it lands in the user's "
            "'⏰ Scheduled runs' chat.\n\nTASK:\n" + prompt,
            run_id=run_id, on_event=on_event, owner=owner,
            work_root=str(wr) if wr else None,
            auto_confirm=bool(sched_cfg.get("auto_confirm", True)),
            budget_overrides=sched_cfg.get("budget") or None,
            stream=True))
        tasks[run_id] = task
        run_owner[run_id] = owner
        try:
            out = await task
        finally:
            tasks.pop(run_id, None)
            run_owner.pop(run_id, None)
        chat_id, turns = _scheduled_chat_turns(owner)
        turns.append({"user_message": f"⏰ {prompt}",
                      "answer": out.get("answer", "") if isinstance(out, dict) else "",
                      "run_id": run_id,
                      "status": out.get("status") if isinstance(out, dict) else "error",
                      "events": []})
        chats.upsert(chat_id, "⏰ Scheduled runs", turns, owner=owner)

    async def _scheduler_tick() -> None:
        for entry in sched_store.due()[: int(sched_cfg.get("max_per_tick", 2))]:
            # mark BEFORE firing: crash => at-most-once (never a double-fire)
            sched_store.mark_fired(entry["id"])
            try:
                await _fire_scheduled(entry)
            except Exception as e:
                print(f"[scheduler] run {entry.get('id')} failed: {e}")

    @app.on_event("startup")
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

        asyncio.create_task(loop())
        print(f"[scheduler] enabled (tick {int(sched_cfg.get('tick_s', 30))}s, "
              f"store {sched_store.path})")

    # ---- admin: process management ----
    def _metrics_port(name: str) -> int | None:
        """The llama-server port for a managed process, via models.presets
        (process names mirror preset names: brain/specialist/embed/rerank)."""
        p = (runtime.config.get("models") or {}).get("presets") or {}
        try:
            port = (p.get(name) or {}).get("port")
            return int(port) if port else None
        except (TypeError, ValueError):
            return None

    async def _proc_stats(name: str, alive: bool) -> dict:
        """Stats for the process card: MTP acceptance parsed from the ring-buffer
        logs, plus cumulative counters from the server's own /metrics endpoint
        (uptime window — everything since the process booted)."""
        from web import server as _srv   # late: _parse_llama_metrics lives there
        st = proc_mgr.stats(name)
        port = _metrics_port(name)
        if not port or not alive:
            return st
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"http://127.0.0.1:{port}/metrics")
            m = _srv._parse_llama_metrics(r.text)
        except Exception:
            return st                       # server busy/down: log stats only
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
        return st

    @app.get("/api/admin/processes")
    async def admin_processes():
        data = proc_mgr.status()
        async def _enrich(item):
            name, st = item
            return name, {**st, "stats": await _proc_stats(name, bool(st.get("alive")))}
        pairs = await asyncio.gather(*(_enrich(i) for i in data.items()))
        return dict(pairs)

    @app.get("/api/admin/processes/{name}/logs")
    async def admin_process_logs(name: str, lines: int = 200):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        return {"name": name, "lines": proc_mgr.logs(name, lines)}

    @app.post("/api/admin/processes/{name}/restart")
    async def admin_process_restart(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.stop_one(name)
        await asyncio.sleep(1)
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name}

    @app.post("/api/admin/processes/{name}/stop")
    async def admin_process_stop(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.stop_one(name)
        return {"ok": True, "name": name}

    @app.post("/api/admin/processes/{name}/start")
    async def admin_process_start(name: str):
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        await proc_mgr.start_one(name)
        return {"ok": True, "name": name}
