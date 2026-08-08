"""Eval test cases — YAML scenarios for the behavioural eval harness.

Unit tests cover the plumbing; eval cases test the *behaviour* of the running
harness + models: scripted or adaptive multi-turn conversations driven through
the real agent loop by runtime/eval_runner.py and graded by a judge model.

Two layers, custom wins on id clash (same pattern as Studio skills/chains):

    <repo>/evals/<id>.yaml                — shipped seeds (git-managed)
    $ORCH_DATA/custom/evals/<id>.yaml     — admin-created (Studio/Eval tab)

Schema (see evals/ for examples):

    id: web-freshness               # optional; defaults to the file stem
    name: Web search uses current year
    tags: [web, freshness]          # bulk-run by tag
    driver: scripted                # scripted | adaptive (driver model writes follow-ups)
    turns:                          # scripted: every turn; adaptive: seed turn(s)
      - user: "What does a Pilatus astronomy evening cost this autumn?"
      - user: "and for two kids?"
    expect:                         # deterministic checks, all optional
      must_use_tools: [web.search]        # every listed tool must be called
      must_use_any_tools: [code.run, code.execute]  # at least one of them
      must_not_use_tools: [llm.call]
      answer_contains_any: ["{year}"]   # {year}/{next_year} auto-substituted
      max_iterations: 10            # per harness turn
      ask_reply: "yes, proceed"     # canned answer for ask.user cards
    judge_rubric: |                 # free-text grading guidance for the judge
      Pass if prices are from the current year and sources are cited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tools.chain.engine import _NAME_OK

log = logging.getLogger(__name__)

DRIVERS = ("scripted", "adaptive")
EXPECT_KEYS = ("must_use_tools", "must_use_any_tools", "must_not_use_tools",
               "answer_contains_any", "max_iterations", "ask_reply")


@dataclass
class EvalCase:
    id: str
    name: str
    tags: list[str] = field(default_factory=list)
    driver: str = "scripted"
    turns: list[str] = field(default_factory=list)      # user messages, in order
    expect: dict = field(default_factory=dict)
    judge_rubric: str = ""
    origin: str = "builtin"                             # builtin | custom

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "tags": self.tags,
                "driver": self.driver,
                "turns": [{"user": t} for t in self.turns],
                "expect": self.expect, "judge_rubric": self.judge_rubric,
                "origin": self.origin}


def validate_case_dict(fallback_id: str, raw: object) -> list[str]:
    """Validate a parsed YAML mapping; returns a list of error strings (empty
    = valid). Used by the loader, the admin validate endpoint, and tests."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["case must be a YAML mapping"]
    cid = str(raw.get("id") or fallback_id)
    if not _NAME_OK.match(cid):
        errors.append(f"invalid id '{cid}' (letters, digits, dash, underscore)")
    if not str(raw.get("name") or "").strip():
        errors.append("name is required")
    driver = raw.get("driver", "scripted")
    if driver not in DRIVERS:
        errors.append(f"driver must be one of {DRIVERS}")
    turns = raw.get("turns")
    if not isinstance(turns, list) or not turns:
        errors.append("at least one turn is required")
    else:
        for i, t in enumerate(turns):
            if not isinstance(t, dict) or not str(t.get("user") or "").strip():
                errors.append(f"turn {i + 1} must be a mapping with a non-empty 'user'")
    tags = raw.get("tags", [])
    if tags and not (isinstance(tags, list)
                     and all(isinstance(t, str) for t in tags)):
        errors.append("tags must be a list of strings")
    expect = raw.get("expect", {}) or {}
    if not isinstance(expect, dict):
        errors.append("expect must be a mapping")
    else:
        for k in expect:
            if k not in EXPECT_KEYS:
                errors.append(f"unknown expect key '{k}' (one of {', '.join(EXPECT_KEYS)})")
        for k in ("must_use_tools", "must_use_any_tools",
                  "must_not_use_tools", "answer_contains_any"):
            v = expect.get(k)
            if v is not None and not (isinstance(v, list)
                                      and all(isinstance(x, str) for x in v)):
                errors.append(f"expect.{k} must be a list of strings")
        mi = expect.get("max_iterations")
        if mi is not None and not (isinstance(mi, int) and mi > 0):
            errors.append("expect.max_iterations must be a positive integer")
        ar = expect.get("ask_reply")
        if ar is not None and not isinstance(ar, str):
            errors.append("expect.ask_reply must be a string "
                          "(canned answer for ask.user cards)")
    if not str(raw.get("judge_rubric") or "").strip():
        errors.append("judge_rubric is required (the judge grades against it)")
    return errors


def parse_case(fallback_id: str, text: str, origin: str) -> EvalCase:
    """Parse + validate one YAML document. Raises ValueError on errors."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"bad YAML: {e}") from e
    errors = validate_case_dict(fallback_id, raw)
    if errors:
        raise ValueError("; ".join(errors))
    return EvalCase(
        id=str(raw.get("id") or fallback_id),
        name=str(raw["name"]).strip(),
        tags=[str(t) for t in (raw.get("tags") or [])],
        driver=raw.get("driver", "scripted"),
        turns=[str(t["user"]) for t in raw["turns"]],
        expect=dict(raw.get("expect") or {}),
        judge_rubric=str(raw["judge_rubric"]).strip(),
        origin=origin,
    )


def _load_dir(base: Path, origin: str, into: dict[str, EvalCase]) -> None:
    base = Path(base)
    if not base.is_dir():
        return
    for f in sorted(base.glob("*.yaml")):
        try:
            into[f.stem] = parse_case(f.stem, f.read_text(encoding="utf-8"), origin)
        except (ValueError, OSError) as e:
            log.warning("eval case %s/%s skipped: %s", base, f.name, e)


def load_cases(builtin_dir: Path | None = None,
               custom_dir: Path | None = None) -> list[EvalCase]:
    """All cases, builtin layer first, custom overriding on id clash."""
    from runtime import paths
    cases: dict[str, EvalCase] = {}
    _load_dir(builtin_dir or (paths.HOME / "evals"), "builtin", cases)
    _load_dir(custom_dir or paths.CUSTOM_EVALS_DIR, "custom", cases)
    return list(cases.values())


def get_case(case_id: str, **kwargs) -> EvalCase | None:
    for c in load_cases(**kwargs):
        if c.id == case_id:
            return c
    return None
