"""model.* — a curated preset catalog the brain can route among and load on demand.

Complements serve.* with a *policy* layer. Two loading modes, chosen per preset:

  * STATIC-PORT (preset has a `port`): the model is served onto that fixed port and
    reachability comes from a matching static `litellm.yaml` entry — no runtime
    /model/new call. This is what makes model.use work on a STATELESS LiteLLM proxy
    (no DB). model.use health-probes the port first (so it also sees systemd-served
    models) and uses `served_id` to catch a wrong-model-on-the-slot conflict.
  * DYNAMIC (no `port`): serve on a free port and register the alias at runtime —
    only works if the proxy allows /model/new (DB-backed LiteLLM).

Loading is semi-deliberate: model.use never evicts a running model unless you pass
swap:true (and even then only for serve-managed models, never a systemd unit).
"""

from __future__ import annotations

from runtime import serving as S
from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.serve.lifecycle import ServeStart, _cfg, _state_dir, S_slug


def _catalog(ctx: ToolContext) -> dict:
    return (ctx.config.get("models") or {})


def _brain_alias(ctx: ToolContext) -> str:
    p = (_catalog(ctx).get("presets") or {}).get("brain") or {}
    return (p.get("alias") or (ctx.config.get("orchestrator") or {}).get("model")
            or "local-orchestrator")


def _live_servers(ctx: ToolContext) -> list[dict]:
    return [s for s in S.list_servers(_state_dir(ctx)) if S.pid_alive(s.get("pid"))]


def _gpus(ctx: ToolContext) -> list[str]:
    return [str(g) for g in (_catalog(ctx).get("gpus") or ["0", "1"])]


def _served_matches(mid: str | None, p: dict) -> bool:
    """Is the model currently on the port the one this preset expects?"""
    sid = (p.get("served_id") or "").lower()
    if not sid:
        return True                       # no id to compare — trust the static mapping
    m = (mid or "").lower()
    return bool(m) and (sid in m or m in sid)


def _stop_on_port(ctx: ToolContext, port: int) -> bool:
    """Stop a serve.start-MANAGED server occupying `port`. Returns False if the
    occupant isn't managed by serve (e.g. a systemd unit) — we never touch those.
    Waits for the process to die AND for VRAM to be released before returning."""
    import asyncio, time
    for s in _live_servers(ctx):
        if int(s.get("port") or 0) == int(port):
            gpu = str(s.get("gpu", "1"))
            # snapshot VRAM before stopping so we know when it's freed
            free_before = S.gpu_free_gib(ctx, gpu)
            S.stop_server(s)
            S.delete_server(_state_dir(ctx), s.get("name"))
            # Wait for the port to stop responding (process fully gone)
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    import socket
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.5)
                    sock.connect(("127.0.0.1", int(port)))
                    sock.close()
                    time.sleep(0.5)  # port still open, keep waiting
                except (ConnectionRefusedError, OSError):
                    break  # port is closed — process is gone
            # Wait for VRAM to be released by the GPU driver
            if free_before is not None:
                vram_deadline = time.time() + 8
                while time.time() < vram_deadline:
                    free_now = S.gpu_free_gib(ctx, gpu)
                    if free_now is not None and free_now > free_before + 1.0:
                        break  # VRAM freed (at least 1 GiB more than before)
                    time.sleep(0.5)
            else:
                time.sleep(2)  # fallback: blind wait if we can't read VRAM
            return True
    return False


class ModelList(Tool):
    name = "model.list"
    description = (
        "Show the model preset catalog and what's live on each port/GPU. Use it to "
        "decide which model to route a task to and to see free VRAM before loading "
        "with model.use. Read-only."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cat = _catalog(ctx)
        presets = cat.get("presets") or {}
        host = _cfg(ctx).get("host", "127.0.0.1")
        rows = []
        for name, p in presets.items():
            port = p.get("port")
            live, mid = False, None
            if port:
                mid = await S.query_model_id(f"http://{host}:{port}")   # 5s-bounded probe
                live = mid is not None
            rows.append({
                "preset": name, "role": p.get("role"), "alias": p.get("alias"),
                "gpu": p.get("gpu"), "port": port, "vram_gib": p.get("vram_gib"),
                "live": live, "serving": mid,
                "matches": _served_matches(mid, p) if live else None})
        gpus = []
        for g in _gpus(ctx):
            gpus.append({"gpu": g, "free_gib": S.gpu_free_gib(ctx, g),
                         "presets_here": [r["preset"] for r in rows if str(r["gpu"]) == g]})
        return ToolResult(status="ok", tool_name=self.name, result={
            "posture": cat.get("default_posture"), "presets": rows, "gpus": gpus})


class ModelUse(Tool):
    name = "model.use"
    description = (
        "Ensure a catalog preset is served and return the LiteLLM alias to spawn on "
        "(agent.spawn(model=alias) / code.delegate). If it's already live on its port, "
        "returns immediately. Otherwise it serves the model on the preset's fixed port "
        "(reachable via the matching static litellm.yaml alias — no dynamic "
        "registration needed). If a DIFFERENT model occupies that port/slot it reports "
        "the conflict rather than evicting; pass swap:true to stop a serve-managed "
        "occupant first (it will never stop a systemd unit). Loading a 35B model takes "
        "tens of seconds — prefer already-live models."
    )
    parameters = {
        "type": "object",
        "properties": {
            "preset": {"type": "string", "description": "Catalog preset name (see model.list)."},
            "swap": {"type": "boolean",
                     "description": "If the target port runs a different (serve-managed) model, "
                                    "stop it first. Default false (report instead)."},
        },
        "required": ["preset"],
    }
    requires_confirmation = True

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        cat = _catalog(ctx)
        presets = cat.get("presets") or {}
        name = (args.get("preset") or "").strip()
        p = presets.get(name)
        if not p:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(f"unknown preset '{name}'. Available: " + ", ".join(presets))
                                    if presets else "the model catalog is empty")
        alias = p.get("alias")
        cfg = _cfg(ctx)
        host = cfg.get("host", "127.0.0.1")
        port = p.get("port")

        if not port:
            return await self._dynamic(ctx, name, p, alias, cfg)

        # ---- STATIC-PORT MODE ----
        base = f"http://{host}:{port}"
        mid = await S.query_model_id(base)                 # None if nothing live there
        if mid is not None:
            if _served_matches(mid, p):
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": f"already serving on :{port}",
                    "gpu": p.get("gpu"), "port": port, "served_model_id": mid})
            # a different model holds this slot
            if args.get("swap") and _stop_on_port(ctx, port):
                pass                                        # freed it; fall through to serve
            else:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "slot busy — different model", "port": port,
                    "serving": mid,
                    "hint": f"port {port} is serving '{mid}', not '{name}'. Stop it "
                            f"(serve.stop, or `systemctl stop` if it's a systemd unit) — or "
                            f"pass swap:true for a serve-managed one — then retry model.use('{name}')."})

        # nothing live on the port → serve it there, no dynamic registration
        gpu = str(p.get("gpu") or cfg.get("default_gpu", "1"))
        need = float(p.get("vram_gib") or 0)
        free = S.gpu_free_gib(ctx, gpu)
        floor = float(cfg.get("min_free_vram_gib", 1.0))
        if free is not None and need and free < need + floor:
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": alias, "status": "not enough VRAM", "gpu": gpu, "free_gib": free,
                "hint": f"GPU {gpu} has ~{free:g} GiB free but '{name}' needs ~{need:g} GiB — "
                        "free it (stop the other model on this card) then retry."})
        res = await ServeStart().execute({
            "name": S_slug(name), "preset": p.get("preset"), "gpu": gpu, "port": port,
            "kind": "llm", "register": False, "est_vram_gib": need}, ctx)   # static alias owns it
        if res.status != "ok":
            return ToolResult(status="error", result=res.result, tool_name=self.name,
                              error=res.error or f"failed to serve '{name}' on :{port}")
        r = res.result or {}
        note = f"reachable via LiteLLM alias '{alias}' (static :{port} mapping)"
        if r.get("note"):
            note += " — " + r["note"]
        return ToolResult(status="ok", tool_name=self.name, result={
            "alias": alias, "status": r.get("state", "loaded"),
            "gpu": gpu, "port": port, "note": note})

    async def _dynamic(self, ctx, name, p, alias, cfg):
        """No fixed port → serve on a free port and register at runtime (needs a
        DB-backed LiteLLM that accepts /model/new)."""
        floor = float(cfg.get("min_free_vram_gib", 1.0))
        need = float(p.get("vram_gib") or 0)
        servers = _live_servers(ctx)
        for s in servers:
            if s.get("litellm_alias") == alias:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "already loaded",
                    "gpu": s.get("gpu"), "port": s.get("port")})
        target = None
        for g in _gpus(ctx):
            fr = S.gpu_free_gib(ctx, g)
            if fr is None or fr >= need + floor:
                target = g
                break
        if target is None:
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": alias, "status": "needs a free GPU",
                "hint": f"no GPU has ~{need:g} GiB free for '{name}'; free one then retry."})
        res = await ServeStart().execute({
            "name": S_slug(name), "preset": p.get("preset"), "gpu": target,
            "kind": "llm", "register": True, "alias": alias, "est_vram_gib": need}, ctx)
        if res.status != "ok":
            return ToolResult(status="error", result=res.result, tool_name=self.name,
                              error=res.error or f"failed to load '{name}'")
        r = res.result or {}
        if not r.get("litellm_alias"):
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": None, "status": "served but NOT registered", "gpu": target,
                "port": r.get("port"),
                "hint": "the proxy rejected the runtime alias add (stateless LiteLLM?) — give "
                        f"'{name}' a fixed `port` in the catalog + a matching litellm.yaml entry."})
        return ToolResult(status="ok", tool_name=self.name, result={
            "alias": alias, "status": r.get("state", "loaded"),
            "gpu": target, "port": r.get("port")})
