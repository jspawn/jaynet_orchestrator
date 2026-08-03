"""Chain engine — named, reusable multi-step pipelines (chain.list / chain.run).

A chain is a YAML file in the chains dir (config `chains.dir`, default
<orch_root>/chains):

    description: research a topic and distill a brief
    steps:
      - id: research
        agent: "Research {{input}} and report the key facts with sources."
        tools: [web.search, web.fetch]        # optional narrowing
        model: local-specialist               # optional
      - id: brief
        prompt: "Distill this into a 5-bullet brief:\n\n{{steps.research.output}}"
        model: local-orchestrator             # optional (default)

Two step kinds:
- `agent`  — a bounded sub-agent via ctx.spawn: full tool power, its own
  confirmation routing and budget carve-out (identical to agent.spawn).
- `prompt` — one stateless LLM call for transforming prior output. LOCAL
  aliases only: a cloud call from inside a chain would bypass the loop's
  privacy/confirmation gate that guards llm.call, so cloud steps are refused
  with a pointer to llm.call instead.

Templates interpolate `{{input}}` (the caller's input) and
`{{steps.<id>.output}}` (a previous step's result). Anything else is an error
at load/render time, not silently passed through.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from runtime.tool_base import ToolContext
from tools.llm.cloud_models import _call_via_litellm, resolve_model_alias

_MAX_STEPS = 20
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_NAME_OK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class ChainError(Exception):
    """User-actionable chain failure (not found, bad YAML, step failed)."""


def chains_dir(config: dict) -> Path:
    d = (config.get("chains") or {}).get("dir")
    if d:
        return Path(d).expanduser()
    return Path(__file__).resolve().parents[2] / "chains"   # <orch_root>/chains


def list_chains(config: dict) -> list[dict]:
    """Every chain in the dir: {name, description, steps}."""
    out = []
    d = chains_dir(config)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.yaml")):
        try:
            chain = load_chain(config, f.stem)
            out.append({"name": f.stem, "description": chain["description"],
                        "steps": len(chain["steps"])})
        except ChainError as e:
            out.append({"name": f.stem, "error": str(e)})
    return out


def load_chain(config: dict, name: str) -> dict:
    """Load and validate one chain by name (filename without .yaml)."""
    if not _NAME_OK.match(name or ""):
        raise ChainError(f"invalid chain name '{name}' "
                         f"(letters, digits, dash, underscore)")
    path = chains_dir(config) / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(c["name"] for c in list_chains(config) if "error" not in c)
        raise ChainError(f"unknown chain '{name}'. Available: {known or '(none)'} "
                         f"— chains are YAML files in {chains_dir(config)}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ChainError(f"chain '{name}' is not valid YAML: {e}") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list) or not raw["steps"]:
        raise ChainError(f"chain '{name}' needs a non-empty 'steps' list")
    if len(raw["steps"]) > _MAX_STEPS:
        raise ChainError(f"chain '{name}' has {len(raw['steps'])} steps "
                         f"(max {_MAX_STEPS})")
    seen: set[str] = set()
    for i, step in enumerate(raw["steps"]):
        if not isinstance(step, dict):
            raise ChainError(f"chain '{name}' step {i + 1} is not a mapping")
        sid = step.get("id")
        if not sid or not _NAME_OK.match(str(sid)):
            raise ChainError(f"chain '{name}' step {i + 1} needs a valid 'id'")
        if sid in seen:
            raise ChainError(f"chain '{name}': duplicate step id '{sid}'")
        seen.add(sid)
        kinds = [k for k in ("prompt", "agent") if step.get(k)]
        if len(kinds) != 1:
            raise ChainError(f"chain '{name}' step '{sid}' needs exactly one of "
                             f"'prompt' or 'agent'")
    return {"name": name,
            "description": str(raw.get("description") or ""),
            "steps": raw["steps"]}


def _render(template: str, variables: dict[str, str], where: str) -> str:
    """Substitute {{placeholders}}; unknown ones are an error, never silent."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return variables[key]
        avail = ["input"] + sorted(k for k in variables if k.startswith("steps."))
        raise ChainError(f"{where}: unknown placeholder '{{{{{key}}}}}' — "
                         f"available: {', '.join(avail)}")
    return _PLACEHOLDER.sub(sub, template)


def _local_alias(model: str | None, ctx: ToolContext) -> str:
    """Resolve a prompt-step model, LOCAL aliases only (see module docstring)."""
    if not model:
        return "local-orchestrator"
    resolved = resolve_model_alias(model, ctx.config)
    if resolved is None:
        # A live serve.start'd alias is local too — accept it.
        from runtime import serving as S
        from tools.serve.lifecycle import _state_dir
        for s in S.list_servers(_state_dir(ctx)):
            if s.get("litellm_alias") == model and S.pid_alive(s.get("pid")):
                return model
        raise ChainError(f"unknown model '{model}'")
    if not resolved.startswith("local-"):
        raise ChainError(
            f"chain prompt steps are LOCAL-only ('{model}' is a cloud alias): a "
            f"cloud call from inside a chain would bypass the privacy/approval "
            f"gate. Run the chain with a local model, or call llm.call directly "
            f"(it is gated) between two chain.run calls.")
    return resolved


async def run_chain(chain: dict, input_text: str, ctx: ToolContext) -> dict:
    """Execute a chain; returns {output, steps, tokens} or raises ChainError."""
    name = chain["name"]
    variables: dict[str, str] = {"input": input_text}
    steps_out: list[dict] = []
    tokens = {"prompt": 0, "completion": 0, "cached": 0}

    for step in chain["steps"]:
        sid = step["id"]
        where = f"chain '{name}' step '{sid}'"
        if step.get("agent"):
            if ctx.spawn is None:
                raise ChainError(f"{where}: sub-agents are not available in "
                                 f"this runtime (agent step needs ctx.spawn)")
            task = _render(str(step["agent"]), variables, where)
            child = await ctx.spawn(
                task,
                tools=step.get("tools"),
                model=step.get("model"),
                name=f"{name}/{sid}",
                budget=step.get("budget"),
                verify=step.get("verify"),
            )
            if child.get("status") != "ok":
                raise ChainError(
                    f"{where}: agent step failed "
                    f"({child.get('error') or child.get('status')}). "
                    f"Completed steps: {', '.join(s['id'] for s in steps_out) or 'none'}")
            output = str(child.get("answer") or "")
            steps_out.append({"id": sid, "kind": "agent", "status": "ok",
                              "sub_run_id": child.get("run_id")})
        else:
            alias = _local_alias(step.get("model"), ctx)
            prompt = _render(str(step["prompt"]), variables, where)
            think = step.get("think")
            res = await _call_via_litellm(
                alias, prompt, None, step.get("system"),
                step.get("format") == "json",
                bool(think) if think is not None else None, ctx)
            if res.status != "ok":
                raise ChainError(f"{where}: prompt step failed ({res.error}). "
                                 f"Completed steps: "
                                 f"{', '.join(s['id'] for s in steps_out) or 'none'}")
            output = str(res.result or "")
            for k in tokens:
                tokens[k] += int(res.tokens_used.get(k, 0))
            steps_out.append({"id": sid, "kind": "prompt", "status": "ok",
                              "model": alias})
        variables[f"steps.{sid}.output"] = output
        steps_out[-1]["preview"] = output[:200] + ("…" if len(output) > 200 else "")

    final = steps_out and variables[f"steps.{chain['steps'][-1]['id']}.output"] or ""
    return {"output": final, "steps": steps_out, "tokens": tokens}
