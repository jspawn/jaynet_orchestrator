"""Grammar and prompt construction for the scoped `/selftest` chat command.

Help and errors are returned by the web layer without a model. Valid requests
become ordinary agent runs with an explicit tool allowlist and the selftest
playbook force-loaded; all normal role/disable/confirmation policies still apply.
"""

from __future__ import annotations

import shlex

HELP = """**`/selftest` — scoped tool and skill checks**
- `/selftest all` — every advertised tool and every skill
- `/selftest tools` — every advertised tool only
- `/selftest skills` — discovery and load validation for every skill
- `/selftest <name> [more names…]` — only the named tools/skills, e.g.
  `/selftest fs.read` or `/selftest coding pdf`

Bare `/selftest` (or `/selftest help`) shows this help; it does not start a test.
Tool probes use the playbook's safe mode. Skill validation loads instructions but
never follows them. Separate multiple names with spaces or commas."""

_EXTRA_SYSTEM = (
    "\n\n— Scoped self-test (/selftest) —\n"
    "Load the playbook NOW via skill.load name=\"selftest\" and follow its "
    "safety and producer/consumer rules. The user's explicit scope overrides "
    "the playbook's default whole-toolset goal and its no-bulk-skill-load "
    "default. Never widen that scope. Finish with trace.coverage and return "
    "its markdown unchanged; do not upgrade failed or not_run rows."
)


def _advertised(registry) -> list[str]:
    return [t.name for t in registry.all()
            if not getattr(t, "hidden", False)]


def _present(registry, names) -> list[str]:
    advertised = set(_advertised(registry))
    return list(dict.fromkeys(name for name in names if name in advertised))


def _run_spec(registry, mode: str, tools: list[str], skills: list[str]) -> dict:
    if mode in {"all", "tools"}:
        requested = _advertised(registry)
    elif mode == "skills":
        requested = _present(registry, ["skill.list", "skill.load", "trace.coverage"])
    else:
        support = ["skill.load", "trace.coverage"]
        if skills:
            support.insert(0, "skill.list")
        requested = _present(registry, support + tools)

    if mode == "all":
        prompt = (
            "Run a scoped self-test of ALL advertised tools and ALL skills. "
            "Use the playbook's safe tool tiers. For skills, call skill.list, "
            "then skill.load every listed skill exactly once to validate it; "
            "do not follow the loaded skill instructions. Finish with trace.coverage."
        )
    elif mode == "tools":
        prompt = (
            "Run a scoped self-test of ALL advertised tools, not the skill "
            "catalog. Do not validate skills (the selftest playbook load is only "
            "for instructions). Finish with trace.coverage."
        )
    elif mode == "skills":
        prompt = (
            "Run a scoped self-test of EVERY skill, with no other tool probes. "
            "Call skill.list, then skill.load every listed skill exactly once "
            "to validate loadability; do not follow the loaded instructions. "
            "Use only skill.list, skill.load, and trace.coverage."
        )
    else:
        parts = []
        if tools:
            parts.append("ONLY these tools: " + ", ".join(f"`{n}`" for n in tools))
        if skills:
            parts.append("ONLY these skills: " + ", ".join(f"`{n}`" for n in skills))
        prompt = (
            "Run a scoped self-test of " + " and ".join(parts) + ". "
            "Use the smallest safe inputs and do not probe anything else. "
            "skill.load/skill.list are support only, and trace.coverage is the "
            "final report. For a named skill, validate it with one skill.load "
            "without following its instructions. If a tool needs an unavailable "
            "fixture or prerequisite, report that instead of expanding scope."
        )
    return {"action": "run", "message": prompt, "tools": requested,
            "extra_system": _EXTRA_SYSTEM}


def parse_command(message: str, registry, skills) -> dict | None:
    """Return None for other input, else help/error/run for `/selftest`."""
    text = (message or "").strip()
    if text in {"/selftest", "/selftest help"}:
        return {"action": "help", "text": HELP}
    if not text.startswith("/selftest "):
        return None
    try:
        tokens = shlex.split(text[len("/selftest "):])
    except ValueError as e:
        return {"action": "error", "text": f"**error** — {e}\n\n{HELP}"}
    tokens = [part for token in tokens for part in token.split(",") if part]
    if not tokens:
        return {"action": "help", "text": HELP}
    if len(tokens) == 1 and tokens[0] in {"all", "tools", "skills"}:
        return _run_spec(registry, tokens[0], [], [])

    known_tools = set(_advertised(registry))
    known_skills = set(skills or ())
    tools: list[str] = []
    skill_names: list[str] = []
    unknown: list[str] = []
    for name in dict.fromkeys(tokens):
        if name in known_tools:
            tools.append(name)
        elif name in known_skills:
            skill_names.append(name)
        else:
            unknown.append(name)
    if unknown:
        return {"action": "error", "text": (
            "**error** — unknown tool/skill: " + ", ".join(f"`{n}`" for n in unknown)
            + ". Use exact names from `/help tools` or the skill catalog.\n\n" + HELP)}
    return _run_spec(registry, "targets", tools, skill_names)
