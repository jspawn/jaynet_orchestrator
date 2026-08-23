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
      answer_exact_any: ["42"]        # GAIA-style: normalized EXACT match of
                                      # the final answer (last line / text after
                                      # "final answer:" / whole answer); case-
                                      # and punctuation-insensitive
      checker: |                      # optional Python GRADING script, run
        import os, sys                # harness-side after the last turn with
        sys.exit(0 if os.environ.get( # cwd = the case work_root (fixture files
            "EVAL_ANSWER") else 1)    # readable), EVAL_ANSWER = final answer.
                                      # exit 0 = pass; stdout tail = failure msg
      max_iterations: 10            # per harness turn
      ask_reply: "yes, proceed"     # canned answer for ask.user cards
    requires_tools: [graph.query]   # optional; case SKIPS (not fails) when a
                                    # listed tool isn't in the run's toolset —
                                    # e.g. plugin disabled on this install
    container:                      # optional; run the case inside a podman
      image: benchlab-tb-x-a1b2c3   # container (Terminal-Bench full mode):
      workdir: /app                 # required image tag + optional workdir
                                    # (default /app) the case sandbox is
                                    # bind-mounted at. code.execute then runs
                                    # INSIDE the container; the checker gets
                                    # EVAL_CONTAINER_ID. Skips when podman or
                                    # the image is missing.
    project:                        # optional; run project-bound with a fixture
      graph: true                   # pre-build the graphify project graph
      files:                        # seeded under <sandbox>/projects/_eval/
        models.py: |                # eval-<id>/files before turn 1
          class User: ...
      seed_code: |                  # optional Python snippet, run with cwd=files
        import random               # after `files` are written — generates LARGE
        random.seed(42)             # fixtures programmatically (deterministic!),
        ...                         # not 200KB YAML literals. Scrubbed env, 120s cap.
    judge_rubric: |                 # free-text grading guidance for the judge
      Pass if prices are from the current year and sources are cited.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from tools.chain.engine import _NAME_OK

log = logging.getLogger(__name__)

DRIVERS = ("scripted", "adaptive")
CASE_KEYS = ("id", "name", "tags", "driver", "turns", "expect",
             "judge_rubric", "requires_tools", "project", "container")
EXPECT_KEYS = ("must_use_tools", "must_use_any_tools", "must_not_use_tools",
               "answer_contains_any", "answer_exact_any", "checker",
               "max_iterations", "ask_reply")
PROJECT_KEYS = ("files", "graph", "seed_code")
CONTAINER_KEYS = ("image", "workdir")
# Podman image reference: name[:tag] or registry/name:tag — no spaces/shell
# metacharacters (the tag is passed to podman argv verbatim).
_IMAGE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")


@dataclass
class EvalCase:
    id: str
    name: str
    tags: list[str] = field(default_factory=list)
    driver: str = "scripted"
    turns: list[str] = field(default_factory=list)      # user messages, in order
    expect: dict = field(default_factory=dict)
    judge_rubric: str = ""
    requires_tools: list[str] = field(default_factory=list)
    project: dict = field(default_factory=dict)         # {files, graph}
    container: dict = field(default_factory=dict)       # {image, workdir}
    origin: str = "builtin"                             # builtin | custom

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "tags": self.tags,
                "driver": self.driver,
                "turns": [{"user": t} for t in self.turns],
                "expect": self.expect, "judge_rubric": self.judge_rubric,
                "requires_tools": self.requires_tools,
                "project": self.project,
                "container": self.container,
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
    for k in raw:
        if k not in CASE_KEYS:
            errors.append(f"unknown case key '{k}' (one of {', '.join(CASE_KEYS)})")
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
                  "must_not_use_tools", "answer_contains_any",
                  "answer_exact_any"):
            v = expect.get(k)
            if v is not None and not (isinstance(v, list)
                                      and all(isinstance(x, str) for x in v)):
                errors.append(f"expect.{k} must be a list of strings")
        chk = expect.get("checker")
        if chk is not None and not isinstance(chk, str):
            errors.append("expect.checker must be a string (Python grading "
                          "script; exit 0 = pass)")
        mi = expect.get("max_iterations")
        if mi is not None and not (isinstance(mi, int) and mi > 0):
            errors.append("expect.max_iterations must be a positive integer")
        ar = expect.get("ask_reply")
        if ar is not None and not isinstance(ar, str):
            errors.append("expect.ask_reply must be a string "
                          "(canned answer for ask.user cards)")
    rt = raw.get("requires_tools")
    if rt is not None and not (isinstance(rt, list)
                               and all(isinstance(x, str) for x in rt)):
        errors.append("requires_tools must be a list of strings")
    proj = raw.get("project")
    if proj is not None:
        if not isinstance(proj, dict):
            errors.append("project must be a mapping")
        else:
            for k in proj:
                if k not in PROJECT_KEYS:
                    errors.append(f"unknown project key '{k}' "
                                  f"(one of {', '.join(PROJECT_KEYS)})")
            files = proj.get("files") or None       # {} counts as absent
            seed = proj.get("seed_code")
            if files is None and seed is None:
                errors.append("project must seed at least one file "
                              "(files: mapping of relpath -> content) or "
                              "generate them via seed_code")
            if files is not None:
                if not isinstance(files, dict):
                    errors.append("project.files must be a mapping of "
                                  "relpath -> content")
                else:
                    for p, content in files.items():
                        pp = PurePosixPath(str(p))
                        if pp.is_absolute() or ".." in pp.parts:
                            errors.append(f"project.files path '{p}' must be a "
                                          "relative path without '..'")
                        elif not isinstance(content, str):
                            errors.append(f"project.files['{p}'] content must be "
                                          "a string")
            if seed is not None and not isinstance(seed, str):
                errors.append("project.seed_code must be a string (Python "
                              "snippet run with the fixture dir as cwd)")
            g = proj.get("graph")
            if g is not None and not isinstance(g, bool):
                errors.append("project.graph must be a boolean")
    ctr = raw.get("container")
    if ctr is not None:
        if not isinstance(ctr, dict):
            errors.append("container must be a mapping")
        else:
            for k in ctr:
                if k not in CONTAINER_KEYS:
                    errors.append(f"unknown container key '{k}' "
                                  f"(one of {', '.join(CONTAINER_KEYS)})")
            image = ctr.get("image")
            if not (isinstance(image, str) and _IMAGE_OK.match(image)):
                errors.append("container.image is required and must be a valid "
                              "image tag (letters, digits, . _ : / @ -)")
            workdir = ctr.get("workdir", "/app")
            if not (isinstance(workdir, str)
                    and PurePosixPath(workdir).is_absolute()):
                errors.append("container.workdir must be an absolute path "
                              "(the container mount point, default /app)")
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
        requires_tools=[str(x) for x in (raw.get("requires_tools") or [])],
        project=dict(raw.get("project") or {}),
        container=dict(raw.get("container") or {}),
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
