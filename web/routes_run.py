"""Run-launching routes: quick-reply fast-path, slash commands, the shared run
launcher, /goal, chat/stream, voice and the tools list (split out of
web/server.py)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from types import SimpleNamespace

from fastapi import Header, HTTPException, Request

from runtime import __version__
from runtime import imp as imp_mod
from runtime.outputs import sweep, sweep_scratch
from web import goals as goals_mod
from web import projects as PJ
from web import watchdog as watchdog_mod
from web.ctx import _BUDGET_KEYS
from web.models import AnswerRequest, ApproveRequest, ChatRequest, VoiceRequest


async def _probe_model_endpoint(base: str) -> str:
    """Liveness probe for the smoke-test fast-path: GET /v1/models and return
    the served model id(s). Raises on any failure — the caller reports it."""
    import httpx
    # Mirror model_client._auth_headers: no header at all when the key is
    # unset (keyless localhost / quickstart), never a bare "Bearer ".
    key = os.environ.get("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{base.rstrip('/')}/v1/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    ids = [m.get("id", "?") for m in (data.get("data") or [])]
    return ", ".join(ids[:3]) or "unknown"


def _named_skill(message: str, names) -> str | None:
    """The first installed skill a message explicitly names — "<name> skill"
    or "skill <name>" (word-boundaried, case-insensitive). Deliberately
    conservative: a bare skill name without the word "skill" is too noisy to
    force a load on. Longest names first so overlapping names resolve to the
    more specific one."""
    for n in sorted(names, key=len, reverse=True):
        if re.search(rf"\b{re.escape(n)}\s+skill\b|\bskill\s+{re.escape(n)}\b",
                     message, re.IGNORECASE):
            return n
    return None


def register(app, s):
    runtime = s.runtime
    bus = s.bus
    provider = s.provider
    qprovider = s.qprovider
    tasks = s.tasks
    run_owner = s.run_owner
    users = s.users
    chats = s.chats
    reports = s.reports
    projects_dir = s.projects_dir
    outputs_dir = s.outputs_dir
    output_ttl_hours = s.output_ttl_hours
    chat_scratch_dir = s.chat_scratch_dir
    chat_scratch_ttl_hours = s.chat_scratch_ttl_hours
    _user = s._user
    _owner = s._owner
    _can_access_run = s._can_access_run
    _scratch_root = s._scratch_root
    _wiki_root = s._wiki_root
    _coerce_budget = s._coerce_budget
    _augment_with_attachments = s._augment_with_attachments
    _augment_with_project = s._augment_with_project
    _image_urls_for = s._image_urls_for
    _sweep_outputs_into_project = s._sweep_outputs_into_project
    _promote_chat_to_project = s._promote_chat_to_project
    _sweep_state = {"last": 0.0}

    # ---- quick-reply fast-path (greetings, thanks, bye) ----
    from runtime.quick_reply import QuickReply
    quick_reply = QuickReply()
    print(f"[quick-reply] loaded {len(quick_reply)} patterns")

    async def _fast_reply(run_id: str, text: str, owner: str | None):
        """Emit the same SSE events as a real run, but with a canned response."""
        import time as _t
        t0 = _t.time()
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            # Same envelope as the loop's emit — seq in particular, so a client
            # connecting after publish still replays these from the buffer.
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        await emit("run_start", {"message": "(fast-path)"})
        await emit("tool_selection", {
            "mode": "fast-path", "count": 0, "selected": [], "diag": {"via": "fast-path"}})
        await emit("model_turn", {
            "model": "fast-path", "content": text, "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": text, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    async def _smoke_reply(run_id: str, owner: str | None):
        """Answer a bare 'test' first message with a liveness probe of the model
        endpoint — proves the wiring works without spending an agent run.
        Same SSE shape as _fast_reply."""
        import time as _t
        t0 = _t.time()
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        ocfg = runtime.config.get("orchestrator", {}) or {}
        base = ocfg.get("litellm_base") or "http://127.0.0.1:4000"
        brain = ocfg.get("model") or "local-orchestrator"
        try:
            served = await _probe_model_endpoint(base)
            answer = (
                "**Smoke test passed** — no model run was used for this.\n\n"
                f"✅ the model endpoint `{base}` is alive, serving `{served}` "
                f"(brain alias `{brain}`).\n\n"
                "Everything is wired up — ask me something real!")
        except Exception as exc:
            answer = (
                "**Smoke test failed** — no model run was used for this.\n\n"
                f"❌ the model endpoint `{base}` (brain alias `{brain}`) is "
                f"not reachable ({type(exc).__name__}).\n\n"
                "Is the model server running? Quick start: `./start.sh` starts "
                "both sides. Full setup: check Admin → Status.")
        await emit("run_start", {"message": "test"})
        await emit("tool_selection", {"mode": "fast-path", "count": 0,
                                      "selected": [],
                                      "diag": {"via": "smoke-test"}})
        await emit("model_turn", {"model": "fast-path", "content": answer,
                                  "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": answer, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    async def _slash_reply(run_id: str, command: str, request: Request,
                           conversation_id: str | None, project_id: str | None):
        """Slash commands (/help, /<tool>): no model involved. Same SSE shape as
        _fast_reply; a slashed tool call runs directly, confirmation gate intact."""
        import time as _t

        from runtime.budget import Budget
        from runtime.loop import slash_spawn
        from runtime.slash import run_slash
        from runtime.tool_base import ToolContext
        t0 = _t.time()
        owner = _owner(request)
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        if project_id:
            _wr = PJ.files_root(projects_dir, owner, os.path.basename(project_id))
        else:
            _wr = _scratch_root(owner, conversation_id)
        bcfg = runtime.config.get("budgets") or {}
        budget = Budget(max_iterations=1,
                        max_wall_clock_s=float(bcfg.get("max_wall_clock_s") or 0),
                        max_cost_usd=float(bcfg.get("max_cost_usd") or 1.0),
                        max_total_tokens=int(bcfg.get("max_total_tokens") or 100000))
        ctx = ToolContext(request_id=run_id, config=runtime.config, budget=budget,
                          owner=owner, work_root=str(_wr) if _wr else None,
                          project_id=project_id,
                          vision_enabled=getattr(runtime, "vision_enabled", False))
        # Spawn-dependent tools (code.delegate, agent.spawn, architect, …) need
        # ctx.spawn; without it they error "sub-agents are not available". The
        # slash context has no parent run, so the child launches as a depth-1
        # agent capped by config agent.default_budget, with confirmations and
        # progress riding this run's stream.
        ctx.spawn = slash_spawn(runtime, run_id=run_id, owner=owner,
                                work_root=str(_wr) if _wr else None,
                                confirm_provider=provider, ask_provider=qprovider,
                                emit=emit)

        async def confirm(name: str, args: dict) -> bool:
            if provider is None:
                return False
            async def _em(t, i, d):
                await emit(t, d)
            return bool(await provider.confirm(run_id, name, args, _em))

        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "slash", "count": 0, "selected": [], "diag": {"via": "slash"}})
        try:
            answer = await run_slash(command, runtime.registry, ctx, confirm)
        except Exception as e:
            answer = f"**error** — {type(e).__name__}: {e}"
        await emit("model_turn", {"model": "slash", "content": answer, "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": answer, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    async def _compact_reply(run_id: str, command: str, request: Request,
                             history: list[dict]):
        """/compact [focus]: summarize the client-owned chat history with the
        local brain and hand back the replacement context — summary + verbatim
        tail — as the run_finish `compact` payload; the client swaps its turn
        list for it. One model call, no tools, no agent loop."""
        import time as _t

        from runtime import compact as _cp
        t0 = _t.time()
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        async def _finish(answer: str, status: str = "ok", tokens: int = 0,
                          payload: dict | None = None, model: str = "compact"):
            await emit("model_turn", {"model": model, "content": answer,
                                      "tool_calls": []})
            data = {"status": status, "answer": answer, "iterations": 0,
                    "cost_usd": 0, "total_tokens": tokens,
                    "latency_ms": int((_t.time() - t0) * 1000)}
            if payload:
                data["compact"] = payload
            await emit("run_finish", data)

        instruction = command.strip()[len("/compact"):].strip()
        older, kept = _cp.slice_history(history)
        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "compact", "count": 0, "selected": [], "diag": {"via": "compact"}})
        if not older:
            await _finish(_cp.nothing_note(len(history or [])))
            return
        try:
            r = await runtime.complete(
                _cp.build_summary_messages(older, instruction), think=False)
            if not r["content"]:
                raise RuntimeError("the brain returned an empty summary")
        except Exception as e:
            await _finish(f"**compact failed** — {type(e).__name__}: {e}",
                          status="error")
            return
        payload = {"summary": r["content"], "kept_messages": len(kept),
                   "dropped_messages": len(older),
                   "instruction": instruction or None}
        display = r["content"] + _cp.result_footer(len(older), len(kept),
                                                   r["usage"])
        await _finish(display, tokens=int(r["usage"].get("total_tokens") or 0),
                      payload=payload, model=runtime.model)

    async def _imp_reply(run_id: str, command: str, request: Request,
                         conversation_id: str | None, project_id: str | None):
        """/imp[ersonate] + /impstop — the model impersonator (see runtime/imp.py
        for the grammar). Same SSE shape as _slash_reply. A local `set` runs
        model.use(swap:true) directly: typing the command IS the active swap
        decision. A cloud `set` needs the explicit `confirm` keyword first —
        everything in the chat leaves the box. The override is user-bound
        (UserStore.brain_override) and applied to runs in /api/chat below."""
        import time as _t

        from runtime.budget import Budget
        from runtime.tool_base import ToolContext
        from web import server as _srv  # late: tests monkeypatch its helpers
        t0 = _t.time()
        owner = _owner(request)
        username = _user(request)["username"]
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        def _tool_ctx() -> ToolContext:
            if project_id:
                _wr = PJ.files_root(projects_dir, owner, os.path.basename(project_id))
            else:
                _wr = _scratch_root(owner, conversation_id)
            bcfg = runtime.config.get("budgets") or {}
            return ToolContext(
                request_id=run_id, config=runtime.config,
                budget=Budget(max_iterations=1,
                              max_wall_clock_s=float(bcfg.get("max_wall_clock_s") or 0),
                              max_cost_usd=float(bcfg.get("max_cost_usd") or 1.0),
                              max_total_tokens=int(bcfg.get("max_total_tokens") or 100000)),
                owner=owner, work_root=str(_wr) if _wr else None,
                project_id=project_id,
                vision_enabled=getattr(runtime, "vision_enabled", False))

        async def _list() -> str:
            local_rows = []
            try:
                res = await _srv.ModelList().execute({}, _tool_ctx())
                if res.status == "ok":
                    # only presets with a chat alias (embed/rerank are RAG services)
                    local_rows = [r for r in (res.result or {}).get("presets") or []
                                  if r.get("alias")]
            except Exception:
                pass
            ids = await _srv._litellm_model_ids(runtime)
            local_aliases = {r["alias"] for r in local_rows} | {runtime.model}
            costs = runtime.cost_table or {}
            if ids is None:      # proxy down — fall back to the cost table's names
                cloud = sorted(a for a in costs
                               if a not in local_aliases and not a.startswith("local-"))
            else:
                cloud = sorted(a for a in ids
                               if a not in local_aliases and not str(a).startswith("local-"))
            return imp_mod.format_list(local_rows, cloud, costs,
                                       users.get_brain_override(username),
                                       runtime.model)

        async def _set(parsed: dict) -> str:
            target = parsed["target"]
            presets = (runtime.config.get("models") or {}).get("presets") or {}

            def _spec(alias: str, kind: str, preset: str | None = None) -> dict:
                s = {"alias": alias, "label": preset or alias, "kind": kind}
                if preset:
                    s["preset"] = preset
                if parsed["budget"]:
                    s["budget"] = parsed["budget"]
                if parsed["ctxguard"]:
                    s["ctxguard"] = parsed["ctxguard"]
                return s

            if target in presets:                              # ---- local preset
                p = presets[target]
                alias = p.get("alias")
                if not alias:
                    return (f"`{target}` is not a chat model (no LiteLLM alias — "
                            "the RAG embed/rerank services can't be a brain).")
                if alias == runtime.model:
                    return (f"`{target}` already IS the default brain — nothing to "
                            "impersonate. (`/impstop` clears an active override.)")
                res = await _srv.ModelUse().execute({"preset": target, "swap": True},
                                                    _tool_ctx())
                r = res.result or {}
                if res.status != "ok":
                    return f"**error** — {res.error}"
                if r.get("hint"):      # slot busy / not enough VRAM / …
                    return f"**{r.get('status', 'failed')}** — {r['hint']}"
                spec = _spec(r.get("alias") or alias, "local", target)
                users.set_brain_override(username, spec)
                return imp_mod.format_set(spec, r.get("status", ""))

            # ---- cloud alias ----
            ids = await _srv._litellm_model_ids(runtime)
            if ids is None:
                return ("**error** — the LiteLLM proxy is unreachable, so I can't "
                        f"validate `{target}`. Try again when it's up.")
            if target not in ids:
                return (f"unknown model `{target}` — not a local preset and not a "
                        "LiteLLM alias. `/imp list` shows both.")
            if target == runtime.model:
                return (f"`{target}` already IS the default brain — nothing to "
                        "impersonate. (`/impstop` clears an active override.)")
            if not parsed["confirm"]:
                return imp_mod.format_cloud_warning(target, runtime.cost_table or {})
            spec = _spec(target, "cloud")
            users.set_brain_override(username, spec)
            return imp_mod.format_set(spec)

        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "slash", "count": 0, "selected": [], "diag": {"via": "imp"}})
        parsed = imp_mod.parse(command)
        act = parsed["action"]
        if act == "error":
            answer = "**error** — " + parsed["error"]
        elif act == "stop":
            had = users.get_brain_override(username)
            users.set_brain_override(username, None)
            answer = (f"impersonation stopped — the brain is back to `{runtime.model}`."
                      if had.get("alias") else
                      "no impersonation was active — the brain already is the default.")
        elif act == "list":
            answer = await _list()
        else:
            answer = await _set(parsed)

        await emit("model_turn", {"model": "imp", "content": answer, "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": answer, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    # ---- shared run launcher (chat endpoint + /goal supervisor) ----
    def _cleanup_task(run_id: str):
        """Done-callback factory: retire the task + replay buffer after a grace
        period. Shared by every run-launching path."""
        def _cleanup(_t: asyncio.Task) -> None:
            async def forget_later():
                from web import server as _srv  # late: tests patch _FORGET_AFTER_S
                await asyncio.sleep(_srv._FORGET_AFTER_S)
                bus.forget(run_id)
                tasks.pop(run_id, None)
                run_owner.pop(run_id, None)
            asyncio.create_task(forget_later())
        return _cleanup

    async def _launch_agent_run(*, username: str, message: str,
                                history: list | None = None,
                                conversation_id: str | None = None,
                                project_id: str | None = None,
                                share_private: bool = False,
                                auto_confirm: bool = False,
                                think: bool = True,
                                req_tools: list | None = None,
                                req_budget: dict | None = None,
                                prefs: dict | None = None,
                                extra_system: str | None = None,
                                extra_roots: list | None = None,
                                images: list | None = None,
                                run_overrides_extra: dict | None = None):
        """Launch one agent run with the full web-layer governance: global /
        per-user / per-request budget layering (tighten-only), the /imp brain
        override, global tool toggles, and the bus event wiring. Both /api/chat
        and the /goal supervisor go through here so the two can never drift.
        Returns (run_id, task) — await the task for the result dict, or just
        stream the bus."""
        from web import server as _srv  # late: tests patch _imp_local_alive
        run_id = uuid.uuid4().hex
        owner = None if username == "_token" else username
        prefs = prefs or {}
        disabled = set(users.get_global_disabled_tools())
        all_names = [t.name for t in runtime.registry.all()]
        enabled = [n for n in all_names if n not in disabled]
        # If the caller sent an explicit tool list, intersect with enabled.
        # If not (req_tools is None), pass None so the auto-selector can run.
        # If there are globally disabled tools, pass enabled as the filter.
        if req_tools is not None:
            allow = [n for n in req_tools if n in enabled]
        elif disabled:
            allow = enabled  # let selector pick from enabled subset
        else:
            allow = None     # let selector decide freely

        # One-shot impersonator notice (dead-slot auto-clear, see below). Injected
        # just after run_start with a fractional seq: the bus buffer keeps
        # insertion order, and seq is only a `> after_seq` replay filter, so a
        # fraction can't collide with the loop's integer sequence.
        imp_notice = {"text": None, "sent": False}

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)
            if (imp_notice["text"] and not imp_notice["sent"]
                    and event.get("type") == "run_start"):
                imp_notice["sent"] = True
                await bus.publish(run_id, {
                    "v": 1, "run_id": run_id, "seq": (event.get("seq") or 0) + 0.5,
                    "ts": time.time(), "type": "model_turn", "iteration": 0,
                    "data": {"model": "imp", "content": imp_notice["text"],
                             "tool_calls": []}})

        # The agent's structural workspace for this run: the active project's
        # files dir, else this chat's owner-scoped scratch dir. None -> the run
        # falls back to its ephemeral per-run tmp only.
        if project_id:
            _wr = PJ.files_root(projects_dir, owner, os.path.basename(project_id))
        else:
            _wr = _scratch_root(owner, conversation_id)
        work_root = str(_wr) if _wr else None
        # Plugin-declared project tools (project_tools hook): a plugin knows
        # what THIS project carries (e.g. graphify: a graph exists, and its
        # augment_project_context hint will advertise graph.*), while the
        # keyword selector only sees the message text. Fired only when the
        # selector gets to choose (req_tools None) — an explicit caller
        # allowlist stays authoritative.
        _force_tools: list[str] = []
        if project_id and req_tools is None and _wr is not None:
            from runtime import hooks as _hooks
            _safe_pid = os.path.basename(project_id)
            _meta = PJ.read_meta(projects_dir, owner, _safe_pid)
            if _meta is not None:
                for _extra in _hooks.fire("project_tools", owner, _safe_pid,
                                          _meta, _wr):
                    if isinstance(_extra, (list, tuple)):
                        _force_tools.extend(str(n) for n in _extra)
        # ---- budget governance ---------------------------------------------
        # Ceilings for this run layer as: admin-set global defaults (runtime.yaml
        # + persisted budget-defaults.json) < per-user account defaults (the
        # /account page) < this request's overrides. Each layer may only TIGHTEN
        # the one below (per-key min) — a user can tighten their own runs but no
        # layer can raise a ceiling past what the admin granted. Special case:
        # 0 = "no ceiling / no opinion" for every key: a positive value
        # tightens an unlimited (0) layer, a 0 never overrides a real ceiling.
        def _tighter(k: str, cur: float, new: float) -> float:
            return new if not cur else (cur if not new else min(cur, new))

        global_budget = runtime.config.get("budgets") or {}
        run_budget = {k: global_budget[k] for k in _BUDGET_KEYS
                      if global_budget.get(k) is not None}
        if username != "_token":      # token sessions have no account page
            for k, v in users.get_budget_defaults(username).items():
                if k in run_budget:
                    run_budget[k] = _tighter(k, run_budget[k], v)
                else:
                    run_budget[k] = v
        for k, v in _coerce_budget(req_budget or {}).items():
            if k not in run_budget:
                run_budget[k] = v
            else:
                run_budget[k] = _tighter(k, run_budget[k], v)
        # ---- model impersonator (/imp): the user-bound brain override --------
        imp = {} if username == "_token" else users.get_brain_override(username)
        imp_model = None
        imp_ctxguard = None
        if imp.get("alias"):
            if imp.get("kind") == "local" and not await _srv._imp_local_alive(runtime, imp):
                # Dead slot: the GPU was swapped under this override (model.use by
                # anyone). Clear it and run on the default brain, telling the user.
                users.set_brain_override(username, None)
                imp_notice["text"] = (
                    f"⚠ impersonated model `{imp.get('label') or imp['alias']}` is no "
                    "longer live on its slot — the override was cleared; this run "
                    f"uses the default brain (`{runtime.model}`).")
            else:
                imp_model = imp["alias"]
                if imp.get("budget"):      # another tighten-only ceiling layer
                    b = float(imp["budget"])
                    run_budget["max_cost_usd"] = (
                        _tighter("max_cost_usd", run_budget["max_cost_usd"], b)
                        if "max_cost_usd" in run_budget else b)
                if imp.get("ctxguard"):
                    imp_ctxguard = int(imp["ctxguard"])
        ro = {"compaction": prefs.get("compaction"),
              "parallel_tools": prefs.get("parallel_tools"),
              "sampling": prefs.get("sampling"),
              "sub_budget": prefs.get("sub_budget"),
              "architect_threshold": prefs.get("architect_threshold"),
              "context_tokens": imp_ctxguard,
              # Per-user timezone for the system-prompt datetime; "" → config.
              "timezone": ("" if username == "_token"
                           else users.get_timezone(username)) or None}
        if _force_tools:
            ro["force_tools"] = _force_tools
        if run_overrides_extra:
            ro.update(run_overrides_extra)
        coro = runtime.run(
            message,
            share_private=share_private,
            auto_confirm=auto_confirm,
            think=think,
            tools=allow,
            disabled_tools=disabled,
            budget_overrides=run_budget,
            run_overrides=ro,
            model=imp_model,
            run_id=run_id,
            on_event=on_event,
            confirm_provider=provider,
            ask_provider=qprovider,
            history=history,
            extra_system=extra_system,
            owner=owner,
            work_root=work_root,
            project_id=project_id,
            extra_roots=extra_roots,
            images=images,
            stream=True,
        )
        task = asyncio.create_task(coro)
        tasks[run_id] = task
        run_owner[run_id] = owner
        task.add_done_callback(_cleanup_task(run_id))

        async def _postmortem() -> None:
            # Watchdog (web/watchdog.py): distressed runs get a coroner's
            # report in the admin area. Detached — never affects the run.
            try:
                result = await task
            except asyncio.CancelledError:
                return
            except Exception:
                return
            try:
                await watchdog_mod.maybe_report(
                    runtime, reports, run_id=run_id, owner=owner, result=result)
            except Exception:
                pass
        asyncio.create_task(_postmortem())

        async def _reflect() -> None:
            # In-chat correction capture (runtime/reflect.py): an explicit
            # correction ("no, use X instead of Y") in a SUCCESSFUL session
            # never reaches the flag/eval improvement loop — this is the
            # path that teaches it. Detached, local-model-only, dedup'd
            # into the proposals inbox; nothing auto-applies.
            try:
                result = await task
            except (asyncio.CancelledError, Exception):
                return
            try:
                if not isinstance(result, dict) or result.get("status") != "ok":
                    return
                from runtime import reflect as _reflect_mod
                await _reflect_mod.maybe_capture(
                    runtime, message=message,
                    answer=str(result.get("answer") or ""),
                    run_ids=[run_id], owner=owner)
            except Exception:
                pass
        asyncio.create_task(_reflect())
        return run_id, task

    # ---- /goal: a user-bound objective pursued across runs (web/goals.py) ----
    goal_tasks: dict[str, asyncio.Task] = {}

    def _goal_kick(username: str) -> None:
        """(Re)start the user's goal supervisor; no-op while one is live."""
        t = goal_tasks.get(username)
        if t is not None and not t.done():
            return

        def _state_root(owner, goal):
            """The loop iteration's workspace — same resolution the launcher
            uses (project files dir, else the chat's scratch dir)."""
            pid = goal.get("project_id")
            if pid:
                return PJ.files_root(projects_dir, owner, os.path.basename(pid))
            row = chats.get_current(owner) or {}
            cid = (row.get("chat") or {}).get("cid")
            return _scratch_root(owner, cid)

        deps = SimpleNamespace(runtime=runtime, users=users, chats=chats,
                               launch=_launch_agent_run,
                               state_root=_state_root)
        goal_tasks[username] = asyncio.create_task(
            goals_mod.supervise(deps, username))

    s.goal_kick = _goal_kick   # web/routes_procs.py resumes goals on startup

    async def _goal_reply(run_id: str, command: str, request: Request,
                          project_id: str | None = None):
        """/goal [objective [| done when: X]] | pause | resume | stop — the
        slash surface for web/goals.py. Same SSE shape as _imp_reply. A goal
        started inside a project keeps it: every turn runs rooted in the
        project's files dir."""
        import time as _t
        t0 = _t.time()
        owner = _owner(request)
        username = _user(request)["username"]
        seq = {"n": 0}

        async def emit(event_type: str, data: dict) -> None:
            seq["n"] += 1
            await bus.publish(run_id, {"v": 1, "run_id": run_id, "seq": seq["n"],
                                       "ts": _t.time(), "type": event_type,
                                       "iteration": 0, "data": data})

        await emit("run_start", {"message": command})
        await emit("tool_selection", {
            "mode": "slash", "count": 0, "selected": [], "diag": {"via": "goal"}})
        if username == "_token":
            answer = "goals are user-bound — log in to use /goal."
        else:
            gcfg = goals_mod.config(runtime)
            parsed = goals_mod.parse(command)
            act = parsed["action"]
            goal = users.get_goal(username)
            if act == "error":
                answer = "**error** — " + parsed["error"]
            elif act == "status":
                answer = goals_mod.format_status(goal, gcfg["max_turns"])
            elif act == "stop":
                had = bool(goal.get("objective"))
                users.set_goal(username, None)
                answer = "goal stopped and cleared." if had else "no goal was set."
            elif act == "pause":
                if goal.get("status") == "active":
                    goal["status"] = "paused"
                    users.set_goal(username, goal)
                    answer = "goal paused — `/goal resume` continues."
                else:
                    answer = "no active goal to pause."
            elif act == "resume":
                if not goal.get("objective"):
                    answer = "no goal to resume — start one with `/goal <objective>`."
                elif goal.get("status") == "active":
                    answer = "the goal is already running."
                else:
                    goal["status"] = "active"
                    users.set_goal(username, goal)
                    _goal_kick(username)
                    answer = "goal resumed — the next turn launches now."
            else:                                       # start
                goal = {"objective": parsed["objective"],
                        "criterion": parsed["criterion"], "status": "active",
                        "turn": 0, "tokens_total": 0,
                        "started_at": _t.strftime("%Y-%m-%dT%H:%M:%S"), "log": []}
                if parsed.get("fresh"):
                    goal["fresh"] = True
                if parsed.get("check"):
                    goal["check"] = parsed["check"]
                if project_id:
                    goal["project_id"] = project_id
                users.set_goal(username, goal)
                if not ((chats.get_current(owner) or {}).get("chat") or {}).get("turns"):
                    # No live chat snapshot: create one so goal turns have
                    # somewhere visible to land on every device.
                    chats.set_current(owner, {
                        "id": None, "cid": uuid.uuid4().hex,
                        "title": ("🔄 " if parsed.get("fresh") else "🎯 ")
                                 + parsed["objective"][:60],
                        "saved": False, "turns": []})
                _goal_kick(username)
                where = (f"in project `{project_id}` — every turn works there.\n"
                         if project_id else "")
                if parsed.get("fresh"):
                    answer = (f"loop set {where}— iteration 1/{gcfg['max_turns']} "
                              "launches now.\n"
                              f"**{parsed['objective']}**\n"
                              f"done when: {parsed['criterion']}\n"
                              "Every iteration starts with a FRESH context — "
                              "STATE.md in the workspace carries the memory "
                              "between iterations.\n"
                              "`/goal` shows status · `/goal stop` ends it. Any "
                              "message you send pauses it; `/goal resume` "
                              "continues.")
                else:
                    answer = (f"goal set {where}— turn 1/{gcfg['max_turns']} launches now.\n"
                              f"**{parsed['objective']}**\n"
                              f"done when: {parsed['criterion']}\n"
                              "`/goal` shows status · `/goal stop` ends it. Any "
                              "message you send pauses it; `/goal resume` continues.")
        await emit("model_turn", {"model": "goal", "content": answer,
                                  "tool_calls": []})
        await emit("run_finish", {
            "status": "ok", "answer": answer, "iterations": 0,
            "cost_usd": 0, "total_tokens": 0,
            "latency_ms": int((_t.time() - t0) * 1000)})

    # ---- chat / stream ----
    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        run_id = uuid.uuid4().hex
        u = _user(request)

        # ---- /wgs: skill-authoring session — agent loop, playbook force-loaded ----
        # Every other slash command bypasses the model (see below); /wgs needs
        # it, so it rewrites itself into a normal run with writing-great-skills
        # pinned via extra_system — a forced-load pointer: the run is told to
        # skill.load the playbook and follow it.
        extra_system = None
        extra_roots = None
        _wgs = req.message.strip()
        if _wgs == "/wgs" or _wgs.startswith("/wgs "):
            req.message = _wgs[4:].strip() or (
                "I want to write or improve a skill. Ask me what it should do.")
            extra_system = (
                "\n\n— Skill-authoring mode (/wgs) —\n"
                "The user invoked /wgs: load the playbook NOW via "
                "skill.load name=\"writing-great-skills\" and follow it for the "
                "rest of this conversation. Its bundled GLOSSARY.md defines the "
                "bold terms — read it when you need a definition.")

        # ---- /llmwiki: wiki-maintenance session — agent loop, wiki skill force-
        # loaded, and the wiki dir granted as an extra writable root (fs.* stays
        # confined elsewhere). Project chat → wiki inside the project, deleted
        # with it; bare chat → the owner's global wiki.
        _lw = req.message.strip()
        if _lw == "/llmwiki" or _lw.startswith("/llmwiki "):
            req.message = _lw[len("/llmwiki"):].strip() or (
                "Show me the current state of the wiki: read index.md and "
                "summarize what is there.")
            wiki_root = _wiki_root(_owner(request), req.project_id)
            if wiki_root is not None:
                extra_roots = [str(wiki_root)]
                extra_system = (
                    "\n\n— Wiki mode (/llmwiki) —\n"
                    "The user invoked /llmwiki: load the wiki playbook NOW via "
                    "skill.load name=\"wiki\" and follow it for the rest of "
                    f"this conversation. The wiki lives at `{wiki_root}` — that "
                    "directory is readable and writable in this run; view, "
                    "create, modify and remove pages there as requested.")
            else:
                extra_system = (
                    "\n\n— Wiki mode (/llmwiki) —\nThe project this chat "
                    "belonged to no longer exists, so its wiki is gone too. "
                    "Tell the user that briefly; do not write any files.")

        # ---- /charter: project-charter interview — agent loop, charter skill
        # force-loaded, project wiki granted as an extra writable root. The
        # interview's answers land as the wiki's first pages; the wiki extractor
        # then carries them into the project graph. Requires a project.
        _ch = req.message.strip()
        if _ch == "/charter" or _ch.startswith("/charter "):
            req.message = _ch[len("/charter"):].strip() or (
                "Start the charter interview for this project.")
            wiki_root = (_wiki_root(_owner(request), req.project_id)
                         if req.project_id else None)
            if wiki_root is not None:
                extra_roots = [str(wiki_root)]
                extra_system = (
                    "\n\n— Charter mode (/charter) —\n"
                    "The user invoked /charter: load the charter playbook NOW "
                    "via skill.load name=\"project-charter\" and follow it for "
                    f"this conversation. The project wiki lives at `{wiki_root}`"
                    " — that directory is readable and writable in this run.")
            else:
                extra_system = (
                    "\n\n— Charter mode (/charter) —\n"
                    + ("No project is active, so there is no project wiki to "
                       "charter. Tell the user briefly to create or select a "
                       "project first; do not write any files."
                       if not req.project_id else
                       "The project this chat belonged to no longer exists, so "
                       "its wiki is gone too. Tell the user that briefly; do "
                       "not write any files."))

        # ---- Skill named in plain text: pin the load mechanically ----
        # The brain can ignore both the user's "use the X skill" and the
        # prompt's "Named skill? Load it." directive (seen live: j-space-loop
        # skipped skill.load entirely despite an explicit instruction). Same
        # enforcement philosophy as /wgs and /charter: when the message names
        # an installed skill, the run is TOLD to load it — a harness
        # guarantee, not a prompt hope. Slash modes above already pinned
        # theirs (extra_system set), so they skip this.
        if extra_system is None and not req.message.strip().startswith("/"):
            _sn = _named_skill(req.message,
                               (getattr(runtime, "skills", None) or {}).keys())
            if _sn:
                extra_system = (
                    f"\n\n— Skill named: {_sn} —\n"
                    f"The user's message names the skill \"{_sn}\": load it "
                    f"NOW via skill.load name=\"{_sn}\" before anything else "
                    "and follow it for the rest of this conversation.")

        # ---- Slash commands: /goal, /imp, /compact, /help, /<tool> — no agent loop ----
        _sl = req.message.strip()
        if _sl.startswith("/") and not req.attachments:
            if _sl == "/goal" or _sl.startswith("/goal ") \
                    or _sl == "/loop" or _sl.startswith("/loop "):
                # User-bound objective pursued across runs (web/goals.py).
                # /loop = the fresh-context sibling (Ralph pattern).
                coro = _goal_reply(run_id, _sl, request, req.project_id)
            elif imp_mod.is_imp(_sl):
                # Model impersonator: user-bound brain override (runtime/imp.py).
                coro = _imp_reply(run_id, _sl, request, req.conversation_id,
                                  req.project_id)
            elif _sl == "/compact" or _sl.startswith("/compact "):
                # Summarizes the chat with the local brain (one call) — the
                # only slash command that touches a model; it needs history.
                coro = _compact_reply(run_id, _sl, request, req.history or [])
            else:
                coro = _slash_reply(run_id, _sl, request, req.conversation_id,
                                    req.project_id)
            task = asyncio.create_task(coro)
            tasks[run_id] = task
            run_owner[run_id] = _owner(request)
            task.add_done_callback(_cleanup_task(run_id))
            return {"run_id": run_id}

        # ---- Smoke-test fast-path: a bare "test" as the very first message ----
        # The classic first thing a new user types. Answer with a liveness
        # probe of the model endpoint instead of spending an agent run on it.
        # Bare "test" only (any casing/punctuation), no attachments, no
        # history, no project — in a project "test" more likely means "run the
        # tests", and real test requests keep reaching skills, tools, the loop.
        _st = req.message.strip().lower().strip("!?.… ")
        if (_st == "test" and not req.attachments and not req.project_id
                and not req.history and not req.conversation_id):
            task = asyncio.create_task(_smoke_reply(run_id, _owner(request)))
            tasks[run_id] = task
            run_owner[run_id] = _owner(request)
            task.add_done_callback(_cleanup_task(run_id))
            return {"run_id": run_id}

        # ---- Fast-path: instant reply for greetings/thanks/bye ----
        qr = quick_reply.match(req.message, u.get("username", ""))
        if qr and not req.attachments and not req.project_id:
            # Same bookkeeping as a real run: run_owner gates /api/stream, and
            # the done-callback retires the task + replay buffer afterwards.
            task = asyncio.create_task(_fast_reply(run_id, qr, _owner(request)))
            tasks[run_id] = task
            run_owner[run_id] = _owner(request)
            task.add_done_callback(_cleanup_task(run_id))
            return {"run_id": run_id}

        # /goal auto-pause: a real message takes priority over an unattended
        # goal. Slash commands and quick replies (handled above) don't pause.
        if u["username"] != "_token":
            _g = users.get_goal(u["username"])
            if _g.get("status") == "active":
                _g["status"] = "paused"
                users.set_goal(u["username"], _g)

        # Opportunistic, throttled cleanup of expired unsaved outputs + abandoned scratch.
        if time.time() - _sweep_state["last"] > 600:
            _sweep_state["last"] = time.time()
            try:
                sweep(outputs_dir, output_ttl_hours)
                sweep_scratch(chat_scratch_dir, chat_scratch_ttl_hours)
            except Exception:
                pass

        message = _augment_with_attachments(request, req.message, req.attachments)
        message = _augment_with_project(request, message, req.project_id)
        images = (_image_urls_for(request, req.attachments)
                  if getattr(runtime, "vision_enabled", False) else None)
        run_id, _task = await _launch_agent_run(
            username=u["username"], message=message, history=req.history,
            conversation_id=req.conversation_id, project_id=req.project_id,
            share_private=req.share_private, auto_confirm=req.auto_confirm,
            think=req.think, req_tools=req.tools,
            req_budget=req.budget_overrides,
            prefs={"compaction": req.compaction,
                   "parallel_tools": req.parallel_tools,
                   "sampling": req.sampling,
                   "sub_budget": req.sub_budget,
                   "architect_threshold": req.architect_threshold},
            extra_system=extra_system, extra_roots=extra_roots, images=images)
        return {"run_id": run_id}

    # ---- voice channel (native/voice clients; server-managed conversation) ----
    def _history_from_turns(turns: list[dict]) -> list[dict]:
        h: list[dict] = []
        for t in turns:
            if t.get("user_message"):
                h.append({"role": "user", "content": t["user_message"]})
            h.append({"role": "assistant", "content": t.get("answer", "")})
        return h

    @app.post("/api/voice")
    async def voice(req: VoiceRequest, request: Request):
        vcfg = runtime.config.get("voice", {}) or {}
        if not vcfg.get("enabled", False):
            raise HTTPException(status_code=404, detail="voice channel disabled")
        if not (req.text or "").strip():
            raise HTTPException(status_code=400, detail="empty text")
        owner = _owner(request)

        # Continue an existing owned conversation, else start a fresh one. A
        # supplied id is only honoured if it belongs to this user (no cross-user
        # writes); otherwise a new id is minted and returned.
        cid = req.conversation_id
        chat = chats.get(cid, owner) if (cid and owner is not None) else None
        if chat:
            turns = list(chat["turns"]); conversation_id = cid
        else:
            turns = []; conversation_id = uuid.uuid4().hex
        project_id = (chat or {}).get("project_id")

        # Voice command: "create a project [called X]". Deterministic — promote
        # this conversation into a project (sweeping any files made so far),
        # bind the chat to it, and confirm by voice. No agent run needed.
        if owner is not None and _CREATE_PROJECT_RE.match(req.text or ""):
            m = _CREATE_PROJECT_RE.match(req.text)
            name = (m.group("name") or "").strip().strip('"\'.')
            if not name:
                name = (chat or {}).get("title") or "Voice project"
            meta = _promote_chat_to_project(owner, {"id": conversation_id,
                    "title": (chat or {}).get("title"), "turns": turns}, name)
            reply = (f"Done. I've created the project {meta['name']} and saved this "
                     f"conversation and its files into it. Anything we make from here "
                     f"goes in there too.")
            turns.append({"user_message": req.text, "answer": reply,
                          "run_id": None, "status": "ok"})
            chats.upsert(conversation_id, (chat or {}).get("title"), turns,
                         owner=owner, project_id=meta["id"])
            return {"conversation_id": conversation_id, "run_id": None,
                    "text": reply, "status": "ok", "project_id": meta["id"]}

        # Safe unattended toolset: drop confirmation-gated tools and cloud
        # llm.call (no UI to approve them on a voice turn), and the user's own
        # disabled tools. The brain still answers directly; tools are optional.
        # Kept for chat mode too (voice:false) — same unattended-client logic.
        gated = {t.name for t in runtime.registry.all()
                 if getattr(t, "requires_confirmation", False)}
        remote = set((runtime.config.get("privacy", {}) or {}).get("remote_llm_tools", []))
        disabled = set(users.get_global_disabled_tools())
        allow = [t.name for t in runtime.registry.all()
                 if t.name not in gated and t.name not in remote and t.name not in disabled]

        # voice:false = chat client on the same server-managed conversation:
        # full markdown persona (no overlay), thinking on, normal budgets/model.
        run_id = uuid.uuid4().hex
        vbudget = (vcfg.get("budget") or None) if req.voice else None

        async def on_event(event: dict) -> None:
            await bus.publish(run_id, event)

        async def _go():
            # If this conversation is bound to a project, give the agent the
            # project context so its work centres there.
            msg = _augment_with_project(request, req.text, project_id) if project_id else req.text
            _wr = (PJ.files_root(projects_dir, owner, os.path.basename(project_id))
                   if project_id else _scratch_root(owner, conversation_id))
            result = await runtime.run(
                msg, think=not req.voice,
                extra_system=vcfg.get("persona") if req.voice else None,
                budget_overrides=vbudget,
                model=vcfg.get("model") if req.voice else None,
                tools=allow, run_id=run_id, on_event=on_event,
                confirm_provider=provider, ask_provider=qprovider,
                history=_history_from_turns(turns),
                owner=owner, work_root=(str(_wr) if _wr else None),
                project_id=project_id, stream=True)
            if owner is not None:   # persist the turn for continuity
                turns.append({"user_message": req.text, "answer": result.get("answer", ""),
                              "run_id": run_id, "status": result.get("status")})
                try:
                    chats.upsert(conversation_id, None, turns,
                                 owner=owner, project_id=project_id)
                except Exception:
                    pass
                # Keep files made this turn inside the project.
                if project_id:
                    try:
                        _sweep_outputs_into_project(owner, [run_id], project_id)
                    except Exception:
                        pass
            return result

        if req.stream:
            task = asyncio.create_task(_go())
            tasks[run_id] = task
            run_owner[run_id] = owner

            def _cleanup(_t: asyncio.Task) -> None:
                async def forget_later():
                    from web import server as _srv  # late: tests patch _FORGET_AFTER_S
                    await asyncio.sleep(_srv._FORGET_AFTER_S)
                    bus.forget(run_id); tasks.pop(run_id, None); run_owner.pop(run_id, None)
                asyncio.create_task(forget_later())
            task.add_done_callback(_cleanup)
            # app opens GET /api/stream/{run_id} (same Bearer token) for tokens
            return {"conversation_id": conversation_id, "run_id": run_id}

        run_owner[run_id] = owner
        try:
            result = await _go()
        finally:
            run_owner.pop(run_id, None)
        return {"conversation_id": conversation_id, "run_id": run_id,
                "text": result.get("answer", ""), "status": result.get("status")}

    @app.get("/api/stream/{run_id}")
    async def stream(run_id: str, request: Request,
                     last_event_id: str | None = Header(default=None)):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        after = int(last_event_id) if (last_event_id or "").isdigit() else 0
        q = bus.subscribe(run_id, after_seq=after)
        from sse_starlette.sse import EventSourceResponse

        async def gen():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"id": str(event.get("seq", 0)),
                           "event": event["type"],
                           "data": json.dumps(event)}
                    if event["type"] in ("run_finish",):
                        break
            finally:
                bus.unsubscribe(run_id, q)

        return EventSourceResponse(gen())

    @app.post("/api/approve/{run_id}")
    async def approve(run_id: str, req: ApproveRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        ok = provider.resolve(run_id, req.confirmation_id, req.approved)
        if not ok:
            raise HTTPException(status_code=404, detail="no pending confirmation")
        return {"ok": True}

    @app.post("/api/answer/{run_id}")
    async def answer(run_id: str, req: AnswerRequest, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        ok = qprovider.resolve(run_id, req.ask_id, {"answers": req.answers})
        if not ok:
            raise HTTPException(status_code=404, detail="no pending questions")
        return {"ok": True}

    @app.post("/api/cancel/{run_id}")
    async def cancel(run_id: str, request: Request):
        if not _can_access_run(request, run_id):
            raise HTTPException(status_code=404, detail="no such run")
        t = tasks.get(run_id)
        if t and not t.done():
            t.cancel()
            return {"ok": True, "cancelled": True}
        return {"ok": True, "cancelled": False}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__,
                "tools": len(runtime.registry.all())}

    # ---- tools (global admin-controlled enable/disable) ----
    @app.get("/api/tools")
    async def list_tools(request: Request):
        disabled = set(users.get_global_disabled_tools())
        out = []
        for t in sorted(runtime.registry.all(), key=lambda x: x.name):
            out.append({
                "name": t.name,
                "namespace": t.name.split(".")[0],
                "description": getattr(t, "description", "") or "",
                "private": bool(getattr(t, "private", False)),
                "requires_confirmation": bool(getattr(t, "requires_confirmation", False)),
                "parameters": getattr(t, "parameters", {}) or {},
                "enabled": t.name not in disabled,
            })
        return {"tools": out}


# "create a project [called X]" spoken to the voice channel. Anchored so it
# only fires on a clear imperative, not when 'project' appears mid-sentence.
_CREATE_PROJECT_RE = re.compile(
    r"^\s*(?:jaynet[,.\s]+)?(?:please[,.\s]+)?"
    r"(?:create|make|start|set\s*up|save\s+(?:this|it|the\s+chat)\s+as)\s+"
    r"(?:a\s+|an\s+)?(?:new\s+)?project"
    r"(?:\s+(?:called|named|titled|for)\s+(?P<name>.+?))?\s*[.!]?\s*$",
    re.IGNORECASE)
