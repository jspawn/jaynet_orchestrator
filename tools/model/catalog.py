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
    """Is the model currently on the port the one this preset expects?

    Checks served_id against what the server reports. Uses substring matching
    AND token overlap (splitting on hyphens/underscores) to handle the common
    case where served_id is a short alias like 'qwen3-30b-a3b' but the server
    reports the full GGUF filename.
    """
    sid = (p.get("served_id") or "").lower().strip()
    if not sid:
        return True                       # no id to compare — trust the static mapping
    m = (mid or "").lower().strip()
    if not m:
        return False
    # Direct substring match (either direction)
    if sid in m or m in sid:
        return True
    # Token overlap: split both on common separators and check if the key
    # tokens of the served_id appear in the model report
    import re
    sid_tokens = set(re.split(r'[-_./]', sid))
    mid_tokens = set(re.split(r'[-_./]', m))
    # Remove trivially common tokens
    noise = {"gguf", "q4", "q5", "q6", "q8", "f16", "bf16", "fp16", "k", "m", "s", "xs", ""}
    sig_sid = sid_tokens - noise
    sig_mid = mid_tokens - noise
    if sig_sid and sig_sid.issubset(sig_mid):
        return True
    # Check if at least 2/3 of significant sid tokens appear in mid
    if sig_sid and len(sig_sid & sig_mid) >= max(1, len(sig_sid) * 2 // 3):
        return True
    return False


def _port_open(port: int) -> bool:
    """True if something still answers a TCP connect on 127.0.0.1:`port`.
    Blocking (short timeout) — callers in async code run it via asyncio.to_thread."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.connect(("127.0.0.1", int(port)))
        sock.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


async def _stop_on_port(ctx: ToolContext, port: int) -> bool:
    """Stop a serve.start-MANAGED server occupying `port`. Returns False if the
    occupant isn't managed by serve (e.g. a systemd unit) — we never touch those.
    Waits for the process to die AND for VRAM to be released before returning.
    The blocking probes/waits (stop_server's kill grace loop, rocm-smi, the port
    connect) run in threads so this doesn't freeze the event loop."""
    import asyncio, time
    for s in _live_servers(ctx):
        if int(s.get("port") or 0) == int(port):
            gpu = str(s.get("gpu", "1"))
            # snapshot VRAM before stopping so we know when it's freed
            free_before = await asyncio.to_thread(S.gpu_free_gib, ctx, gpu)
            await asyncio.to_thread(S.stop_server, s)
            S.delete_server(_state_dir(ctx), s.get("name"))
            # Wait for the port to stop responding (process fully gone)
            deadline = time.time() + 10
            while time.time() < deadline:
                if not await asyncio.to_thread(_port_open, port):
                    break  # port is closed — process is gone
                await asyncio.sleep(0.5)  # port still open, keep waiting
            # Wait for VRAM to be released by the GPU driver
            if free_before is not None:
                vram_deadline = time.time() + 8
                while time.time() < vram_deadline:
                    free_now = await asyncio.to_thread(S.gpu_free_gib, ctx, gpu)
                    if free_now is not None and free_now > free_before + 1.0:
                        break  # VRAM freed (at least 1 GiB more than before)
                    await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(2)  # fallback: blind wait if we can't read VRAM
            return True
    return False


class ModelList(Tool):
    name = "model.list"
    read_only = True
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
        # Probe each unique port once
        port_probes: dict[int, str | None] = {}
        # Count how many presets share each port
        port_preset_count: dict[int, int] = {}
        for name, p in presets.items():
            port = p.get("port")
            if port:
                port_preset_count[int(port)] = port_preset_count.get(int(port), 0) + 1
        rows = []
        for name, p in presets.items():
            port = p.get("port")
            mid = None
            if port:
                iport = int(port)
                if iport not in port_probes:
                    port_probes[iport] = await S.query_model_id(f"http://{host}:{iport}")
                mid = port_probes[iport]
            port_up = mid is not None
            if port_up:
                # If only one preset uses this port (e.g. brain on :8090), it's always a match
                if port_preset_count.get(int(port), 0) == 1:
                    matches = True
                else:
                    matches = _served_matches(mid, p)
            else:
                matches = False
            rows.append({
                "preset": name, "role": p.get("role"), "alias": p.get("alias"),
                "gpu": p.get("gpu"), "port": port, "vram_gib": p.get("vram_gib"),
                "live": matches,           # only True if THIS preset's model is actually served
                "port_up": mid is not None, # port responds (some model is there)
                "serving": mid,             # what model ID the port reports
                "matches": matches})
        # Summary: which model is actually on each slot
        slots = {}
        for r in rows:
            if r["live"]:
                slots[f"gpu{r['gpu']}:{r['port']}"] = r["preset"]
        gpus = []
        for g in _gpus(ctx):
            active_here = [r["preset"] for r in rows if str(r["gpu"]) == g and r["live"]]
            gpus.append({"gpu": g, "free_gib": S.gpu_free_gib(ctx, g),
                         "presets_here": [r["preset"] for r in rows if str(r["gpu"]) == g],
                         "active": active_here[0] if active_here else None})
        return ToolResult(status="ok", tool_name=self.name, result={
            "posture": cat.get("default_posture"), "presets": rows, "gpus": gpus,
            "active_slots": slots})


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
            if args.get("swap") and await _stop_on_port(ctx, port):
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
            # litellm_alias is what serve.start registered (and reported) — it
            # matches the preset's `alias`, so this fast-path actually fires.
            if alias and s.get("litellm_alias") == alias:
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
