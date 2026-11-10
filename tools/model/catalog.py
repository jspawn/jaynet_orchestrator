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
every pinned GPU that can't fit it ALONGSIDE — per-card measured/estimated shares
vs free VRAM, see need_shares; a co-tenant the newcomer fits next to is left
running), stopping
serve-managed servers and boot-posture slots (via the process manager, so
auto-restart stays disarmed) but never a systemd unit or a remote box. The result's
`evicted` list records what was stopped; specialist.delegate passes include_brain and
restores the evicted set after the child run (models.swap_back).
"""

from __future__ import annotations

import time

from runtime import serving as S
from runtime.preset_store import internal_preset_alias, route_target
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
    # v falsy = tombstone: the admin config editor's reset writes the dotpath
    # back as None (key stays, value cleared) — a cleared custom tag must not
    # linger in the brain's directory as a "None" description.
    return {str(k): str(v) for k, v in raw.items() if str(k).strip() and v}


# Specialist slots probed when routing by strength. Catalog order, not this
# tuple, decides priority; the slots only tell us which presets are live.
_ROUTING_SLOTS = ("specialist", "specialist2", "specialist3")


def preferred_brain(config: dict) -> dict:
    """First allowed, active brain preset in catalog priority order."""
    from runtime.preset_store import preset_allowed
    presets = ((config.get("models") or {}).get("presets") or {})
    for name, p in presets.items():
        if p.get("alias") == "local-orchestrator" and not p.get("archived") \
                and preset_allowed(config, name):
            return {"preset": name, "alias": "local-orchestrator"}
    return {}


def tagged_presets(config: dict, wanted: str) -> list[dict]:
    """Presets carrying a strength tag (exact or 'allround'): [{preset, alias,
    strengths}] — live or not. The honest-error companion to route_strength:
    lets a caller say 'dolphin IS tagged security, but stopped'."""
    presets = ((config.get("models") or {}).get("presets") or {})
    out = []
    from runtime.preset_store import preset_allowed
    for name, p in presets.items():
        if p.get("archived") or not preset_allowed(config, name):
            continue               # shelved/restricted presets never route
        strengths = list(p.get("strengths") or [])
        if wanted in strengths or "allround" in strengths:
            out.append({"preset": name, "alias": p.get("alias"),
                        "strengths": strengths})
    return out


def _route_slot_allowed(config: dict, slot: dict | None) -> bool:
    from runtime.preset_store import preset_allowed
    return bool(slot and slot.get("alias")
                and preset_allowed(config, str(slot.get("preset") or "")))


async def _live_routes(config: dict) -> list[dict]:
    """Live specialist presets, deduplicated and sorted by catalog priority."""
    presets = ((config.get("models") or {}).get("presets") or {})
    rank = {name: i for i, name in enumerate(presets)}
    found: dict[str, dict] = {}
    for slot_name in _ROUTING_SLOTS:
        try:
            slot = await live_slot(config, slot=slot_name)
        except Exception:
            slot = None
        if not slot or not slot.get("alias"):
            continue
        name = str(slot.get("preset") or "")
        if not name:
            # Legacy/custom probes may omit identity. Prefer the assigned slot
            # preset, then the first catalog holder of the reported alias.
            assigned = str(((config.get("models") or {})
                            .get("slots") or {}).get(slot_name) or "")
            matches = [n for n, p in presets.items()
                       if p.get("alias") == slot.get("alias")]
            name = assigned if assigned in matches else (matches[0] if matches else "")
            slot = {**slot, "preset": name}
        if not _route_slot_allowed(config, slot):
            continue
        p = presets.get(name) or {}
        if p.get("archived") or p.get("alias") == "local-orchestrator":
            continue
        found.setdefault(name, slot)
    return sorted(found.values(), key=lambda s: rank.get(s.get("preset"), len(rank)))


async def route_strength(config: dict, wanted: str) -> str | None:
    """Highest-priority LIVE exact strength holder, then allround holder."""
    routes = await _live_routes(config)
    for route in routes:
        if wanted in (route.get("strengths") or []):
            return route["alias"]
    for route in routes:
        if "allround" in (route.get("strengths") or []):
            return route["alias"]
    return None


async def route_strength_exact(config: dict, wanted: str) -> str | None:
    """Highest-priority live exact holder; no allround fallback."""
    for route in await _live_routes(config):
        if wanted in (route.get("strengths") or []):
            return route["alias"]
    return None


async def strength_plans(config: dict, wanted: str) -> list[dict]:
    """Ordered routing attempts: exact-tag presets, then allround presets.

    A stopped higher-priority local preset deliberately precedes a live lower
    one. Remote presets participate only when already live. Brains are excluded;
    the preferred brain is the delegate's separate final fallback.
    """
    from runtime.preset_store import preset_allowed
    presets = ((config.get("models") or {}).get("presets") or {})
    live = {r.get("preset"): r for r in await _live_routes(config)}
    # Live probes are authoritative about the serving model's tags (and may be
    # legacy/custom entries absent from the catalog). Unknown live names append
    # after priority-ranked catalog entries and are never hot-loaded.
    names = list(presets) + [n for n in live if n not in presets]
    plans = []
    for exact in (True, False):
        for name in names:
            p = presets.get(name) or {}
            current = live.get(name)
            if p.get("archived") or not preset_allowed(config, name):
                continue
            alias = (current or {}).get("alias") or p.get("alias")
            if not alias or p.get("alias") == "local-orchestrator":
                continue
            strengths = ((current or {}).get("strengths")
                         if current else p.get("strengths")) or []
            if (wanted in strengths) != exact:
                continue
            if not exact and "allround" not in strengths:
                continue
            target = route_target(presets, name) if name in presets else alias
            if current:
                plans.append({"mode": "live" if exact else "allround",
                              "alias": alias, "target": target, "preset": name})
            elif name in presets and not (p.get("remote_host") or "").strip():
                plans.append({"mode": "swap", "alias": alias, "target": target,
                              "preset": name,
                              **({"allround": True} if not exact else {})})
    return plans


async def strength_route(config: dict, wanted: str) -> dict:
    """First plan from strength_plans, preserving the historical public shape."""
    plans = await strength_plans(config, wanted)
    if not plans:
        # Historical extension seam: custom live-only routers can still supply
        # an allround alias when no catalog candidate exists.
        alias = await route_strength(config, wanted)
        return {"mode": "allround", "alias": alias} if alias else {}
    plan = plans[0]
    if plan.get("mode") == "swap":
        return {k: v for k, v in plan.items()
                if k != "target" and (k != "allround" or v)}
    return {"mode": plan["mode"], "alias": plan["alias"]}


def _payload_compatible(primary: dict, candidate: dict) -> bool:
    """Can a request body/response parser built for primary safely serve on
    candidate? Cross-family failover needs target-aware payloads, which the
    proxy cannot provide — restrict it to the same compatibility group."""
    if not primary or not candidate:
        return False
    p_backend = primary.get("backend") or "llama"
    c_backend = candidate.get("backend") or "llama"
    if p_backend != c_backend:
        return False
    p_think = (primary.get("caps") or {}).get("thinking")
    c_think = (candidate.get("caps") or {}).get("thinking")
    p_switch = p_think is True or (p_think is None and p_backend == "llama")
    c_switch = c_think is True or (c_think is None and c_backend == "llama")
    if p_switch != c_switch:
        return False
    if (primary.get("caps") or {}).get("vision") \
            and not (candidate.get("caps") or {}).get("vision"):
        return False
    p_family = str((primary.get("profile") or {}).get("think_strip") or "both")
    c_family = str((candidate.get("profile") or {}).get("think_strip") or "both")
    return p_family == c_family


async def live_strength_fallbacks(config: dict, wanted: str, primary: str,
                                  primary_preset: str = "",
                                  exclude_presets=()) -> list[str]:
    """Verified-live, payload-compatible candidates for LiteLLM failover.

    Internal aliases name exact preset deployments. Identity probing matters:
    alternatives can share one port, so a stopped preset must never become a
    fallback merely because another model is listening on its endpoint.
    """
    presets = ((config.get("models") or {}).get("presets") or {})
    routes = await _live_routes(config)
    primary_name = primary_preset or next(
        (name for name in presets if internal_preset_alias(name) == primary), "")
    primary_name = primary_name or next(
        (str(r.get("preset") or "") for r in routes if r.get("alias") == primary), "")
    if primary == "local-orchestrator" and not primary_name:
        primary_name = str(preferred_brain(config).get("preset") or "")
    primary_cfg = presets.get(primary_name) or {}
    if not primary_cfg:
        return []
    excluded = set(exclude_presets) | {primary_name}
    ordered = [r for r in routes if wanted in (r.get("strengths") or [])]
    ordered += [r for r in routes if "allround" in (r.get("strengths") or [])
                and r not in ordered]
    out = []
    for route in ordered:
        name = str(route.get("preset") or "")
        if name in presets and name not in excluded \
                and _payload_compatible(primary_cfg, presets[name]):
            out.append(internal_preset_alias(name))
            excluded.add(name)
    brain = preferred_brain(config)
    brain_name = str(brain.get("preset") or "")
    if brain_name and brain_name not in excluded:
        try:
            live = await live_slot(config, slot="brain")
        except Exception:
            live = None
        if live and live.get("preset") == brain_name \
                and _route_slot_allowed(config, live) \
                and _payload_compatible(primary_cfg, presets.get(brain_name) or {}):
            out.append(internal_preset_alias(brain_name))
    return list(dict.fromkeys(out))


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
# Shared by the loop's prompt injection and specialist.delegate so back-to-back runs
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
        # Candidate order decides who wins an AMBIGUOUS probe (two presets
        # sharing port + served_id — e.g. an archived predecessor and its
        # replacement): the slot's ASSIGNED preset first, archived ones
        # last. Live 2026-10-21: the probe mapped the specialist slot to
        # the archived twin, and the swap restore then tried to reload the
        # archived preset and failed, leaving the slot dark.
        cands.sort(key=lambda np: bool(np[1].get("archived")))
        if gpu is None:
            assigned = ((config.get("models") or {}).get("slots") or {}) \
                .get(slot, slot)
            cands.sort(key=lambda np: np[0] != assigned)
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


# ---- measured scheduling (preset-measured scheduling, 2026-10-05) --------------
# vram_gib is a HAND ESTIMATE. model.measure records the real footprint into
# preset["measured"] ({at, backend, server, preset_binary, ctx, gpu,
# vram_gib: {card: gib}, total_vram_gib, ram_gib, probe}); the helpers below
# let the eviction planner schedule by FIT instead of treating any co-tenant
# on a pinned GPU as a conflict.

_CTX_RE = None   # lazily compiled ^CTX_SIZE=(\d+) (conf launch value)


def _preset_ctx(p: dict) -> int | None:
    """The preset's configured ctx (CTX_SIZE in its .conf), None when the
    conf is unreadable — a measured record taken at another ctx is stale."""
    global _CTX_RE
    path = str((p or {}).get("preset") or "")
    if not path:
        return None
    if _CTX_RE is None:
        import re
        _CTX_RE = re.compile(r"^CTX_SIZE=(\d+)", re.MULTILINE)
    try:
        from pathlib import Path
        m = _CTX_RE.search(Path(path).read_text(
            encoding="utf-8", errors="replace"))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _mem_available_gib() -> float | None:
    """System MemAvailable in GiB (/proc/meminfo); None when unreadable —
    RAM fit checks go advisory then."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / (1024 * 1024), 2)
    except Exception:
        pass
    return None


def _measured_current(p: dict) -> dict | None:
    """The preset's measured record when it still describes the PRESENT
    config: same ctx, same gpu pinning AND same launch identity as now — a
    preset edited since the measurement (ctx bump, re-pin, llama ↔ vllm ↔
    radiance binary switch) falls through to the estimate. Records from
    before the identity keys exist carry no preset_binary — grandfathered,
    compared only when present."""
    m = (p or {}).get("measured")
    if not isinstance(m, dict) or not m:
        return None
    if str(m.get("gpu") or "") != str((p or {}).get("gpu") or ""):
        return None
    if m.get("ctx") != _preset_ctx(p):
        return None
    if "preset_binary" in m and str(m.get("preset_binary") or "") \
            != str((p or {}).get("binary") or ""):
        return None
    return m


def measured_stale(p: dict) -> bool:
    """A measured record EXISTS but no longer describes the present config
    (ctx/GPU/binary edited since — the planner ignores it then). UI badge
    helper."""
    m = (p or {}).get("measured")
    return bool(isinstance(m, dict) and m) and _measured_current(p) is None


def need_shares(ctx: ToolContext, p: dict) -> dict[str, float] | None:
    """Per-card GiB preset `p` needs on its pinned GPUs (preset-measured
    scheduling, 2026-10-05). Source of truth order:
    (a) a CURRENT preset["measured"] record — per-card measured vram_gib;
    (b) the vram_gib hand estimate, split EVENLY across the pinned cards;
    (c) neither → None (advisory: callers keep the conservative behavior).
    CPU presets (no pinned cards) → None; their contested resource is RAM,
    checked separately via _measured_current + _mem_available_gib."""
    from runtime.preset_store import gpu_list
    cards = gpu_list(p)
    if not cards:
        return None
    m = _measured_current(p)
    if m is not None:
        per = m.get("vram_gib")
        if isinstance(per, dict) and all(g in per for g in cards):
            try:
                return {g: float(per[g]) for g in cards}
            except (TypeError, ValueError):
                pass                 # hand-corrupted blob → estimate
    try:
        est = float((p or {}).get("vram_gib") or 0)
    except (TypeError, ValueError):
        est = 0.0
    if est > 0:
        share = round(est / len(cards), 2)
        return {g: share for g in cards}
    return None


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
    'slot') — the Admin → Models → Servers managed servers
    (brain/specialist/…). Goes through
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


async def plan_eviction(ctx: ToolContext, p: dict,
                        include_brain: bool = False) -> list[dict]:
    """What must stop so preset `p` can load. Returns eviction records:
      {"kind": "serve", "name", "preset", "gpu", "port", "alias"}
      {"kind": "slot",  "slot", "preset", "port"}
    `preset` on a record is the model ACTUALLY live there (probed), so a
    restore brings back reality, not the boot default. The brain slot is
    never touched unless include_brain — evicting it kills the current
    run's model, safe only for callers that restore before the brain's
    next turn (specialist.delegate does).

    Fit-aware (preset-measured scheduling, 2026-10-05): a PORT holder is
    always a conflict (two servers can't share a port), but a GPU-sharing
    occupant only conflicts when the hardware genuinely can't fit the
    newcomer ALONGSIDE: with per-card shares known (need_shares — measured
    truth, else the vram_gib estimate split evenly) and per-card free
    readable, co-tenancy is fine when every shared card g has
    free(g) ≥ share(g) + min_free_vram_gib. Shares unknown or free
    unreadable → the old flat rule (any GPU overlap conflicts). CPU-pinned
    presets play the same game on RAM: with a CURRENT measured.ram_gib,
    co-tenant CPU models only conflict when MemAvailable < ram_gib +
    models.min_free_ram_gib. The RAM check is NOT CPU-only (2026-10-21):
    vllm/radiance servers are GPU-pinned yet hold real host RAM in their
    engine-core/worker children, so a measured ram_gib gates them too — a
    RAM-short load evicts the CPU-pinned tenants (the usual big holders)."""
    import asyncio

    from runtime.preset_store import gpu_list, resolve_slot
    needed = set(gpu_list(p))
    port = int(p.get("port") or 0)
    shares = need_shares(ctx, p)
    floor = float(_cfg(ctx).get("min_free_vram_gib", 1.0))
    free: dict[str, float | None] = {}
    if shares is not None and needed:
        free = await asyncio.to_thread(S.gpus_free_gib, ctx, sorted(needed))

    def gpu_conflict(cards: set[str]) -> bool:
        overlap = needed & cards
        if not overlap:
            return False
        if shares is None:
            return True                    # unknown need → conservative

        def _short(g: str) -> bool:
            f = free.get(g)
            return f is None or f < shares[g] + floor

        return any(_short(g) for g in overlap)

    # RAM fit: not CPU-only — vllm/radiance servers are GPU-pinned yet hold
    # real host RAM in their worker children. Only a CURRENT measured ram_gib
    # enables the check — without it co-tenancy stays advisory (as before),
    # and without it CPU co-tenants never conflict.
    ram_short = False
    m = _measured_current(p)
    try:
        ram_need = float((m or {}).get("ram_gib") or 0)
    except (TypeError, ValueError):
        ram_need = 0.0
    if ram_need > 0:
        avail = _mem_available_gib()
        ram_floor = float((_catalog(ctx).get("min_free_ram_gib", 2.0))
                          or 2.0)
        if avail is not None and avail < ram_need + ram_floor:
            ram_short = True

    records: list[dict] = []
    seen_slots: set[str] = set()
    seen_serves: set[str] = set()

    for s in _live_servers(ctx):
        cards = set(_server_gpus(s))
        hit = (port and int(s.get("port") or 0) == port) or \
              gpu_conflict(cards) or (ram_short and not cards)
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
            cards = set(gpu_list(sp))
            # Unknown RAM on a CPU-only box must not load a second large
            # brain alongside the first during an internal specialist swap.
            cpu_brain_conflict = (include_brain and not needed
                                  and slot == "brain" and not cards)
            hit = ((port and sport == port) or gpu_conflict(cards)
                   or (ram_short and not cards) or cpu_brain_conflict)
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
                    # The slot fast path must first clear whatever the swap
                    # parked on the slot's hardware: the swap-in is NOT in
                    # `records` (it was the newcomer, not an evictee), so
                    # without this the restored server boots into occupied
                    # cards and crash-loops under the manager's auto-restart
                    # (live: full-box radiance brain restored onto the
                    # turbo-coder swap-in, crash loop ×20, 2026-10-22).
                    if slotp:
                        leftovers = [r for r in await plan_eviction(ctx, slotp)
                                     if not (r.get("kind") == "slot"
                                             and r.get("slot") == rec["slot"])]
                        if leftovers:
                            await evict_records(ctx, leftovers)
                    ok = await pm.start_one(rec["slot"])
                    if ok and slotp and slotp.get("port"):
                        ok = await _wait_serving(ctx, slotp)
                    notes.append(f"restored {label} on slot '{rec['slot']}'"
                                 if ok else
                                 f"FAILED to restore slot '{rec['slot']}' — "
                                 f"restart it in Admin → Models → Servers")
                else:
                    ok = await _restore_via_model_use(ctx, rec["preset"])
                    notes.append(f"restored {label} (was swapped onto "
                                 f"'{rec['slot']}')" if ok else
                                 f"FAILED to restore '{label}' — load it "
                                 f"with model.use or Admin → Models → Servers")
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
    # Restoration returns the pre-run hardware state, not a new model route.
    previous = getattr(ctx, "_model_restrict_bypass", False)
    setattr(ctx, "_model_restrict_bypass", True)
    try:
        res = await ModelUse().execute({"preset": preset, "swap": True}, ctx)
    finally:
        setattr(ctx, "_model_restrict_bypass", previous)
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
                "archived": bool(p.get("archived")),
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
    admin_only = True
    description = (
        "Ensure a catalog preset is served and return the LiteLLM alias to spawn on "
        "(agent.spawn(model=alias) / specialist.delegate). If it's already live on its port, "
        "returns immediately. Otherwise it serves the model on the preset's fixed port "
        "(reachable via the matching static litellm.yaml alias — no dynamic "
        "registration needed). If other models hold the preset's port or ANY of its "
        "pinned GPUs it reports the conflict rather than evicting — though a "
        "co-tenant the newcomer provably FITS ALONGSIDE (per-card measured/"
        "estimated VRAM plus a free margin) is not a conflict; pass swap:true to "
        "stop the serve-managed or boot-posture (Admin → Models → Servers) occupants first — "
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
        if p.get("archived"):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(f"preset '{name}' is archived — it is shelved and "
                                     f"never routes or serves. Unarchive it in "
                                     f"Admin → Models → Presets to use it again."))
        from runtime.preset_store import preset_allowed
        if not getattr(ctx, "_model_restrict_bypass", False) \
                and not preset_allowed(ctx.config, name):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"preset '{name}' is not in this run's allowed_presets")
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
                                         f"{label} listens on at {remote} (admin → Models → Presets)"))
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
                        f"(admin → Models → Presets), add the key to the env file, and "
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
        # (specialist.delegate does, with swap-back) — never from a model-facing
        # argument, which would stop this run's own model with no restore.
        include_brain = bool(args.get("include_brain")) and bool(
            getattr(ctx, "_allow_brain_evict", False))
        plan = await plan_eviction(ctx, p, include_brain=include_brain)
        if plan:
            if not args.get("swap"):
                occupants = ", ".join(
                    r.get("slot") or r.get("name") or "?" for r in plan)
                fit_known = need_shares(ctx, p) is not None
                hint = (f"loading '{name}' doesn't fit ALONGSIDE the models "
                        f"on its pinned hardware (need known, free "
                        f"insufficient) — held by: {occupants}. Stop them "
                        f"(serve.stop / Admin → Models → Servers) or pass "
                        f"swap:true to free the hardware automatically."
                        if fit_known else
                        f"loading '{name}' needs its port/GPUs free — held "
                        f"by: {occupants}. Stop them (serve.stop / Admin → "
                        f"Models → Servers) or pass swap:true to free the "
                        f"hardware automatically.")
                return ToolResult(status="ok", tool_name=self.name, result={
                    "alias": alias, "status": "hardware busy", "port": port,
                    "occupants": occupants, "hint": hint})
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
        # Measured truth beats the hand estimate (preset-measured
        # scheduling): total need comes from need_shares when available.
        shares = need_shares(ctx, p)
        need = round(sum(shares.values()), 2) if shares is not None else 0.0
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
            # The load failed AFTER the eviction ran — roll it back here or
            # the box stays gutted (live 2026-10-20: a specialist swap OOM'd
            # at model load; the error return dropped the evicted list, the
            # delegate's swap-back never saw it, and the brain stayed down
            # for the rest of the eval suite).
            result = dict(res.result or {})
            if evicted:
                notes = await restore_evicted(ctx, evicted)
                result["evicted"] = evicted
                result["restore"] = "; ".join(notes)
            return ToolResult(status="error", result=result, tool_name=self.name,
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
