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

import time

from runtime import serving as S
from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.serve.lifecycle import S_slug, ServeStart, _cfg, _state_dir


def _catalog(ctx: ToolContext) -> dict:
    return (ctx.config.get("models") or {})


# ---- strength tags: registry + routing -------------------------------------------

def strength_registry(config: dict) -> dict:
    """The meaning of each preset strength tag (models.strengths in
    runtime.yaml): {tag: one-line description}. Tags on presets stay
    free-form — registered tags are the ones the brain sees described in the
    system prompt and delegation routes by. 'allround' is the built-in
    catch-all and needs no entry."""
    raw = ((config.get("models") or {}).get("strengths") or {})
    return {str(k): str(v) for k, v in raw.items() if str(k).strip()}


# Specialist slots probed, in priority order, when routing by strength.
_ROUTING_SLOTS = ("specialist", "specialist2", "specialist3")


def tagged_presets(config: dict, wanted: str) -> list[dict]:
    """Presets carrying a strength tag (exact or 'allround'): [{preset, alias,
    strengths}] — live or not. The honest-error companion to route_strength:
    lets a caller say 'dolphin IS tagged security, but stopped'."""
    presets = ((config.get("models") or {}).get("presets") or {})
    out = []
    for name, p in presets.items():
        strengths = list(p.get("strengths") or [])
        if wanted in strengths or "allround" in strengths:
            out.append({"preset": name, "alias": p.get("alias"),
                        "strengths": strengths})
    return out


async def route_strength(config: dict, wanted: str) -> str | None:
    """Alias of a LIVE specialist slot whose preset advertises the wanted
    strength tag — the harness's model-priority rule: work goes to the model
    strong at it, not the default brain. An exact strength tag beats an
    'allround' catch-all; an earlier slot wins ties. None when nothing
    matching is live (callers fall back honestly)."""
    allround = None
    for slot_name in _ROUTING_SLOTS:
        try:
            slot = await live_slot(config, slot=slot_name)
        except Exception:
            slot = None
        if not slot or not slot.get("alias"):
            continue
        strengths = slot.get("strengths") or []
        if wanted in strengths:
            return slot["alias"]
        if "allround" in strengths and allround is None:
            allround = slot["alias"]
    return allround


async def route_strength_exact(config: dict, wanted: str) -> str | None:
    """Alias of a LIVE specialist slot whose preset carries exactly `wanted`
    — no 'allround' fallback. strength_route decides when the fallback
    applies; this is the post-swap confirmation probe."""
    for slot_name in _ROUTING_SLOTS:
        try:
            slot = await live_slot(config, slot=slot_name)
        except Exception:
            slot = None
        if slot and slot.get("alias") and wanted in (slot.get("strengths") or []):
            return slot["alias"]
    return None


async def strength_route(config: dict, wanted: str) -> dict:
    """The full routing plan for strength-tagged work: {"mode", "alias",
    "preset"?} or {} when nothing routes at all. Modes: 'live' (an exact
    tag holder is serving), 'swap' (a LOCAL preset carries the tag but is
    stopped — the caller swaps it onto its slot via model.use rather than
    settling for a weaker model), 'allround' (no preset carries the tag;
    the allround specialist takes it). Remote presets are never swap
    candidates — JayNet doesn't launch off-box servers."""
    exact = await route_strength_exact(config, wanted)
    if exact:
        return {"mode": "live", "alias": exact}
    presets = ((config.get("models") or {}).get("presets") or {})
    for t in tagged_presets(config, wanted):
        if wanted not in (t.get("strengths") or []) or not t.get("alias"):
            continue
        p = presets.get(t["preset"]) or {}
        if not (p.get("remote_host") or "").strip():
            return {"mode": "swap", "alias": t["alias"], "preset": t["preset"]}
    allround = await route_strength(config, wanted)
    if allround:
        return {"mode": "allround", "alias": allround}
    return {}


def invalidate_live_slots() -> None:
    """Drop the live_slot probe cache. Call after starting or stopping a
    server (model.use, swaps) so strength routing sees the new reality now,
    not whenever the 120s TTL happens to expire."""
    _live_slot_cache.clear()


def _brain_alias(ctx: ToolContext) -> str:
    from runtime.preset_store import resolve_slot
    p = resolve_slot(ctx.config, "brain")
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


def _probe_base(p: dict, default_host: str = "127.0.0.1") -> str:
    """Base URL (no /v1) to probe for a preset: its remote endpoint when
    adopted (llama-server/vLLM/Ollama off-box), else loopback:port."""
    from runtime.preset_store import remote_base
    if (p.get("remote_host") or "").strip():
        return remote_base(p)
    return f"http://{default_host}:{p.get('port') or 8080}"


def _match_served(mids: list[str] | None, p: dict) -> str | None:
    """The served id on an endpoint matching this preset (None if no match).
    Scans ALL reported ids — vLLM/Ollama can list several models per server."""
    return next((m for m in (mids or []) if _served_matches(m, p)), None)


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


# live_slot probe cache: key ("slot:x" / "gpu:x") -> (monotonic ts, result).
# Shared by the loop's prompt injection and code.delegate so back-to-back runs
# don't re-probe.
_LIVE_SLOT_TTL_S = 120.0
_live_slot_cache: dict[str, tuple[float, dict | None]] = {}


async def live_slot(config: dict, gpu: str | None = None,
                    slot: str = "specialist") -> dict | None:
    """Which catalog preset is actually live on a slot right now.

    Default (gpu=None): resolve the slot's assigned preset and probe every
    preset sharing its PORT — placement-independent, so the specialist is
    found whether it lives on GPU 0, GPU 3, a split, or CPU. With an explicit
    `gpu`, probe the presets occupying that card instead (a preset on "0,1"
    counts as present on both). Returns {preset, serving, strengths, alias},
    or None when the port is down or the served model matches no preset.
    TTL-cached (~120s), cheap, and never raises — a probe failure is just None.
    """
    key = f"gpu:{gpu}" if gpu is not None else f"slot:{slot}"
    hit = _live_slot_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _LIVE_SLOT_TTL_S:
        return hit[1]
    result = None
    try:
        from runtime.preset_store import gpu_list, remote_key, resolve_slot
        presets = ((config.get("models") or {}).get("presets") or {})
        if gpu is not None:
            cands = [(name, p) for name, p in presets.items()
                     if str(gpu) in gpu_list(p)
                     and (p.get("port") or p.get("remote_host"))]
        else:
            slotp = resolve_slot(config, slot)
            port = slotp.get("port")
            if port:
                cands = [(name, p) for name, p in presets.items()
                         if p.get("port") and int(p["port"]) == int(port)]
            elif (slotp.get("remote_host") or "").strip():
                # remote slot whose port lives in the endpoint URL (or scheme
                # default) — group by the endpoint itself
                base = _probe_base(slotp)
                cands = [(name, p) for name, p in presets.items()
                         if (p.get("remote_host") or "").strip()
                         and _probe_base(p) == base]
            else:   # slot unset/unported — legacy fallback: last card
                gpus = [str(g) for g in
                        ((config.get("models") or {}).get("gpus") or ["0", "1"])]
                fallback = gpus[-1] if gpus else "1"
                cands = [(name, p) for name, p in presets.items()
                         if fallback in gpu_list(p) and p.get("port")]
        for base in sorted({_probe_base(p) for _, p in cands}):
            # first preset on this endpoint that HAS a key wins — a keyless
            # neighbour must not shadow the keyed one (endpoint would 401 →
            # look dead); same or-accumulation shape as the model.list path
            api_key = next((k for _, p in cands
                            if _probe_base(p) == base
                            and (k := remote_key(p))), None)
            mids = await S.query_model_ids(base, api_key=api_key)
            if not mids:
                continue
            for name, p in cands:
                if _probe_base(p) != base:
                    continue
                hit = _match_served(mids, p)
                if hit:
                    result = {"preset": name, "serving": hit,
                              "strengths": list(p.get("strengths") or []),
                              "alias": p.get("alias")}
                    break
            if result:
                break
    except Exception:
        result = None
    _live_slot_cache[key] = (now, result)
    return result


async def _wait_freed(ctx: ToolContext, port: int, gpu: str,
                      free_before: float | None) -> None:
    """After a stop: wait for the port to close, then for the driver to
    release the VRAM (a follow-up load on a half-freed card OOMs). Bounded;
    the probes run in threads so the event loop stays responsive."""
    import asyncio
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        if not await asyncio.to_thread(_port_open, port):
            break  # port is closed — process is gone
        await asyncio.sleep(0.5)  # port still open, keep waiting
    if free_before is not None:
        vram_deadline = time.time() + 8
        while time.time() < vram_deadline:
            free_now = await asyncio.to_thread(S.gpu_free_gib, ctx, gpu)
            if free_now is not None and free_now > free_before + 1.0:
                break  # VRAM freed (at least 1 GiB more than before)
            await asyncio.sleep(0.5)
    else:
        await asyncio.sleep(2)  # fallback: blind wait if we can't read VRAM


async def _stop_on_port(ctx: ToolContext, port: int) -> bool:
    """Stop a serve.start-MANAGED server occupying `port`. Returns False if the
    occupant isn't managed by serve (e.g. a systemd unit) — we never touch those.
    Waits for the process to die AND for VRAM to be released before returning.
    The blocking probes/waits (stop_server's kill grace loop, rocm-smi, the port
    connect) run in threads so this doesn't freeze the event loop."""
    import asyncio
    for s in _live_servers(ctx):
        if int(s.get("port") or 0) == int(port):
            gpu = str(s.get("gpu", "1"))
            # snapshot VRAM before stopping so we know when it's freed
            free_before = await asyncio.to_thread(S.gpu_free_gib, ctx, gpu)
            await asyncio.to_thread(S.stop_server, s)
            S.delete_server(_state_dir(ctx), s.get("name"))
            await _wait_freed(ctx, port, gpu, free_before)
            return True
    return False


async def _stop_managed_slot(ctx: ToolContext, port: int) -> bool:
    """Stop a boot-posture (process_manager) server whose SLOT resolves to
    `port` — the Processes-tab servers (brain/specialist/…). Goes through the
    manager: stop_one marks it intentionally stopped, so the run loop's
    auto-restart won't resurrect it mid-swap to fight the incoming model for
    the port (live evidence: the specialist kept qwen3.8 up through every
    security delegate because serve's registry didn't know it). False when no
    manager is wired (CLI/tests) or no managed slot holds the port."""
    import asyncio

    from runtime import process_manager as pm_mod
    pm = pm_mod.CURRENT
    if pm is None:
        return False
    from runtime.preset_store import resolve_slot
    for name in pm.names():
        try:
            p = resolve_slot(ctx.config, name)
        except Exception:
            continue
        if not p or int(p.get("port") or 0) != int(port):
            continue
        gpu = str(p.get("gpu", "1"))
        free_before = await asyncio.to_thread(S.gpu_free_gib, ctx, gpu)
        await pm.stop_one(name)
        await _wait_freed(ctx, port, gpu, free_before)
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

        # Probe each unique endpoint once (remote presets live off-box)
        from runtime.preset_store import remote_key
        probes: dict[str, list[str] | None] = {}
        ep_count: dict[str, int] = {}
        ep_keys: dict[str, str | None] = {}
        for name, p in presets.items():
            if p.get("port") or (p.get("remote_host") or "").strip():
                ep = _probe_base(p, host)
                ep_count[ep] = ep_count.get(ep, 0) + 1
                ep_keys[ep] = ep_keys.get(ep) or remote_key(p)
        rows = []
        for name, p in presets.items():
            port = p.get("port")
            ep = None
            if p.get("port") or (p.get("remote_host") or "").strip():
                ep = _probe_base(p, host)
                if ep not in probes:
                    try:
                        probes[ep] = await S.query_model_ids(
                            ep, api_key=ep_keys.get(ep))
                    except S.EndpointAuth:
                        probes[ep] = "auth"      # no key set, or key rejected
            auth = probes.get(ep) == "auth" if ep else False
            mids = None if auth else (probes.get(ep) if ep else None)
            port_up = auth or mids is not None
            mid = mids[0] if mids else None
            if mids:
                # A single-preset, single-model endpoint (llama-server style)
                # is always a match; multi-model servers (vLLM/Ollama) must
                # match the preset's served_id against the list.
                if ep_count.get(ep, 0) == 1 and len(mids) == 1:
                    matches = True
                else:
                    hit = _match_served(mids, p)
                    matches = hit is not None
                    mid = hit or mid
            else:
                matches = False
            rows.append({
                "preset": name, "role": p.get("role"), "alias": p.get("alias"),
                "gpu": p.get("gpu"), "port": port, "vram_gib": p.get("vram_gib"),
                "remote_host": (p.get("remote_host") or "").strip(),
                "strengths": list(p.get("strengths") or []),
                "live": matches,           # only True if THIS preset's model is actually served
                "port_up": port_up,         # endpoint responds (some model is there)
                "serving": ("(API key rejected — check api_key_env)"
                            if auth and ep_keys.get(ep)
                            else "(requires an API key)" if auth else mid),
                "matches": matches})
        # Summary: which model is actually on each slot
        slots = {}
        for r in rows:
            if r["live"]:
                slots[f"gpu{r['gpu']}:{r['port']}"] = r["preset"]
        gpus = []
        from runtime.preset_store import gpu_list
        for g in _gpus(ctx):
            active_here = [r["preset"] for r in rows if g in gpu_list(r) and r["live"]]
            gpus.append({"gpu": g, "free_gib": S.gpu_free_gib(ctx, g),
                         "presets_here": [r["preset"] for r in rows if g in gpu_list(r)],
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
        "or boot-posture (Processes-tab) occupant first — the latter goes through "
        "the process manager so auto-restart stays off (it will never stop a "
        "systemd unit). Remote presets "
        "(remote_host set — an off-box server like llama-server, vLLM or Ollama) "
        "are only health-probed, never launched "
        "or stopped. Loading a 35B model takes "
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

        # ---- REMOTE MODE ----
        # A remote preset is an adopted off-box server (llama-server, vLLM,
        # Ollama, …): JayNet only probes it — it never launches, swaps, or
        # stops anything off-box.
        remote = (p.get("remote_host") or "").strip()
        if remote:
            from runtime.preset_store import BACKEND_LABELS
            label = BACKEND_LABELS.get(p.get("backend") or "", "llama-server")
            base = _probe_base(p)
            if not port and "://" not in remote:
                return ToolResult(status="error", result=None, tool_name=self.name,
                                  error=(f"remote preset '{name}' has no port — set the port "
                                         f"{label} listens on at {remote} (admin → Presets)"))
            try:
                from runtime.preset_store import remote_key
                mids = await S.query_model_ids(base, api_key=remote_key(p))
            except S.EndpointAuth:
                keyed = bool((p.get("api_key_env") or "").strip())
                hint = (f"{base} rejected the key from ${p['api_key_env']} — "
                        f"check the value in the env file and retry."
                        if keyed else
                        f"{base} requires an API key — set the preset's "
                        f"api_key_env field to the env var holding it "
                        f"(admin → Presets), add the key to the env file, and "
                        f"retry.")
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "authentication required",
                    "remote": base, "hint": hint})
            if mids is None:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "unreachable", "remote": base,
                    "hint": f"'{name}' is a remote preset — nothing answers at {base}. Start "
                            f"{label} there (reachable from this box, LAN-only) and retry; "
                            f"JayNet never launches remote models itself."})
            hit = _match_served(mids, p)
            if hit is None:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "slot busy — different model",
                    "remote": base, "serving": ", ".join(mids[:4]),
                    "hint": f"{base} is serving {', '.join(mids[:4]) or 'nothing'}, not "
                            f"'{name}'. Fix that on {remote} — JayNet never stops "
                            f"remote servers."})
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": alias, "status": f"already serving on {base}",
                "remote": base, "port": port, "served_model_id": hit})

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
            stopped = False
            if args.get("swap"):
                stopped = await _stop_on_port(ctx, port)
                if not stopped:
                    # boot-posture (Processes-tab) servers live in a different
                    # registry — stop them through the process manager so
                    # auto-restart doesn't resurrect them mid-swap.
                    stopped = await _stop_managed_slot(ctx, port)
                if stopped:
                    invalidate_live_slots()             # occupant is gone
            if stopped:
                pass                                        # freed it; fall through to serve
            else:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "slot busy — different model", "port": port,
                    "serving": mid,
                    "hint": f"port {port} is serving '{mid}', not '{name}'. Stop it "
                            f"(serve.stop, or `systemctl stop` if it's a systemd unit) — or "
                            f"pass swap:true for a serve-managed one — then retry model.use('{name}')."})

        # nothing live on the port → serve it there, no dynamic registration
        # device: "" means CPU (explicit) — only an UNSET gpu falls back to default
        gpu = p.get("gpu")
        gpu = str(cfg.get("default_gpu", "1")) if gpu is None else str(gpu)
        need = float(p.get("vram_gib") or 0)
        free = S.gpu_free_gib(ctx, gpu) if gpu else None   # CPU: no VRAM check
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
        invalidate_live_slots()                     # new model answering now
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
        invalidate_live_slots()                     # new model answering now
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
