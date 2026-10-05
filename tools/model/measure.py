"""model.measure — record a preset's REAL memory footprint into the catalog
(preset-measured scheduling, 2026-10-05).

`vram_gib` is a hand estimate; the fit-aware scheduler (tools/model/catalog.py
need_shares / plan_eviction) prefers measured per-card truth. Flow:

1. hibernate EVERYTHING local touching a GPU (boot-posture slots incl. the
   brain, plus serve-managed servers) via plan_eviction/evict_records with a
   pseudo-target spanning all local cards — steady-state CPU tenants (embed/
   rerank) ride through both readings and cancel out in the delta;
2. baseline per-GPU VRAM (serving.read_vram) + system RAM (MemAvailable);
3. load the preset through the same ServeStart path model.use uses, wait
   until it answers (big dense models load for minutes);
4. probe it with one tiny chat completion against its litellm alias
   (llama.cpp preallocates weights + full-ctx KV at startup; the probe only
   needs to touch the compute buffers);
5. read VRAM/RAM again — per-card usage = after − baseline on the preset's
   pinned cards only (other cards may drift), ram = MemAvailable drop;
6. write preset["measured"] into the preset store;
7. restore exactly what was hibernated (restore_evicted), even on
   cancellation — the brain is the current run's model.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from runtime import serving as S
from runtime.tool_base import Tool, ToolContext, ToolResult
from tools.model import catalog as MC
from tools.serve.lifecycle import S_slug, ServeStart

log = logging.getLogger(__name__)

# Ready-wait for the measured preset's server: big dense models load for
# minutes (the tool itself is bounded by its call_timeout_overrides entry).
_READY_TIMEOUT_S = 600.0
# Let the post-hibernate box settle before the baseline read (driver frees
# trail the process stops by a moment even after _wait_freed).
_SETTLE_S = 2.0


async def _wait_ready(port: int, timeout_s: float) -> bool:
    """Poll /v1/models until the fresh server answers (or the budget runs
    out). Same discipline as the imagegen restore wait: bounded, never
    hangs the tool."""
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if await S.query_model_id(base) is not None:
                return True
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(3.0, remaining))


async def _probe(alias: str) -> str:
    """One tiny chat completion against the preset's litellm alias —
    "ok", or a short error string (recorded in measured["probe"]; the VRAM
    reading stays meaningful either way)."""
    import os

    import httpx

    from runtime.paths import LITELLM_BASE
    body = {"model": alias, "max_tokens": 16, "temperature": 0,
            "messages": [{"role": "user", "content": "Say OK."}]}
    headers = {"Authorization": "Bearer "
               + os.environ.get("LITELLM_MASTER_KEY", "")}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{LITELLM_BASE}/v1/chat/completions",
                                  json=body, headers=headers)
            r.raise_for_status()
        return "ok"
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]


class ModelMeasure(Tool):
    name = "model.measure"
    description = (
        "Measure a LOCAL preset's real VRAM (per pinned GPU) and RAM footprint "
        "and store it in the catalog — the scheduler then packs models by "
        "measured fit instead of the vram_gib hand estimate. DISRUPTIVE: it "
        "hibernates EVERY local model (including this run's brain), loads "
        "the preset, probes it, then restores what was running. Run once per "
        "local preset after a download and after preset edits (a changed ctx "
        "or GPU pinning stales the measurement). Remote presets are never "
        "launched, so never measurable."
    )
    parameters = {
        "type": "object",
        "properties": {
            "preset": {"type": "string",
                       "description": "Catalog preset name (see model.list) — "
                                      "LOCAL only."},
        },
        "required": ["preset"],
    }
    requires_confirmation = True     # hibernates the whole box

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = (args.get("preset") or "").strip()
        presets = ((ctx.config.get("models") or {}).get("presets") or {})
        p = presets.get(name)
        if not p:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=(f"unknown preset '{name}'. Available: "
                                     + ", ".join(presets))
                              if presets else "the model catalog is empty")
        if p.get("archived"):
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"preset '{name}' is archived — unarchive "
                                    "it in Admin → Models → Presets first")
        if (p.get("remote_host") or "").strip():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"preset '{name}' is REMOTE — JayNet never "
                                    "launches remote servers, so there is "
                                    "nothing to measure from here")
        alias = p.get("alias")
        if not alias:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"preset '{name}' has no litellm alias to "
                                    "probe against — measuring needs one")

        # ---- 1+2: hibernate every local GPU tenant, baseline ----------------
        all_gpus = ",".join(MC._gpus(ctx))
        pseudo = {"gpu": all_gpus}     # no port, no need → conservative plan
        plan = await MC.plan_eviction(ctx, pseudo, include_brain=True)
        stopped, failures = await MC.evict_records(ctx, plan)
        if failures:
            notes = await MC.restore_evicted(ctx, stopped)
            return ToolResult(status="error", tool_name=self.name,
                              result={"restored": notes},
                              error="could not hibernate the box: "
                                    + "; ".join(failures))
        hibernated = [r.get("slot") or r.get("name") or "?" for r in stopped]
        await asyncio.sleep(_SETTLE_S)
        from runtime.preset_store import gpu_list
        cards = gpu_list(p)
        base_used = await self._used_map(ctx)
        ram0 = MC._mem_available_gib()

        # ---- 3-6: load, probe, measure, write — restore ALWAYS runs ---------
        measured = None
        err: BaseException | None = None
        srv_name = S_slug(f"measure-{name}")
        try:
            measured = await self._load_and_measure(
                ctx, name, p, alias, cards, base_used, ram0, srv_name)
        except BaseException as e:
            err = e
            log.warning("model.measure(%s) interrupted (%s) — restoring the "
                        "hibernated models", name, e)
        # The measurement server goes down BEFORE the restore — it can hold
        # the very port/cards the hibernated models retake (a slot's boot
        # process would crash-loop on the bind otherwise).
        try:
            await MC._stop_serve_record(
                ctx, {"kind": "serve", "name": srv_name})
        except Exception:
            log.exception("model.measure(%s): could not stop the "
                          "measurement server '%s'", name, srv_name)
        notes = await MC.restore_evicted(ctx, stopped)
        not_back = [n for n in notes if n.startswith("FAILED")]
        if not_back:
            log.warning("model.measure(%s): restore failures: %s",
                        name, not_back)
        if err is not None:
            # Cancellation (and friends) propagates AFTER the restore; an
            # ordinary failure (load error, ready timeout) is a loud error
            # result, not a crash.
            if isinstance(err, (asyncio.CancelledError, KeyboardInterrupt,
                                SystemExit, GeneratorExit)):
                raise err
            return ToolResult(status="error", tool_name=self.name,
                              result={"hibernated": hibernated,
                                      "restored": notes,
                                      "restore_not_ready": not_back},
                              error=f"measurement failed: {err}")
        return ToolResult(status="ok", tool_name=self.name, result={
            "status": "ok",
            "preset": name,
            "measured": measured,
            "hibernated": hibernated,
            "restored": notes,
            "restore_not_ready": not_back,
            "note": (f"measured footprint stored on preset '{name}' — the "
                     "scheduler now packs by these numbers. Re-measure after "
                     "changing the preset's ctx or GPU pinning."
                     + (" WARNING: some models did not come back — see "
                        "restore_not_ready" if not_back else "")),
        })

    @staticmethod
    async def _used_map(ctx: ToolContext) -> dict:
        vram = await asyncio.to_thread(S.read_vram, ctx) or []
        return {str(g["index"]): g.get("used_gib") for g in vram}

    async def _load_and_measure(self, ctx, name, p, alias, cards,
                                base_used, ram0, srv_name) -> dict:
        """Steps 3-6; raises on load/timeout (the caller restores anyway)."""
        serve_args = {
            "name": srv_name, "preset": p.get("preset"),
            "gpu": str(p.get("gpu") or ""), "kind": "llm", "register": False,
        }
        if p.get("port"):
            serve_args["port"] = p["port"]
        _bin = MC._serve_binary(ctx, p)
        if _bin:
            serve_args["llama_bin"] = _bin
        res = await ServeStart().execute(serve_args, ctx)
        if res.status != "ok":
            raise RuntimeError(res.error or f"failed to serve '{name}'")
        port = int((res.result or {}).get("port") or p.get("port") or 0)
        if not port or not await _wait_ready(port, _READY_TIMEOUT_S):
            log.warning("model.measure(%s): server not answering on :%s "
                        "after %.0fs", name, port, _READY_TIMEOUT_S)
            raise RuntimeError(
                f"'{name}' did not answer on :{port} within "
                f"{_READY_TIMEOUT_S:.0f}s — no measurement taken "
                f"(hibernated models are being restored)")
        probe = await _probe(alias)

        after_used = await self._used_map(ctx)
        ram1 = MC._mem_available_gib()
        per = {}
        for g in cards:
            u0, u1 = base_used.get(g), after_used.get(g)
            if u0 is not None and u1 is not None:
                per[g] = round(max(0.0, u1 - u0), 1)
        ram = (round(max(0.0, ram0 - ram1), 1)
               if ram0 is not None and ram1 is not None else 0.0)
        measured = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "backend": (p.get("backend") or "llama-server"),
            "ctx": MC._preset_ctx(p),
            "gpu": str(p.get("gpu") or ""),
            "vram_gib": per,
            "total_vram_gib": round(sum(per.values()), 1),
            "ram_gib": ram,
            "probe": probe,
        }
        from runtime.preset_store import PresetStore, db_path_for
        PresetStore(db_path_for(ctx.config)).upsert(
            name, {"measured": measured})
        try:
            from runtime.preset_store import load_into_config
            load_into_config(ctx.config)   # the live config sees it now
        except Exception:
            pass
        return measured
