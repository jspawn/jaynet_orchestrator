"""`serve.*` — manage model servers on this box's GPUs.

Promotes the gpu-serve skill's manual recipe to a managed capability: bring a
second model, an embedder, or a reranker up on GPU 1 (keeping GPU 0 for the
brain), wait until it answers, and track it so it can be reused, health-checked,
and torn down to free VRAM. A served LLM can optionally be registered as a LiteLLM
alias so `agent.spawn(model=...)` / `llm.call` can target it — which is what lets a
spawned sub-agent run in parallel with the brain on the second card.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from runtime import serving as S
from runtime.tool_base import Tool, ToolContext, ToolResult


def _cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}) or {}).get("serve", {}) or {}


def _state_dir(ctx: ToolContext) -> str:
    from runtime.paths import SERVE_DIR
    return _cfg(ctx).get("state_dir", str(SERVE_DIR))


def _litellm(ctx: ToolContext) -> tuple[str, str | None]:
    from runtime.paths import LITELLM_BASE
    cfg = _cfg(ctx)
    base = cfg.get("litellm_admin_base") or \
        ctx.config.get("orchestrator", {}).get("litellm_base", LITELLM_BASE)
    return base, os.environ.get("LITELLM_MASTER_KEY")


def _age(entry: dict) -> str:
    try:
        t0 = datetime.fromisoformat(entry["started_at"].replace("Z", "+00:00"))
        secs = (datetime.now(UTC) - t0).total_seconds()
    except (KeyError, ValueError):
        return "?"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    return f"{secs / 3600:.1f}h"


def _view(ctx: ToolContext, entry: dict) -> dict:
    alive = S.pid_alive(entry.get("pid"))
    return {"name": entry["name"], "kind": entry.get("kind"),
            "model": entry.get("served_model_id") or entry.get("model"),
            "gpu": entry.get("gpu"), "port": entry.get("port"),
            "base_url": entry.get("base_url"),
            "state": "running" if alive else "dead",
            "litellm_alias": entry.get("litellm_alias"),
            "uptime": _age(entry) if alive else None, "pid": entry.get("pid")}


class ServeStart(Tool):
    name = "serve.start"
    description = (
        "Launch a model server (a second LLM, an embedder, or a reranker) on a GPU, "
        "pinned to GPU 1 by default so GPU 0 stays free for the brain. Give a `preset` "
        "(resolved by the serve dispatcher) or an explicit `command`. Checks VRAM "
        "headroom, picks a free port, waits until the server is healthy, and tracks it. "
        "An LLM can be registered as a LiteLLM alias (default on) so agent.spawn / "
        "llm.call can target it by name. Consumes VRAM — confirm before launching.")
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short handle for this server (e.g. 'fast-llm', 'embedder')."},
            "preset": {"type": "string", "description": "Preset name passed to the serve dispatcher."},
            "command": {"type": "string", "description": "Explicit launch command (advanced; overrides preset)."},
            "kind": {"type": "string", "enum": ["llm", "embedding", "rerank"], "description": "Server role. Default llm."},
            "gpu": {"type": "string", "description": "HIP_VISIBLE_DEVICES value. Default '1'."},
            "port": {"type": "integer", "description": "Port to bind. Default: next free from port_base."},
            "extra_args": {"type": "string", "description": "Extra flags appended to the preset command."},
            "est_vram_gib": {"type": "number", "description": "Rough VRAM the model needs; used for the headroom check."},
            "register": {"type": "boolean", "description": "Register an LLM as a LiteLLM alias (default true for kind=llm)."},
            "alias": {"type": "string", "description": "LiteLLM alias to register under (default: the server `name`). model.use passes the catalog preset's alias so the registered name is the one callers were told to use."},
            "llama_bin": {"type": "string", "description": "Resolved llama-server binary path, exported as LLAMA_BIN for the launch. model.use passes the preset registry's binary — file-mode launches otherwise fall back to the built-in default path."},
            "wire_rag": {"type": "boolean", "description": "For embedding/rerank: point rag.* at this server for the rest of the session."},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cfg = _cfg(ctx)
        name = S_slug(args["name"])
        if S.read_server(_state_dir(ctx), name) and S.pid_alive(
                (S.read_server(_state_dir(ctx), name) or {}).get("pid")):
            return ToolResult(status="error", result=None,
                              error=f"a server named '{name}' is already running; "
                                    f"serve.stop it first or pick another name")
        kind = args.get("kind", "llm")
        gpu = str(args.get("gpu", cfg.get("default_gpu", "1")))
        host = cfg.get("host", "127.0.0.1")
        state_dir = _state_dir(ctx)

        # Remote presets are served by another LAN box — nothing to launch here.
        preset_arg = args.get("preset")
        if preset_arg:
            cp = ((ctx.config.get("models") or {}).get("presets") or {}).get(
                str(preset_arg))
            if cp and (cp.get("remote_host") or "").strip():
                return ToolResult(
                    status="error", result=None,
                    error=f"'{preset_arg}' is a remote preset — already served at "
                          f"{cp['remote_host']}:{cp.get('port') or 8080} on another box. "
                          f"serve.* manages only local processes; start llama-server on "
                          f"{cp['remote_host']} instead.")

        # port
        reserved = set(cfg.get("reserved_ports", [8090, 4000])) | S.taken_ports(state_dir)
        try:
            port = int(args["port"]) if args.get("port") else \
                S.pick_free_port(int(cfg.get("port_base", 8091)), reserved, host)
        except RuntimeError as e:
            return ToolResult(status="error", result=None, error=str(e))

        # VRAM headroom (advisory if unreadable)
        free = S.gpu_free_gib(ctx, gpu)
        need = float(args.get("est_vram_gib") or 0)
        floor = float(cfg.get("min_free_vram_gib", 1.0))
        vram_note = None
        if free is not None:
            if need and free < need:
                return ToolResult(status="error", result=None,
                                  error=f"GPU {gpu} has ~{free} GiB free but ~{need} GiB "
                                        f"requested — free VRAM (serve.stop a server) or pick another GPU")
            if free < floor:
                return ToolResult(status="error", result=None,
                                  error=f"GPU {gpu} has only ~{free} GiB free — too full to launch")
            vram_note = f"GPU {gpu}: ~{free} GiB free before launch"
        else:
            vram_note = "VRAM not read (rocm-smi unavailable) — launching without a headroom check"

        # command
        command = args.get("command")
        if not command:
            preset = args.get("preset")
            if not preset:
                return ToolResult(status="error", result=None,
                                  error="provide a `preset` or an explicit `command`")
            from runtime import paths as _paths
            dispatcher = cfg.get(
                "dispatcher", str(_paths.HOME / "scripts" / "start-model.sh"))
            template = cfg.get("command_template",
                               "{dispatcher} {preset} --host {host} --port {port}")
            command = template.format(dispatcher=dispatcher, preset=preset,
                                      host=host, port=port)
            if args.get("extra_args"):
                command += " " + args["extra_args"]

        # A resolved binary from the preset registry (model.use) rides in the
        # child ENVIRONMENT (launch_server's env_extra) — file-mode
        # start-model.sh reads LLAMA_BIN first, so a custom-build box
        # (rocm/vulkan outside $JAYNET_HOME/bin) launches the right binary
        # instead of dying 'llama-server not found'. NOT a command prefix:
        # bash's exec does not parse VAR=val assignments after it (live: the
        # first prefix attempt became the executable path).
        launch_env = None
        llama_bin = (args.get("llama_bin") or "").strip()
        if llama_bin:
            launch_env = {"LLAMA_BIN": llama_bin}

        base_url = f"http://{host}:{port}"
        from runtime.paths import WORK_DIR
        launch = S.launch_server(
            state_dir, name, command,
            cwd=cfg.get("default_cwd", str(WORK_DIR)),
            gpu=gpu, source_env=bool(cfg.get("source_env", True)),
            env_setup=cfg.get("env_setup"), env_extra=launch_env)

        entry = {"name": name, "kind": kind, "model": args.get("preset") or "custom",
                 "served_model_id": None, "gpu": launch["gpus"], "port": port,
                 "host": host, "base_url": base_url, "pid": launch["pid"],
                 "command": command, "log_dir": launch["log_dir"],
                 "started_at": S._now_iso(), "status": "starting",
                 "litellm_alias": None, "litellm_model_id": None,
                 "request_id": ctx.request_id}
        S.write_server(state_dir, entry)

        # wait for readiness
        timeout = float(cfg.get("health_timeout_s", 90))
        healthy = await S.wait_healthy(base_url, timeout, pid=launch["pid"])
        if not healthy:
            if not S.pid_alive(launch["pid"]):
                S.delete_server(state_dir, name)
                return ToolResult(status="error", result=None,
                                  error=f"server '{name}' exited before becoming healthy. "
                                        f"Last log lines:\n{S.tail(launch['stderr'], 15)}")
            # still loading (big model) — keep it, report as starting
            return ToolResult(status="ok", result={
                "name": name, "state": "starting", "base_url": base_url, "port": port,
                "gpu": launch["gpus"], "pid": launch["pid"], "vram": vram_note,
                "note": f"launched but not healthy after {int(timeout)}s — a large model "
                        f"may still be loading; poll serve.health/serve.status",
                "log_dir": launch["log_dir"]})

        served_id = await S.query_model_id(base_url) or (args.get("preset") or name)
        entry["served_model_id"] = served_id
        entry["status"] = "running"

        result = {"name": name, "state": "running", "kind": kind, "base_url": base_url,
                  "port": port, "gpu": launch["gpus"], "pid": launch["pid"],
                  "served_model_id": served_id, "vram": vram_note}

        # make an LLM callable by name (best effort)
        register = args.get("register", kind == "llm")
        if register and kind == "llm":
            admin_base, key = _litellm(ctx)
            if key:
                # An explicit alias (e.g. the catalog preset's, via model.use) wins
                # over the slugged server name so the registered alias, the stored
                # state, and the alias we tell the caller to use all agree.
                alias = args.get("alias") or name
                ok, detail = await S.litellm_register(admin_base, key, alias, base_url, served_id)
                if ok:
                    entry["litellm_alias"] = alias
                    entry["litellm_model_id"] = detail
                    result["litellm_alias"] = alias
                    result["call_hint"] = f"callable as model='{alias}' via agent.spawn / llm.call"
                else:
                    result["register_failed"] = detail
                    result["call_hint"] = (f"not registered with LiteLLM ({detail}); reachable "
                                           f"directly at {base_url}/v1 — add a static alias to call by name")
            else:
                result["call_hint"] = (f"LITELLM_MASTER_KEY unset; reachable directly at "
                                       f"{base_url}/v1 but not registered as an alias")

        # wire RAG to a fresh embedder/reranker for the rest of the session
        if args.get("wire_rag") and kind in ("embedding", "rerank"):
            rag = ctx.config.setdefault("tools", {}).setdefault("rag", {})
            key_name = "embed_url" if kind == "embedding" else "rerank_url"
            rag[key_name] = base_url + "/v1/embeddings" if kind == "embedding" \
                else base_url + "/v1/rerank"
            result["wired_rag"] = {key_name: rag[key_name],
                                   "note": "runtime change only — set it in config to persist"}

        S.write_server(state_dir, entry)
        return ToolResult(status="ok", result=result)


class ServeStop(Tool):
    name = "serve.stop"
    description = ("Stop a running model server by name and free its VRAM. Also "
                   "deregisters its LiteLLM alias if one was created.")
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string"}}, "required": ["name"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        state_dir = _state_dir(ctx)
        name = S_slug(args["name"])
        entry = S.read_server(state_dir, name)
        if not entry:
            return ToolResult(status="error", result=None, error=f"no server named '{name}'")
        # stop_server blocks (SIGTERM grace loop + possible SIGKILL, up to ~6s of
        # time.sleep) — run it in a thread so the event loop stays responsive.
        # The pid-reuse identity guards live inside stop_server and are unchanged.
        was_alive = await asyncio.to_thread(S.stop_server, entry)
        if entry.get("litellm_model_id"):
            admin_base, key = _litellm(ctx)
            if key:
                await S.litellm_deregister(admin_base, key, entry["litellm_model_id"])
        S.delete_server(state_dir, name)
        return ToolResult(status="ok", result={
            "name": name, "stopped": was_alive,
            "note": "VRAM freed" if was_alive else "was not running; registry cleared"})


class ServeList(Tool):
    name = "serve.list"
    read_only = True
    description = "List model servers this orchestrator has launched, with their live state."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        servers = [_view(ctx, e) for e in S.list_servers(_state_dir(ctx))]
        return ToolResult(status="ok", result={"count": len(servers), "servers": servers})


class ServeStatus(Tool):
    name = "serve.status"
    read_only = True
    description = ("Detailed status of one server (or all if name omitted): liveness, "
                   "a live health probe, GPU, port, uptime, and how to call it.")
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string"}}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        state_dir = _state_dir(ctx)
        name = args.get("name")
        entries = ([S.read_server(state_dir, S_slug(name))] if name
                   else S.list_servers(state_dir))
        if name and not entries[0]:
            return ToolResult(status="error", result=None, error=f"no server named '{name}'")
        out = []
        for e in entries:
            v = _view(ctx, e)
            if v["state"] == "running":
                v["health"] = await S.health_now(e["base_url"])
            out.append(v)
        return ToolResult(status="ok", result={"servers": out})


class ServeHealth(Tool):
    name = "serve.health"
    read_only = True
    description = "Probe a server's /health endpoint right now."
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string"}}, "required": ["name"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        entry = S.read_server(_state_dir(ctx), S_slug(args["name"]))
        if not entry:
            return ToolResult(status="error", result=None, error=f"no server named '{args['name']}'")
        h = await S.health_now(entry["base_url"])
        return ToolResult(status="ok", result={"name": entry["name"],
                                               "base_url": entry["base_url"], **h})


def S_slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(s)).strip("-")[:40] or "server"
