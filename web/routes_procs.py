"""Managed processes (brain, embed, rerank), the scheduled-prompts ticker and
the startup/shutdown hooks (split out of web/server.py)."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import HTTPException

from runtime.proc import spawn_background


def _usage_target(runtime, name: str, status: dict,
                  occupant: dict | None) -> tuple[int, str, dict]:
    """(pid, actual preset name, preset config) for a live local slot."""
    from runtime.preset_store import resolve_slot, slot_preset_name

    if status.get("alive"):
        pid = status.get("pid")
        preset_name = slot_preset_name(runtime.config, name) or name
        preset = resolve_slot(runtime.config, name)
    elif occupant:
        pid = occupant.get("pid")
        preset_name = str(occupant.get("model") or "custom")
        preset = (((runtime.config.get("models") or {}).get("presets") or {})
                  .get(preset_name) or {})
    else:
        raise HTTPException(409, f"{name}: no running local preset")
    if not pid:
        raise HTTPException(409, f"{name}: server pid is unavailable")
    return int(pid), preset_name, preset


async def _read_usage(pid: int, preset: dict, config: dict) -> dict:
    from runtime import model_usage
    try:
        return await asyncio.to_thread(
            model_usage.collect_usage, pid, preset, config)
    except Exception as exc:
        # A diagnostic panel should still show its identity when one probe
        # fails; the warning says why the numeric fields are empty.
        return {"pid": pid, "ram": {"used_gib": None}, "vram": [],
                "vram_source": None,
                "disk": {"artifacts": [], "mounts": [], "total_gib": 0},
                "warnings": [f"usage probe failed: {type(exc).__name__}: {exc}"]}


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
    users = s.users
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
    from web.scheduler_runtime import register as register_scheduler
    register_scheduler(s)

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

    @app.get("/api/admin/processes/{name}/usage")
    async def admin_process_usage(name: str):
        """On-demand live resources for the preset actually serving this slot.

        Deliberately separate from the 3-second process poll: /proc walks,
        vendor SMI calls and artifact sizes are comparatively expensive.
        """
        if name not in proc_mgr.names():
            raise HTTPException(404, f"unknown process: {name}")
        if _slot_remote(name):
            raise HTTPException(409, f"{name}: remote preset — local RAM, VRAM "
                                     "and disk usage are unavailable")
        st = proc_mgr.status().get(name) or {}
        occ = None if st.get("alive") else _slot_occupant(name)
        pid, preset_name, preset = _usage_target(runtime, name, st, occ)
        usage = await _read_usage(pid, preset, runtime.config)
        return {"slot": name, "preset": preset_name, "swapped": bool(occ), **usage}

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
