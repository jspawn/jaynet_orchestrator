"""Eval test cases — YAML scenarios for the behavioural eval harness.

Unit tests cover the plumbing; eval cases test the *behaviour* of the running
harness + models: scripted or adaptive multi-turn conversations driven through
the real agent loop by runtime/eval_runner.py and graded by a judge model.

Two layers, custom wins on id clash (same pattern as Studio skills/chains):

    <repo>/evals/<id>.yaml                — shipped seeds (git-managed)
    $JAYNET_DATA/custom/evals/<id>.yaml     — admin-created (Studio & Eval → Eval)

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
      canary_not_in_tool_args: ["TOP-SECRET"]  # injection cases: the planted
                                      # canary string must never appear in any
                                      # tool call's args (trace scan)
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
      network: true                 # optional (default false = --network none):
                                    # outbound network for tasks that download
                                    # (official Terminal-Bench allows it). The
                                    # container is throwaway and credential-free.
      compose: /abs/task/dir        # optional: multi-service tasks — the dir
      client_service: client        # holds the task's docker-compose.yaml and
                                    # the whole stack is started per run via
                                    # podman-compose (project-scoped, torn down
                                    # after); client_service (default client)
                                    # names the agent container inside it.
    budget:                         # optional; overrides the eval.* run budget
      turn_wall_clock_s: 1200       # for THIS case (0 = unlimited)
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tools.chain.engine import _NAME_OK

log = logging.getLogger(__name__)

DRIVERS = ("scripted", "adaptive")
CASE_KEYS = ("id", "name", "tags", "driver", "turns", "expect",
             "judge_rubric", "judge", "requires_tools", "project", "container",
             "budget", "stub_tools")
EXPECT_KEYS = ("must_use_tools", "must_use_any_tools", "must_not_use_tools",
               "answer_contains_any", "answer_exact_any", "checker",
               "max_iterations", "ask_reply", "canary_not_in_tool_args",
               "tool_calls")
PROJECT_KEYS = ("files", "graph", "seed_code")
CONTAINER_KEYS = ("image", "workdir", "network", "compose", "client_service")
BUDGET_KEYS = ("turn_wall_clock_s",)
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
    judge: bool = True                              # False = deterministic only
    requires_tools: list[str] = field(default_factory=list)
    project: dict = field(default_factory=dict)         # {files, graph}
    container: dict = field(default_factory=dict)       # {image, workdir, network, compose, client_service}
    budget: dict = field(default_factory=dict)          # {turn_wall_clock_s}
    stub_tools: list[dict] = field(default_factory=list)  # {name, description, parameters, result?}
    origin: str = "builtin"                             # builtin | custom

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "tags": self.tags,
                "driver": self.driver,
                "turns": [{"user": t} for t in self.turns],
                "expect": self.expect, "judge_rubric": self.judge_rubric,
                "judge": self.judge,
                "requires_tools": self.requires_tools,
                "project": self.project,
                "container": self.container,
                "budget": self.budget,
                "stub_tools": self.stub_tools,
                "origin": self.origin}


# ---- case schema (pydantic; was a 140-line hand-rolled validator) -------------
# Field types carry the isinstance checks; validators carry the exact error
# wording the loader, the admin validate endpoint and the tests have always
# produced. validate_case_dict() remaps pydantic's error dicts back to those
# strings, so error text is unchanged.


class _TurnModel(BaseModel):
    model_config = ConfigDict(extra="allow")   # turns may carry extra keys
    user: object = None

    @field_validator("user")
    @classmethod
    def _user_nonempty(cls, v):
        if not str(v or "").strip():
            raise ValueError("empty")   # wording comes from the loc remap
        return v


class _ExpectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    must_use_tools: list[StrictStr] | None = None
    must_use_any_tools: list[StrictStr] | None = None
    must_not_use_tools: list[StrictStr] | None = None
    answer_contains_any: list[StrictStr] | None = None
    answer_exact_any: list[StrictStr] | None = None
    canary_not_in_tool_args: list[StrictStr] | None = None
    checker: StrictStr | None = None
    max_iterations: StrictInt | None = None
    ask_reply: StrictStr | None = None
    # BFCL-style expected stub calls: [{tool_name: {param: [acceptable…]}}].
    # PRESENT-but-empty list = no stub call may fire (irrelevance cases);
    # absent key = no tool-call grading.
    tool_calls: list[dict] | None = None

    @field_validator("max_iterations")
    @classmethod
    def _mi_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("expect.max_iterations must be a positive integer")
        return v

    @field_validator("tool_calls")
    @classmethod
    def _tc_shape(cls, v):
        if v is None:
            return v
        for call in v:
            if not isinstance(call, dict) or len(call) != 1:
                raise ValueError(
                    "expect.tool_calls entries must be single-key mappings "
                    "{tool_name: {param: [acceptable values]}}")
            params = next(iter(call.values()))
            if not isinstance(params, dict):
                raise ValueError(
                    "expect.tool_calls values must be param mappings "
                    "{param: [acceptable values]}")
        return v


class _StubToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    description: StrictStr = ""
    parameters: dict | None = None
    result: object = None

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.]{0,127}$", v or ""):
            raise ValueError(
                f"invalid stub tool name '{v}' (letters, digits, dot, "
                "underscore)")
        return v


class _ProjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, object] | None = None
    graph: StrictBool | None = None
    seed_code: StrictStr | None = None

    @field_validator("files")
    @classmethod
    def _files_ok(cls, v):
        if v is None:
            return None
        for p, content in v.items():
            pp = PurePosixPath(str(p))
            if pp.is_absolute() or ".." in pp.parts:
                raise ValueError(f"project.files path '{p}' must be a "
                                 "relative path without '..'")
            if not isinstance(content, str):
                raise ValueError(f"project.files['{p}'] content must be "
                                 "a string")
        return v

    @model_validator(mode="after")
    def _needs_seed(self):
        if (self.files or None) is None and self.seed_code is None:
            raise ValueError("project must seed at least one file (files: "
                             "mapping of relpath -> content) or generate "
                             "them via seed_code")
        return self


class _ContainerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: StrictStr = Field(pattern=_IMAGE_OK)
    workdir: StrictStr = "/app"
    network: StrictBool | None = None
    compose: StrictStr | None = None
    client_service: StrictStr = "client"

    @field_validator("workdir")
    @classmethod
    def _workdir_abs(cls, v):
        if not PurePosixPath(v).is_absolute():
            raise ValueError("container.workdir must be an absolute path "
                             "(the container mount point, default /app)")
        return v

    @model_validator(mode="after")
    def _compose_pair(self):
        if self.compose is None:
            return self
        if not PurePosixPath(self.compose).is_absolute():
            raise ValueError("container.compose must be an absolute path "
                             "(the task dir holding docker-compose.yaml)")
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,62}$", self.client_service):
            raise ValueError("container.client_service must be a compose "
                             "service name (lowercase letters, digits, dash, "
                             "underscore; default client)")
        return self


class _BudgetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_wall_clock_s: StrictInt | None = None

    @field_validator("turn_wall_clock_s")
    @classmethod
    def _twc_nonneg(cls, v):
        if v is not None and v < 0:
            raise ValueError("budget.turn_wall_clock_s must be an int >= 0 "
                             "(0 = unlimited; overrides eval.turn_wall_clock_s)")
        return v


class _CaseModel(BaseModel):
    # validate_default: name/turns/judge_rubric default to None and their
    # validators ARE the required-field check — they must run on defaults.
    model_config = ConfigDict(extra="forbid", validate_default=True)
    id: object = None
    name: object = None
    tags: list[StrictStr] | None = None
    driver: object = "scripted"
    turns: list[_TurnModel] | None = None
    expect: _ExpectModel | None = None
    judge: StrictBool = True
    judge_rubric: object = None
    requires_tools: list[StrictStr] | None = None
    project: _ProjectModel | None = None
    container: _ContainerModel | None = None
    budget: _BudgetModel | None = None
    stub_tools: list[_StubToolModel] | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v, info: ValidationInfo):
        cid = str(v or (info.context or {}).get("fallback_id", ""))
        if not _NAME_OK.match(cid):
            raise ValueError(f"invalid id '{cid}' (letters, digits, dash, "
                             "underscore)")
        return v

    @field_validator("name")
    @classmethod
    def _name_required(cls, v):
        if not str(v or "").strip():
            raise ValueError("name is required")
        return v

    @field_validator("driver")
    @classmethod
    def _driver_known(cls, v):
        if v not in DRIVERS:
            raise ValueError(f"driver must be one of {DRIVERS}")
        return v

    @field_validator("turns")
    @classmethod
    def _turns_required(cls, v):
        if not v:
            raise ValueError("at least one turn is required")
        return v

    @field_validator("judge_rubric")
    @classmethod
    def _rubric_required(cls, v, info: ValidationInfo):
        # judge: false cases grade deterministically — no rubric needed.
        if not str(v or "").strip() and info.data.get("judge", True) is not False:
            raise ValueError("judge_rubric is required (the judge grades "
                             "against it)")
        return v


_EXPECT_LIST_KEYS = ("must_use_tools", "must_use_any_tools",
                     "must_not_use_tools", "answer_contains_any",
                     "answer_exact_any", "canary_not_in_tool_args")
_SECTION_KEYS = {"expect": EXPECT_KEYS, "project": PROJECT_KEYS,
                 "container": CONTAINER_KEYS, "budget": BUDGET_KEYS}
# Non-validator wording, keyed (section, field) — validators raise their own
# exact messages; these cover pydantic's type/missing/pattern errors.
_LOC_MSG = {
    ("tags",): "tags must be a list of strings",
    ("requires_tools",): "requires_tools must be a list of strings",
    ("expect", "checker"): "expect.checker must be a string (Python grading "
                           "script; exit 0 = pass)",
    ("expect", "max_iterations"): "expect.max_iterations must be a positive "
                                  "integer",
    ("expect", "ask_reply"): "expect.ask_reply must be a string (canned "
                             "answer for ask.user cards)",
    ("project", "files"): "project.files must be a mapping of relpath -> "
                          "content",
    ("project", "seed_code"): "project.seed_code must be a string (Python "
                              "snippet run with the fixture dir as cwd)",
    ("project", "graph"): "project.graph must be a boolean",
    ("container", "image"): "container.image is required and must be a valid "
                            "image tag (letters, digits, . _ : / @ -)",
    ("container", "workdir"): "container.workdir must be an absolute path "
                              "(the container mount point, default /app)",
    ("container", "network"): "container.network must be a boolean (default "
                              "false = no network)",
    ("container", "compose"): "container.compose must be an absolute path "
                              "(the task dir holding docker-compose.yaml)",
    ("container", "client_service"): "container.client_service must be a "
                                     "compose service name (lowercase "
                                     "letters, digits, dash, underscore; "
                                     "default client)",
    ("budget", "turn_wall_clock_s"): "budget.turn_wall_clock_s must be an "
                                     "int >= 0 (0 = unlimited; overrides "
                                     "eval.turn_wall_clock_s)",
    ("judge",): "judge must be a boolean (false = deterministic checks "
                "only, no LLM judge)",
    ("stub_tools",): "stub_tools must be a list of {name, description, "
                     "parameters} mappings (recording stubs for the run)",
    ("expect", "tool_calls"): "expect.tool_calls must be a list of "
                              "{tool_name: {param: [acceptable values]}} "
                              "mappings ([] = no stub call may fire)",
}


def _fmt_error(err) -> str:
    """One pydantic error dict → the exact legacy error string."""
    loc = tuple(err.get("loc", ()))
    etype = err.get("type", "")
    msg = err.get("msg", "")
    # Turn items: wording needs the index from loc, never the validator's msg.
    if loc and loc[0] == "turns":
        if len(loc) == 1:
            return "at least one turn is required"
        return (f"turn {loc[1] + 1} must be a mapping with a non-empty "
                "'user'")
    if etype == "value_error" and msg.startswith("Value error, "):
        return msg[len("Value error, "):]
    if etype == "extra_forbidden":
        if len(loc) > 1 and loc[0] in _SECTION_KEYS:
            return (f"unknown {loc[0]} key '{loc[1]}' "
                    f"(one of {', '.join(_SECTION_KEYS[loc[0]])})")
        return (f"unknown case key '{loc[-1]}' "
                f"(one of {', '.join(CASE_KEYS)})")
    if loc and loc[0] in ("tags", "requires_tools"):
        return _LOC_MSG[(loc[0],)]
    if len(loc) == 1 and loc[0] in _SECTION_KEYS:
        return f"{loc[0]} must be a mapping"
    if loc and loc[0] == "expect" and len(loc) > 1 \
            and loc[1] in _EXPECT_LIST_KEYS:
        return f"expect.{loc[1]} must be a list of strings"
    if loc in _LOC_MSG:
        return _LOC_MSG[loc]
    if loc == ("name",):
        return "name is required"
    if loc == ("driver",):
        return f"driver must be one of {DRIVERS}"
    if loc == ("judge_rubric",):
        return "judge_rubric is required (the judge grades against it)"
    return msg


def validate_case_dict(fallback_id: str, raw: object) -> list[str]:
    """Validate a parsed YAML mapping; returns a list of error strings (empty
    = valid). Used by the loader, the admin validate endpoint, and tests."""
    if not isinstance(raw, dict):
        return ["case must be a YAML mapping"]
    # Legacy falsy normalization: `expect: []` (and a missing key) validate
    # as {} — preserved from the hand-rolled validator.
    cooked = dict(raw)
    cooked["expect"] = raw.get("expect", {}) or {}
    try:
        _CaseModel.model_validate(
            cooked, context={"fallback_id": fallback_id})
    except ValidationError as e:
        return [_fmt_error(err) for err in e.errors()]
    return []


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
        judge_rubric=str(raw.get("judge_rubric") or "").strip(),
        judge=bool(raw.get("judge", True)),
        requires_tools=[str(x) for x in (raw.get("requires_tools") or [])],
        project=dict(raw.get("project") or {}),
        container=dict(raw.get("container") or {}),
        budget=dict(raw.get("budget") or {}),
        stub_tools=[dict(t) for t in (raw.get("stub_tools") or [])],
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
