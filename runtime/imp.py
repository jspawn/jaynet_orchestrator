"""Model impersonator (/imp) — command grammar + reply rendering.

`/imp <model>` routes the user's chat brain to another model (user-bound,
stored server-side — it follows them across devices) until `/impstop` or
`/imp off`. The web layer owns execution (user store, model.use for local
GPU-1 swaps, LiteLLM probes for cloud aliases); the grammar and the markdown
rendering live here, pure and testable.
"""

from __future__ import annotations

NAMES = ("imp", "impersonate", "impstop")


def is_imp(text: str) -> bool:
    """True for /imp, /imp <args>, /impersonate <args>, /impstop — but not for
    lookalikes such as /important."""
    t = (text or "").strip()
    return (t in ("/imp", "/impersonate", "/impstop")
            or t.startswith(("/imp ", "/impersonate ", "/impstop ")))


def _err(msg: str) -> dict:
    return {"action": "error",
            "error": msg + " — usage: `/imp <model> [budget=<usd>] "
                           "[ctxguard=<tokens>] [confirm]`"}


def parse(text: str) -> dict:
    """Parse one command line → {action: list|stop|set|error, ...}.

    `set` carries target/budget/ctxguard/confirm; `confirm` is the explicit
    keyword cloud aliases require (privacy: the whole chat leaves the box)."""
    t = (text or "").strip()
    parts = t.split()
    if parts and parts[0] == "/impstop":
        return {"action": "stop"}
    args = parts[1:]
    if not args or args == ["list"]:
        return {"action": "list"}
    if args[0] in ("off", "stop"):
        return {"action": "stop"}
    out = {"action": "set", "target": args[0],
           "budget": None, "ctxguard": None, "confirm": False}
    for tok in args[1:]:
        if tok == "confirm":
            out["confirm"] = True
            continue
        key, eq, val = tok.partition("=")
        if not eq:
            return _err(f"unknown argument `{tok}`")
        if key == "budget":
            try:
                out["budget"] = round(float(val), 4)
                if out["budget"] <= 0:
                    raise ValueError
            except ValueError:
                return _err(f"budget must be a positive dollar amount, got `{val}`")
        elif key == "ctxguard":
            try:
                out["ctxguard"] = int(val)
                if out["ctxguard"] <= 0:
                    raise ValueError
            except ValueError:
                return _err(f"ctxguard must be a positive token count, got `{val}`")
        else:
            return _err(f"unknown option `{key}` — known: budget, ctxguard, confirm")
    return out


def format_list(local_rows: list[dict], cloud: list[str], costs: dict,
                current: dict, default_brain: str) -> str:
    """`/imp list` markdown. local_rows are model.list preset rows (only ones
    with a chat alias); cloud is LiteLLM aliases minus local ones."""
    lines = ["**Model impersonator** — route every chat run to another brain "
             "until `/impstop` (or `/imp off`). User-bound: it follows you "
             "across devices."]
    if current.get("alias"):
        bits = [f"`{current.get('label') or current['alias']}` "
                f"({current.get('kind', '?')}, alias `{current['alias']}`)"]
        if current.get("budget"):
            bits.append(f"budget ≤ ${current['budget']:g}")
        if current.get("ctxguard"):
            bits.append(f"ctx guard {int(current['ctxguard']):,} tok")
        lines.append("\n**Active:** " + " · ".join(bits))
    else:
        lines.append(f"\n**Active:** none — the brain is `{default_brain}`.")
    if local_rows:
        lines.append("\n**Local presets** — `/imp <name>` swaps the GPU-1 slot "
                     "when needed (typing the command IS the swap decision):")
        for r in local_rows:
            role = (r.get("role") or "").split(" - ")[0].strip()
            live = " — **live**" if r.get("live") else ""
            lines.append(f"- `{r['preset']}` — {role} "
                         f"(GPU{r.get('gpu')}, ~{r.get('vram_gib', '?')} GiB){live}")
    if cloud:
        lines.append("\n**Cloud aliases** — `/imp <alias> confirm` "
                     "(the whole chat leaves the box):")
        for a in cloud:
            c = costs.get(a) or {}
            cost = (f" — ~${c.get('input', 0):g}/${c.get('output', 0):g} per 1M tok"
                    if c else "")
            lines.append(f"- `{a}`{cost}")
    lines.append("\nOptions: `budget=<usd>` (cost ceiling while active), "
                 "`ctxguard=<tokens>` (context-window guard for the nudge).")
    return "\n".join(lines)


def format_cloud_warning(target: str, costs: dict) -> str:
    """First `/imp <cloud-alias>` reply: the active-decision gate. Nothing is
    stored until the user re-runs with `confirm`."""
    c = costs.get(target) or {}
    cost = (f" (~${c.get('input', 0):g}/${c.get('output', 0):g} per 1M tok)"
            if c else "")
    return (f"**cloud impersonation: `{target}`**{cost}\n\n"
            "Everything in your chats — messages AND tool results — leaves this "
            "box and bills to the cloud key. Budgets still cap spend; consider "
            "adding `budget=<usd>`.\n\n"
            f"Re-run to confirm: `/imp {target} confirm`")


def format_set(spec: dict, status: str = "") -> str:
    """Confirmation markdown after an override was stored."""
    lines = [f"impersonating **{spec.get('label') or spec['alias']}** "
             f"({spec['kind']}, alias `{spec['alias']}`)"
             + (f" — {status}" if status else "")]
    opts = []
    if spec.get("budget"):
        opts.append(f"budget ≤ ${spec['budget']:g} per run")
    if spec.get("ctxguard"):
        opts.append(f"ctx guard {int(spec['ctxguard']):,} tok")
    if opts:
        lines.append(" · ".join(opts))
    if spec.get("kind") == "cloud":
        lines.append("☁ chat content now leaves the box — the header badge "
                     "keeps reminding you.")
    lines.append("All your chats (every device) use this brain until `/impstop`.")
    return "\n".join(lines)
