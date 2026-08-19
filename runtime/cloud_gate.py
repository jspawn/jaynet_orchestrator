"""Destination-alias cloud gate (security audit S1).

The loop's privacy/confirmation gates are tool-NAME based
(`privacy.remote_llm_tools: [llm.call]`), but several tools reach a cloud MODEL
behind a local-sounding tool name: council.debate panelists, eval.compare
models, verify.* verifier overrides, and agent.spawn / chain `agent` steps
(ctx.spawn model=). This module
makes "cloud" a property of the destination ALIAS so all of them enforce the
loop's two gate semantics:

  1. private taint + no share_private -> refuse (a tool cannot ask the human
     the way the loop's llm.call gate can; ctx.spawn CAN ask and does);
  2. confirmation.confirm_cloud_calls -> human approval before the call
     (tools via their `needs_confirmation` override, ctx.spawn inline).

Local aliases never gate. "Local" = the `local-*` prefix OR a key of
`orchestrator.local_concurrency` — the same source the loop's
`AgentRuntime._local_aliases` is derived from. Unknown/empty aliases are NOT
local: the gate fails closed.
"""

from __future__ import annotations


def local_aliases(config: dict) -> frozenset:
    """Configured local LiteLLM aliases: the orchestrator.local_concurrency
    keys (same source as AgentRuntime._local_aliases / _local_concurrency)."""
    return frozenset(((config.get("orchestrator") or {}).get("local_concurrency")) or {})


def is_local_alias(alias: str | None, config: dict) -> bool:
    """True if calls to this alias stay on-box."""
    return bool(alias) and (alias.startswith("local-") or alias in local_aliases(config))


def cloud_targets(aliases, config: dict) -> list[str]:
    """The subset of `aliases` whose calls would leave the box."""
    return [a for a in aliases if not is_local_alias(a, config)]


def confirm_cloud_enabled(config: dict) -> bool:
    """Mirror of the loop's confirmation.confirm_cloud_calls switch."""
    return bool((config.get("confirmation") or {}).get("confirm_cloud_calls", True))


def privacy_refusal(ctx, aliases) -> str | None:
    """Refusal message when a cloud target would carry private-tainted content
    off-box from a TOOL (council.debate, eval.compare), else None.

    Tools see the run's taint state via ctx.private_taint but have no
    confirm-provider seam, so unlike the loop's llm.call gate (which can ask
    the human per call) a tainted run is a hard REFUSE here — fail safe."""
    if getattr(ctx, "share_private", False) or not getattr(ctx, "private_taint", False):
        return None
    cloud = cloud_targets(aliases, ctx.config)
    if not cloud:
        return None
    return ("blocked by privacy: the conversation contains private tool results and "
            f"{', '.join(cloud)} is a cloud model, and this tool cannot ask for the "
            "per-call approval the llm.call gate offers — the call was refused. "
            "Use a local model instead, or ask the user to enable 'share with cloud' "
            "for this run.")


def spawn_gate(model: str | None, config: dict, *, private_taint: bool,
               share_private: bool) -> str | None:
    """Gate decision for a sub-agent on `model` (the loop's ctx.spawn choke
    point — covers agent.spawn and chain `agent` steps). Returns None for a
    local target (no gate), "privacy" when the run is tainted and sharing is
    not allowed (needs the privacy confirmation, never auto-approved), or
    "confirm" when confirm_cloud_calls requires a standard approval."""
    if not model or is_local_alias(model, config):
        return None
    if private_taint and not share_private:
        return "privacy"
    if confirm_cloud_enabled(config):
        return "confirm"
    return None
