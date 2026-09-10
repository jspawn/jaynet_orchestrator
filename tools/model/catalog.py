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
swap:true — and then it frees everything the incoming preset needs (its port AND
every pinned GPU, including multi-card occupants like a brain on "0,1"), stopping
serve-managed servers and boot-posture slots (via the process manager, so
auto-restart stays disarmed) but never a systemd unit or a remote box. The result's
`evicted` list records what was stopped; code.delegate passes include_brain and
restores the evicted set after the child run (models.swap_back).
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


def _serve_binary(ctx: ToolContext, p: dict) -> str:
    """Resolved llama-server binary path for a preset ("" = launcher default).
    model.use must pass it explicitly: ServeStart launches via start-model.sh
    --preset (FILE mode), which never consults the preset DB's binary
    registry — on a custom-build box (rocm/vulkan binary outside
    $JAYNET_HOME/bin) the launch dies with 'llama-server not found'
    (live: the first real dolphin swap)."""
    try:
        from runtime.preset_store import PresetStore, db_path_for
        path, _ = PresetStore(db_path_for(ctx.config)).binary_for(p)
        return path or ""
    except Exception:
        return ""


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


async def _wait_freed(ctx: ToolContext, port: int, gpus,
                      free_before) -> None:
    """After a stop: wait for the port to close, then for the driver to
    release the VRAM on EVERY affected card (a follow-up load on a
    half-freed card OOMs). `gpus` is a card-id list (a single "1" is
    accepted for the old callers); `free_before` is either one float
    (first card) or a {card: float|None} snapshot. Bounded; the probes
    run in threads so the event loop stays responsive."""
    import asyncio
    import time
    if isinstance(gpus, str):
        gpus = [gpus]
    if not isinstance(free_before, dict):
        free_before = {gpus[0]: free_before} if gpus else {}
    deadline = time.time() + 10
    while time.time() < deadline:
        if not await asyncio.to_thread(_port_open, port):
            break  # port is closed — process is gone
        await asyncio.sleep(0.5)  # port still open, keep waiting
    pending = {g: fb for g, fb in free_before.items() if fb is not None}
    if pending:
        vram_deadline = time.time() + 8
        while time.time() < vram_deadline and pending:
            free_now = await asyncio.to_thread(S.gpus_free_gib, ctx, list(pending))
            pending = {g: fb for g, fb in pending.items()
                       if not (free_now.get(g) is not None
                               and free_now[g] > fb + 1.0)}
            if pending:
                await asyncio.sleep(0.5)
    else:
        await asyncio.sleep(2)  # fallback: blind wait if we can't read VRAM


def _server_gpus(s: dict) -> list[str]:
    from runtime.preset_store import gpu_list
    return gpu_list({"gpu": s.get("gpu")})


async def _stop_serve_record(ctx: ToolContext, rec: dict) -> bool:
    """Stop a serve.start-MANAGED server (planner record kind 'serve').
    Returns False if the occupant isn't managed by serve (e.g. a systemd
    unit) — we never touch those. Waits for the process to die AND for VRAM
    to be released on all its cards before returning."""
    import asyncio
    for s in _live_servers(ctx):
        if s.get("name") != rec.get("name"):
            continue
        gpus = _server_gpus(s) or [str(s.get("gpu", "1"))]
        free_before = await asyncio.to_thread(S.gpus_free_gib, ctx, gpus)
        stopped = await asyncio.to_thread(S.stop_server, s)
        if not stopped and S.pid_alive(s.get("pid")):
            # a LIVE occupant refused the stop (pid identity mismatch) —
            # don't delete the registry entry or pretend the port is free
            return False
        S.delete_server(_state_dir(ctx), s.get("name"))
        await _wait_freed(ctx, int(rec.get("port") or s.get("port") or 0),
                          gpus, free_before)
        return True
    return False


async def _stop_slot_record(ctx: ToolContext, rec: dict) -> bool:
    """Stop a boot-posture (process_manager) SLOT (planner record kind
    'slot') — the Processes-tab servers (brain/specialist/…). Goes through
    the manager: stop_one marks it intentionally stopped, so the run loop's
    auto-restart won't resurrect it mid-swap to fight the incoming model
    (live evidence: the specialist kept qwen3.8 up through every security
    delegate because serve's registry didn't know it). False when no
    manager is wired (CLI/tests)."""
    import asyncio

    from runtime import process_manager as pm_mod
    pm = pm_mod.CURRENT
    if pm is None:
        return False
    from runtime.preset_store import gpu_list, resolve_slot
    try:
        p = resolve_slot(ctx.config, rec["slot"])
    except Exception:
        return False
    if not p:
        return False
    gpus = gpu_list(p) or [str(p.get("gpu", "1"))]
    free_before = await asyncio.to_thread(S.gpus_free_gib, ctx, gpus)
    await pm.stop_one(rec["slot"])
    await _wait_freed(ctx, int(p.get("port") or 0), gpus, free_before)
    return True


async def plan_eviction(ctx: ToolContext, target_name: str, p: dict,
                        include_brain: bool = False) -> list[dict]:
    """What must stop so preset `p` can load: every running model touching
    ANY of the preset's pinned GPUs, plus whatever holds its port. Returns
    eviction records:
      {"kind": "serve", "name", "preset", "gpu", "port", "alias"}
      {"kind": "slot",  "slot", "preset", "port"}
    `preset` on a record is the model ACTUALLY live there (probed), so a
    restore brings back reality, not the boot default. The brain slot is
    never touched unless include_brain — evicting it kills the current
    run's model, safe only for callers that restore before the brain's
    next turn (code.delegate does)."""
    from runtime.preset_store import gpu_list, resolve_slot
    needed = set(gpu_list(p))
    port = int(p.get("port") or 0)
    records: list[dict] = []
    seen_slots: set[str] = set()
    seen_serves: set[str] = set()

    for s in _live_servers(ctx):
        hit = (port and int(s.get("port") or 0) == port) or \
              (needed and needed & set(_server_gpus(s)))
        if hit and s.get("name") not in seen_serves:
            seen_serves.add(s.get("name"))
            records.append({"kind": "serve", "name": s.get("name"),
                            "preset": s.get("model"), "gpu": s.get("gpu"),
                            "port": s.get("port"),
                            "alias": s.get("litellm_alias")})

    from runtime import process_manager as pm_mod
    pm = pm_mod.CURRENT
    if pm is not None:
        for slot in pm.names():
            if slot == "brain" and not include_brain:
                continue
            try:
                sp = resolve_slot(ctx.config, slot)
            except Exception:
                continue
            if not sp or (sp.get("remote_host") or "").strip():
                continue  # remote slots run off-box — nothing to stop here
            sport = int(sp.get("port") or 0)
            hit = (port and sport == port) or \
                  (needed and needed & set(gpu_list(sp)))
            if hit and slot not in seen_slots:
                seen_slots.add(slot)
                # What is REALLY on the slot right now (a previous swap may
                # have changed it) — the restore target.
                live = None
                try:
                    live = await live_slot(ctx.config, slot=slot)
                except Exception:
                    live = None
                records.append({"kind": "slot", "slot": slot,
                                "preset": (live or {}).get("preset"),
                                "port": sp.get("port")})
    return records


async def evict_records(ctx: ToolContext, records: list[dict]) -> tuple[list[dict], list[str]]:
    """Execute an eviction plan. Returns (stopped_records, failure_notes) —
    on any failure the caller must NOT proceed to load (a half-freed card
    OOMs the incoming model)."""
    stopped, failures = [], []
    for rec in records:
        try:
            if rec["kind"] == "serve":
                ok = await _stop_serve_record(ctx, rec)
            else:
                ok = await _stop_slot_record(ctx, rec)
        except Exception as e:
            ok, err = False, str(e)
        else:
            err = None
        if ok:
            stopped.append(rec)
        else:
            label = rec.get("slot") or rec.get("name") or "?"
            failures.append(f"could not stop {label}"
                            + (f" ({err})" if err else ""))
    if stopped:
        invalidate_live_slots()
    return stopped, failures


async def restore_evicted(ctx: ToolContext, records: list[dict]) -> list[str]:
    """Bring back what a swap evicted (reverse order: the brain returns
    first). Slot presets restore through the process manager (boot-posture
    semantics, auto-restart re-arms); serve-registry presets re-serve via
    model.use. Returns human notes — failures included, never raised; the
    caller surfaces them (a missing restore must never be silent)."""
    notes: list[str] = []
    if not records:
        return notes
    from runtime import process_manager as pm_mod
    pm = pm_mod.CURRENT
    for rec in reversed(records):
        label = rec.get("preset") or rec.get("slot") or rec.get("name") or "?"
        try:
            if rec["kind"] == "slot" and pm is not None:
                slotp = None
                try:
                    from runtime.preset_store import resolve_slot
                    slotp = resolve_slot(ctx.config, rec["slot"])
                except Exception:
                    slotp = None
                # If the slot's assigned preset is also what was live,
                # start_one is the cleanest restore (manager-supervised).
                assigned = None
                try:
                    slots = ((ctx.config.get("models") or {}).get("slots") or {})
                    assigned = slots.get(rec["slot"], rec["slot"])
                except Exception:
                    pass
                if not rec.get("preset") or rec.get("preset") == assigned:
                    ok = await pm.start_one(rec["slot"])
                    if ok and slotp and slotp.get("port"):
                        ok = await _wait_serving(ctx, slotp)
                    notes.append(f"restored {label} on slot '{rec['slot']}'"
                                 if ok else
                                 f"FAILED to restore slot '{rec['slot']}' — "
                                 f"restart it in Admin → Processes")
                else:
                    ok = await _restore_via_model_use(ctx, rec["preset"])
                    notes.append(f"restored {label} (was swapped onto "
                                 f"'{rec['slot']}')" if ok else
                                 f"FAILED to restore '{label}' — load it "
                                 f"with model.use or Admin → Processes")
            elif rec["kind"] == "serve" and rec.get("preset") \
                    and rec["preset"] != "custom":
                ok = await _restore_via_model_use(ctx, rec["preset"])
                notes.append(f"restored {label}" if ok else
                             f"FAILED to restore '{label}' — load it with model.use")
            else:
                notes.append(f"did not restore {label} (not a catalog preset) "
                             f"— restart it manually")
        except Exception as e:
            notes.append(f"FAILED to restore {label}: {e}")
    invalidate_live_slots()
    return notes


async def _restore_via_model_use(ctx: ToolContext, preset: str) -> bool:
    res = await ModelUse().execute({"preset": preset, "swap": True}, ctx)
    return res.status == "ok" and not (res.result or {}).get("hint")


async def _wait_serving(ctx: ToolContext, p: dict, wait_s: float = 120.0) -> bool:
    """Poll until the preset's own model answers on its endpoint (a big
    brain takes tens of seconds to load)."""
    import asyncio
    base = _probe_base(p)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        mids = await S.query_model_ids(base)
        if mids and _match_served(mids, p):
            return True
        await asyncio.sleep(2.0)
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
        "registration needed). If other models hold the preset's port or ANY of its "
        "pinned GPUs it reports the conflict rather than evicting; pass swap:true to "
        "stop the serve-managed or boot-posture (Processes-tab) occupants first — "
        "slots go through the process manager so auto-restart stays off (it will "
        "never stop a systemd unit). Remote presets "
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
                     "description": "Free the preset's port and pinned GPUs: stop whatever "
                                    "serve-managed/boot-posture models occupy them. Default "
                                    "false (report instead). The result's `evicted` list "
                                    "says exactly what was stopped. The brain itself is "
                                    "never evicted this way — it is this run's model."},
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
        evicted: list[dict] = []
        port_conflict = False
        if mid is not None:
            if _served_matches(mid, p):
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": f"already serving on :{port}",
                    "gpu": p.get("gpu"), "port": port, "served_model_id": mid})
            port_conflict = True

        # Occupants: whatever holds the target port PLUS every running model
        # on ANY pinned GPU (a brain spanning both cards is the real occupant
        # of GPU 1 even though it lives on another port). Brain eviction is
        # INTERNAL-ONLY: honored when the caller set ctx._allow_brain_evict
        # (code.delegate does, with swap-back) — never from a model-facing
        # argument, which would stop this run's own model with no restore.
        include_brain = bool(args.get("include_brain")) and bool(
            getattr(ctx, "_allow_brain_evict", False))
        plan = await plan_eviction(ctx, name, p, include_brain=include_brain)
        if plan:
            if not args.get("swap"):
                occupants = ", ".join(
                    r.get("slot") or r.get("name") or "?" for r in plan)
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "hardware busy", "port": port,
                    "occupants": occupants,
                    "hint": f"loading '{name}' needs its port/GPUs free — held by: "
                            f"{occupants}. Stop them (serve.stop / Admin → Processes) "
                            f"or pass swap:true to free the hardware automatically."})
            evicted, failures = await evict_records(ctx, plan)
            if failures:
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "could not free the hardware",
                    "evicted": [r.get("slot") or r.get("name") for r in evicted],
                    "hint": "; ".join(failures) +
                            " — refusing to load onto half-freed hardware."})
            if port_conflict:
                # The planned stops ran — is the port actually free now? An
                # occupant in NO registry (systemd unit, hand-started server)
                # survives every stop we can issue; never serve onto it.
                mid = await S.query_model_id(base)
                port_conflict = mid is not None and not _served_matches(mid, p)
        if port_conflict:
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": alias, "status": "slot busy — different model", "port": port,
                "serving": mid,
                "hint": f"port {port} is serving '{mid}', not '{name}', and that "
                        f"server is not managed by JayNet (a systemd unit or a "
                        f"hand-started process) — stop it yourself "
                        f"(`systemctl stop …`), then retry model.use('{name}')."})

        # nothing live in the way → serve it there, no dynamic registration
        # device: "" means CPU (explicit) — only an UNSET gpu falls back to default
        gpu = p.get("gpu")
        gpu = str(cfg.get("default_gpu", "1")) if gpu is None else str(gpu)
        cards = [g for g in str(gpu).split(",") if g]
        need = float(p.get("vram_gib") or 0)
        import asyncio
        free_map = await asyncio.to_thread(S.gpus_free_gib, ctx, cards) \
            if cards else {}
        floor = float(cfg.get("min_free_vram_gib", 1.0))
        known = {g: f for g, f in free_map.items() if f is not None}
        if need and known and len(known) == len(cards) \
                and sum(known.values()) < need + floor:
            return ToolResult(status="ok", tool_name=self.name, result={
                "alias": alias, "status": "not enough VRAM", "gpu": gpu,
                "free_gib": known,
                "hint": f"GPUs {gpu} have ~{sum(known.values()):g} GiB free combined "
                        f"but '{name}' needs ~{need:g} GiB — free them (or retry with "
                        f"swap:true to evict the occupants) then retry."})
        serve_args = {
            "name": S_slug(name), "preset": p.get("preset"), "gpu": gpu, "port": port,
            "kind": "llm", "register": False, "est_vram_gib": need}
        _bin = _serve_binary(ctx, p)
        if _bin:
            serve_args["llama_bin"] = _bin
        res = await ServeStart().execute(serve_args, ctx)   # static alias owns it
        if res.status != "ok":
            return ToolResult(status="error", result=res.result, tool_name=self.name,
                              error=res.error or f"failed to serve '{name}' on :{port}")
        invalidate_live_slots()                     # new model answering now
        r = res.result or {}
        note = f"reachable via LiteLLM alias '{alias}' (static :{port} mapping)"
        if evicted:
            names = ", ".join(
                str(x.get("preset") or x.get("slot") or x.get("name"))
                for x in evicted)
            note += f" — evicted to free the hardware: {names}"
        if r.get("note"):
            note += " — " + r["note"]
        return ToolResult(status="ok", tool_name=self.name, result={
            "alias": alias, "status": r.get("state", "loaded"),
            "gpu": gpu, "port": port, "note": note,
            "evicted": evicted})

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
        serve_args = {
            "name": S_slug(name), "preset": p.get("preset"), "gpu": target,
            "kind": "llm", "register": True, "alias": alias, "est_vram_gib": need}
        _bin = _serve_binary(ctx, p)
        if _bin:
            serve_args["llama_bin"] = _bin
        res = await ServeStart().execute(serve_args, ctx)
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
