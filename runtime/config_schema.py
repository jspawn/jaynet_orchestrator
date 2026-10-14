"""Typed schema for the highest-traffic config sections (code audit
2026-09-23, item 8 — "Untyped 461-key config"; extended 2026-10-12 batch C
with the rail sections security/loop_guard/privacy/confirmation — a typo
there silently disables a safety rail).

`config/runtime.yaml` is consumed as nested plain dicts with defaults
scattered through the code and ~45 hand-written `except (TypeError,
ValueError)` coercions; a misspelled nested key (agent.verify_delegate_chek)
was silently ignored and the rail stayed at its default. The sections
`agent`, `budgets`, `tool_selection`, `eval`, `security`, `loop_guard`,
`privacy` and `confirmation` are now modeled as pydantic
BaseModels whose field names/types/defaults exactly mirror the shipped
runtime.yaml.

Consumers STAY on plain dicts: validate_typed_sections() COERCES values in
place (the string "40" becomes the int 40 inside the config dict), so the
hand coercions downstream keep working unchanged. Failure modes are
deliberately soft (backcompat — existing live configs must not break):

- unknown nested key  → load-time WARNING with a did-you-mean hint
  (difflib.get_close_matches); the key passes through untouched (it may be
  forward-compatible)
- uncoercible value   → WARNING; the raw value is kept (current behavior)
  instead of crashing

Free-form maps (role_temperature, keyword_namespaces, …) are typed as dicts,
not closed models — their keys are user data, not schema.
"""
from __future__ import annotations

import difflib
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError


class _Section(BaseModel):
    # Extra keys pass through untouched (forward compatibility); they are
    # reported by validate_typed_sections() as warnings, never rejected.
    model_config = ConfigDict(extra="allow")


# ---- budgets ---------------------------------------------------------------

class BudgetsConfig(_Section):
    max_iterations: int = 60
    max_wall_clock_s: int = 0          # 0 = disabled
    wall_clock_grace_s: int = 0
    wall_clock_max_extensions: int = 0
    stall_s: int = 180
    max_cost_usd: float = 1.00
    max_total_tokens: int = 1000000
    cached_token_weight: float = 0.1
    warn_fraction: float = 0.8
    final_warn_fraction: float = 0.95


# ---- eval ------------------------------------------------------------------

class EvalConfig(_Section):
    enabled: bool = True
    max_cost_usd: float = 0.50
    suite_max_cost_usd: float = 2.00
    benchmark_max_cost_usd: float = 10.00
    judge_model: str = "local-specialist"
    driver_model: str = "local-specialist"
    adaptive_max_turns: int = 6
    judge_temperature: float = 0.0
    judge_timeout_s: int = 600
    turn_wall_clock_s: int = 1800
    wall_clock_grace_s: int = 120
    wall_clock_max_extensions: int = 5
    verify_gate: bool = True
    verify_max_checks: int = 3
    # A case whose turns were served by a fallback model measured the wrong
    # model: record it as status=invalid (kept, but excluded from tallies).
    invalid_on_fallback: bool = True


# ---- tool_selection ----------------------------------------------------------

class RoutingNudgeConfig(_Section):
    enabled: bool = True
    code_keywords: list[str] = [
        "implement", "refactor", "debug", "compile", "traceback", "pytest",
        "write a function", "write a script", "shell script",
        "source code", "code review", "fix this code", "patch the",
        "unit test",
    ]
    strength_keywords: dict[str, list[str]] = {
        "security": ["vulnerability", "vuln", "exploit", "pentest",
                     "pen test", "cve", "sql injection", "xss",
                     "privilege escalation", "malware", "forensic",
                     "security audit", "rce", "reverse shell", "intrusion",
                     "incident response", "security threat",
                     "threat detection", "capture the flag"],
    }


class ToolSelectionConfig(_Section):
    mode: str = "auto"
    core_namespaces: list[str] = [
        "web.search", "web.fetch", "ask.user", "skill.load", "note.set",
        "todos", "run.badge", "deliver.files", "memory.search",
        "fs.list", "fs.read", "llm.call", "tools.load",
    ]
    max_expansions: int = 2
    # Keyword matching also scans the last N turns of the chat (bounded) so a
    # loaded namespace stays sticky across keyword-free follow-ups. 0 disables.
    history_turns: int = 3
    routing_nudge: RoutingNudgeConfig = RoutingNudgeConfig()
    full_toolset_keywords: list[str] = [
        "selftest", "self-test", "smoke test", "test the tools",
        "test all tools", "all the tools work",
    ]
    keyword_namespaces: dict[str, list[str]] = {
        "code": ["code", "implement", "function", "class", "build", "fix",
                 "bug", "refactor", "debug", "patch", "diff", "script",
                 "test", "pytest", "lint", "ruff", "mypy", "format",
                 "delegate", "offload", "coder", "compile", "error",
                 "traceback", "exception", "symbol", "tree", "deps", "venv",
                 "dependency", "install", "run", "execute", "calculate",
                 "compute", "math", "regex", "parse", "convert"],
        "lint": ["lint", "ruff", "mypy", "format", "type check", "eslint",
                 "flake8", "black"],
        "test": ["unit test", "write test", "test suite", "pytest",
                 "run tests", "asgi", "test coverage"],
        "architect": ["architect", "plan", "complex", "break down",
                      "decompose", "design", "multi-file", "large task"],
        "fs": ["write", "edit", "save", "create", "modify", "grep",
               "replace", "find and replace", "locate", "file", "folder",
               "directory", "move", "rename", "copy", "delete", "archive",
               "zip", "tar", "extract", "unzip", "untar", "pdf"],
        "archives": ["archive", "zip", "tar", "compress", "extract",
                     "unzip", "untar", "bundle"],
        "pdf": ["pdf", "portable document"],
        "git": ["git", "commit", "branch", "stash", "push", "pull", "fetch",
                "merge", "pull request", "checkout", "restore", "rebase",
                "worktree", "work tree", "diff", "status", "log", "blame"],
        "research": ["research", "deep dive", "investigate", "look into",
                     "find everything", "dig into", "thorough",
                     "sourced report", "literature", "survey", "deep-dive"],
        "web": ["scrape", "extract", "crawl", "structured data", "paginated",
                "render", "headless", "screenshot", "screen shot", "capture",
                "snapshot", "arxiv", "paper", "academic paper", "api",
                "rest", "webhook", "http request", "endpoint"],
        "browser": ["screenshot", "screen shot", "capture", "page to pdf",
                    "save as pdf", "snapshot", "render"],
        "arxiv": ["arxiv", "paper", "academic paper", "preprint",
                  "machine learning paper"],
        "serve": ["serve", "server", "model", "preset", "start model",
                  "stop model", "model list", "swap model", "slot", "gpu",
                  "vram", "load model"],
        "model": ["model", "preset", "catalog", "model list",
                  "switch model", "swap"],
        "ops": ["ops", "status", "systemd", "service", "health",
                "is it running", "uptime", "rocm", "systemctl"],
        "gpu": ["gpu", "vram", "graphics card", "video memory"],
        "job": ["job", "launch", "detach", "background", "long running",
                "queue"],
        "eval": ["eval", "compare", "benchmark", "side by side",
                 "model comparison", "self test", "self-test",
                 "test yourself", "eval case", "behaviour test"],
        "council": ["council", "debate", "vote", "majority vote",
                    "self-consistency", "second opinion", "pros and cons",
                    "trade-off", "tradeoff", "should we", "deliberate",
                    "panel", "adversarial"],
        "agent": ["fanout", "fan out", "in parallel", "parallelize",
                  "map-reduce", "map reduce", "several subtasks",
                  "parallel subtasks", "spawn", "subagent", "sub-agent"],
        "schedule": ["schedule", "remind", "reminder", "cron",
                     "every morning", "every day", "every hour",
                     "in an hour", "later today", "recurring"],
        "rag": ["rag", "knowledge base", "retrieve", "retrieval", "indexed",
                "collection", "embeddings", "vector store", "ingest"],
        "kg": ["knowledge", "graph", "relation", "entity",
               "knowledge graph", "ontology", "link between"],
        "memory": ["remember", "forget", "memory", "save for later",
                   "note for next time", "preference", "memorize",
                   "don't forget", "did we", "do you recall", "have we",
                   "we discussed", "we talked about"],
        "docs": ["summarize", "summarise", "digest", "bulk document",
                 "all documents", "whole folder", "every file"],
        "context": ["stage", "too big", "too long", "oversized",
                    "out of context"],
        "image": ["image", "picture", "draw", "drawing", "paint",
                  "painting", "illustration", "artwork", "poster", "logo",
                  "wallpaper", "avatar", "text-to-image"],
        "audio": ["speak", "speech", "say it", "read aloud",
                  "read this aloud", "voice", "voice message", "tts",
                  "text to speech", "text-to-speech", "podcast",
                  "narration", "narrate", "transcribe", "transcription",
                  "voice note"],
        "doc": ["pageindex", "tree index", "document index", "long pdf",
                "search this document", "search the report", "document qa"],
        "verify": ["verify", "score", "rank", "probe", "judge", "evaluate",
                   "quality", "good enough", "check quality", "fable",
                   "fable method", "fable loop", "fable judge", "prove it",
                   "did that work", "audit"],
        "trace": ["trace", "what went wrong", "last run", "why did", "debug",
                  "earlier run", "history", "previous run", "failure",
                  "failed", "yesterday", "last time", "last week",
                  "last session", "this morning", "earlier today", "recent",
                  "what did we", "what happened", "patterns", "recurring",
                  "workflow"],
        "mcp": ["mcp", "model context protocol", "mcp server", "mcp tool"],
        "chain": ["chain", "pipeline", "multi-step pipeline",
                  "run the chain"],
        "procedure": ["procedure", "macro", "saved script",
                      "reusable script", "run the procedure",
                      "same calculation", "same conversion", "rerun",
                      "re-run"],
    }
    max_tools: int | None = None


# ---- agent -----------------------------------------------------------------

class DeliverableCheckConfig(_Section):
    enabled: bool = True
    warn_at: float = 0.75


class StallCheckConfig(_Section):
    enabled: bool = True
    after: int = 2


class StrengthGateConfig(_Section):
    enabled: bool = True


class FreshRetryConfig(_Section):
    enabled: bool = True
    after: int = 2


class ProcedureSelectorConfig(_Section):
    enabled: bool = True
    shapes: dict[str, list[str]] = {
        "implement-from-spec": ["implement the", "research paper",
                                "from the paper", "from scratch",
                                "passes the test", "write /app",
                                "create /app", "convert the"],
        "debug-and-fix": ["failing test", "tests fail", "test fails",
                          "fix the bug", "debug the", "broken build",
                          "fix the failing", "bug in the"],
        "research-and-verify": ["find the official", "official codebase",
                                "official repo", "official implementation",
                                "look up the", "find the source"],
    }


class AnchorConfig(_Section):
    mode: str = "off"
    todos_reinject: str = "trailing"
    budget: bool = True


class StateFileConfig(_Section):
    enabled: bool = False
    max_chars: int = 8000
    instructions: str = ""          # "" = built-in default (runtime/loop.py)


class AgentVerifyConfig(_Section):
    max_checks: int = 4
    stall_after: int = 2
    timeout_s: int = 180
    protect: list[str] = [
        "**/test_*.py", "**/*_test.py", "**/tests/**/*.py", "**/conftest.py",
    ]
    unprotect: list[str] = []          # [] = no caller-declared tamper exceptions


class AgentConfig(_Section):
    max_depth: int = 2
    default_sub_iterations: int = 16
    final_synthesis: bool = True
    deliverable_check: DeliverableCheckConfig = DeliverableCheckConfig()
    stall_check: StallCheckConfig = StallCheckConfig()
    exactness_gate: bool = True
    exactness_keywords: list[str] = []     # [] = built-ins
    verify_delegate_check: bool = True
    verify_delegate_authored_check: bool = True
    verify_delegate_review: bool = True
    just_reply_check: bool = True
    just_reply_keywords: list[str] = []    # [] = built-ins
    max_bounces_per_answer: int = 3
    strength_gate: StrengthGateConfig = StrengthGateConfig()
    worker_prompt: bool = True
    worker_prompts: dict[str, str] = {}
    role_temperature: dict[str, float] = {
        "coding": 0.2, "security": 0.2, "reasoning": 0.3,
        "research": 0.4, "creative": 0.8, "osint": 0.4,
        "bugtrace": 0.2,
    }
    fresh_retry: FreshRetryConfig = FreshRetryConfig()
    procedure_selector: ProcedureSelectorConfig = ProcedureSelectorConfig()
    anchor: AnchorConfig = AnchorConfig()
    state_file: StateFileConfig = StateFileConfig()
    verify: AgentVerifyConfig = AgentVerifyConfig()
    default_budget: dict[str, Any] | None = None


# ---- security / loop_guard / privacy / confirmation (batch C) -----------------
# The rail sections: a typo here silently disables a safety rail (the audit's
# core concern), so these are typed ahead of the free-form tool maps.

class SecurityConfig(_Section):
    # Operator extension surface on top of DEFAULT_ADMIN_ONLY_TOOLS
    # (runtime/loop.py) and the declared Tool.admin_only tier.
    admin_only_tools: list[str] = [
        "ops.run", "job.*", "serve.*", "model.use", "model.measure",
        "git.push", "mcp.call", "test.run", "code.deps",
    ]


class LoopGuardConfig(_Section):
    max_rejections: int = 6
    near_dup_threshold: float = 0.75
    near_dup_tools: list[str] = ["web.search", "web.fetch", "arxiv.search"]
    failure_nudge_after: int = 3
    failure_nudge_tools: list[str] = ["code.run", "code.execute", "code.check"]
    hard_block_repeat_errors: int = 3
    stall_hard_stop: bool = True
    jspace_badge_gate: bool = True
    host_give_up_after: int = 4
    delegate_nudge_after: int = 3
    delegate_enforce: bool = False
    delegate_escalate: bool = True
    stuck_delegate_after: int = 3
    auto_delegate_after: int = 2


class PrivacyConfig(_Section):
    private_tool_namespaces: list[str] = []
    remote_llm_tools: list[str] = ["llm.call"]


class ConfirmationConfig(_Section):
    enabled: bool = True
    non_interactive: str = "deny"
    confirm_cloud_calls: bool = True


# ---- web / models / processes (batch C, stage 2) ------------------------------
# web is flat (all scalars); models/processes have free-form nested maps
# (presets, strengths, binaries, per-slot dicts) whose keys are user data —
# those are typed as dicts, not closed models. Slot/preset INTERNAL keys are
# deliberately not schema-typed: the drift guard pins the shipped defaults.

class WebConfig(_Section):
    tool_preview_chars: int = 8000
    chats_db: str = "chats.db"
    users_db: str = "users.db"
    cookie_secure: bool = False
    uploads_dir: str = "uploads"
    max_upload_mb: int = 25
    max_restore_mb: int = 1024
    outputs_dir: str = "outputs"
    output_ttl_hours: int = 24
    max_output_mb: int = 200
    projects_dir: str = "projects"
    max_project_file_mb: int = 25
    chat_scratch_dir: str = "chat-scratch"
    chat_scratch_ttl_hours: int = 336


class ModelsConfig(_Section):
    presets_db: str = "presets.db"
    gpus: list[str] = ["0", "1"]
    default_posture: str = "brain+specialist"
    swap_back: bool = True
    min_free_ram_gib: float = 2.0
    boot: list[Any] | None = None      # boot_posture steps; null = default set
    strengths: dict[str, str] = {
        "bugtrace": "bug tracing and root-cause analysis — stack traces, "
                    "error/log forensics, crash triage, regression hunts",
        "coding": "code synthesis, debugging, refactors, test fixing",
        "creative": "creative and everyday writing — stories, poems, "
                    "emails, letters, messages, speeches, tone-sensitive "
                    "rewrites",
        "multi-step": "multi-step reasoning chains, puzzles, long "
                      "sequential computations",
        "osint": "open-source intelligence, threat intel, investigative "
                 "research — source grading, attribution, geolocation, "
                 "passive recon",
        "reasoning": "math, logic, planning, tricky deduction",
        "research": "literature and web research, analysis, synthesis",
        "security": "security research, exploit analysis, pentesting",
        "vision": "image understanding",
    }
    presets: dict[str, Any] = {
        "brain": {"alias": "local-orchestrator", "gpu": "0", "port": 8090,
                  "preset": "presets/brain-qwen3-4b.conf",
                  "role": "default brain (Qwen3-4B, Apache-2.0) - the "
                          "orchestrator driver; swap for a bigger model in "
                          "admin → Models → Presets",
                  "served_id": "qwen3-4b",
                  "strengths": ["reasoning", "orchestration"], "vram_gib": 4},
        "brain-moe": {"alias": "local-orchestrator", "gpu": "0", "port": 8090,
                      "preset": "presets/brain-qwen3-30b-a3b-moe.conf",
                      "role": "example MoE brain (Qwen3-30B-A3B, Apache-2.0) - "
                              "~3B active params = small-model speed, "
                              "big-model knowledge; the all-day brain once "
                              "you have a 24 GB card",
                      "served_id": "qwen3-30b-a3b",
                      "strengths": ["reasoning", "orchestration"],
                      "vram_gib": 20},
        "embed": {"gpu": "", "port": 8095,
                  "preset": "presets/embed-qwen3-0.6b.conf",
                  "role": "default RAG embedding server (CPU-only, "
                          "Qwen3-Embedding-0.6B, Apache-2.0)",
                  "served_id": "qwen3-embedding-0.6b", "vram_gib": 0},
        "rerank": {"gpu": "", "port": 8096,
                   "preset": "presets/rerank-qwen3-0.6b.conf",
                   "role": "RAG reranker server (CPU-only, "
                         "Qwen3-Reranker-0.6B Q8_0)",
                   "served_id": "qwen3-reranker-0.6b", "vram_gib": 0},
        "specialist": {"alias": "local-specialist", "gpu": "1", "port": 8080,
                       "preset": "presets/specialist-qwen2.5-coder-32b-dense.conf",
                       "role": "example dense specialist (Qwen2.5-Coder-32B, "
                               "Apache-2.0) - slower per token than MoE but "
                               "stronger per token; the specialist.delegate "
                               "target",
                       "served_id": "qwen2.5-coder-32b",
                       "strengths": ["coding", "reasoning"], "vram_gib": 22},
    }
    binaries: dict[str, Any] = {}
    # Runtime-injected by preset_store.load_into_config (DB state layered
    # over the yaml): slot→preset assignment + probed GPU topology. Absent
    # from the shipped runtime.yaml — excluded from the drift test's dump
    # comparison (RUNTIME_INJECTED in tests/test_config_schema.py).
    slots: dict[str, str] = {}
    gpu_info: dict[str, Any] = {}
    # Runtime-injected by cloud_store.load_into_config (the cloud_models DB
    # table): friendly name → litellm alias map. Same exclusion.
    cloud: dict[str, Any] = {}


class ProcessesConfig(_Section):
    brain: dict[str, Any] = {"command": "scripts/start-model.sh brain",
                             "kill_signal": 15, "restart": True,
                             "restart_delay": 10}
    specialist: dict[str, Any] = {"command": "scripts/start-model.sh specialist",
                                  "kill_signal": 15, "restart": True,
                                  "restart_delay": 10, "start_delay": 20}
    specialist2: dict[str, Any] = {"command": "scripts/start-model.sh specialist2",
                                   "kill_signal": 15, "restart": True,
                                   "restart_delay": 10, "start_delay": 25}
    specialist3: dict[str, Any] = {"command": "scripts/start-model.sh specialist3",
                                   "kill_signal": 15, "restart": True,
                                   "restart_delay": 10, "start_delay": 30}
    embed: dict[str, Any] = {"command": "scripts/start-model.sh embed",
                             "env": {"HIP_VISIBLE_DEVICES": ""},
                             "kill_signal": 15, "restart": True,
                             "restart_delay": 10, "start_delay": 5}
    rerank: dict[str, Any] = {"command": "scripts/start-model.sh rerank",
                              "env": {"HIP_VISIBLE_DEVICES": ""},
                              "kill_signal": 15, "restart": True,
                              "restart_delay": 10, "start_delay": 10}
    vision: dict[str, Any] = {"command": "scripts/start-model.sh vision",
                              "env": {"HIP_VISIBLE_DEVICES": ""},
                              "kill_signal": 15, "restart": True,
                              "restart_delay": 10, "start_delay": 15}
    stt: dict[str, Any] = {"command": "scripts/start-model.sh stt",
                           "env": {"HIP_VISIBLE_DEVICES": ""},
                           "kill_signal": 15, "restart": True,
                           "restart_delay": 10, "start_delay": 20}


#: The typed sections: top-level key -> model. Every key the shipped
#: runtime.yaml carries in these sections MUST have a field here —
#: tests/test_config_schema.py guards the drift both ways.
SECTION_MODELS: dict[str, type[_Section]] = {
    "agent": AgentConfig,
    "budgets": BudgetsConfig,
    "tool_selection": ToolSelectionConfig,
    "eval": EvalConfig,
    "security": SecurityConfig,
    "loop_guard": LoopGuardConfig,
    "privacy": PrivacyConfig,
    "confirmation": ConfirmationConfig,
    "web": WebConfig,
    "models": ModelsConfig,
    "processes": ProcessesConfig,
}


def _adapters(model: type[_Section]) -> dict[str, TypeAdapter]:
    return {name: TypeAdapter(f.annotation)
            for name, f in model.model_fields.items()}


# TypeAdapter is cheap but not free — built once per model, lazily (the
# top-level section models' nested models register on first use).
_ADAPTERS: dict[type[_Section], dict[str, TypeAdapter]] = {}


def _adapters_for(model: type[_Section]) -> dict[str, TypeAdapter]:
    ad = _ADAPTERS.get(model)
    if ad is None:
        ad = _ADAPTERS[model] = _adapters(model)
    return ad


def _validate_into(path: str, data: dict, model: type[_Section],
                   warnings: list[str]) -> None:
    """Coerce one section dict in place against its model; collect soft
    warnings for unknown keys (did-you-mean hint) and uncoercible values
    (raw value kept). Recurses into nested model fields so a typo at any
    depth (agent.stall_check.aftr) is caught."""
    fields = model.model_fields
    adapters = _adapters_for(model)
    for key in data:
        if key not in fields:
            hint = ""
            close = difflib.get_close_matches(str(key), list(fields), n=1)
            if close:
                hint = f" — did you mean '{close[0]}'?"
            warnings.append(f"{path}.{key}: unknown config key{hint} "
                            "(kept as-is)")
    for name, field in fields.items():
        if name not in data:
            continue
        value = data[name]
        ann = field.annotation
        if (isinstance(ann, type) and issubclass(ann, _Section)
                and isinstance(value, dict)):
            _validate_into(f"{path}.{name}", value, ann, warnings)
            continue
        try:
            data[name] = adapters[name].validate_python(value)
        except ValidationError:
            warnings.append(f"{path}.{name}: value {value!r} has the wrong "
                            f"type (kept as-is)")


def validate_typed_sections(config: dict, log=None) -> list[str]:
    """Validate + coerce the four typed sections of an assembled config
    IN PLACE (consumers stay on plain dicts). Returns the warning strings;
    with `log`, also logs each as a warning. Never raises on bad config —
    unknown keys and uncoercible values pass through untouched (backcompat)."""
    warnings: list[str] = []
    if not isinstance(config, dict):
        return warnings
    for section, model in SECTION_MODELS.items():
        data = config.get(section)
        if isinstance(data, dict):
            _validate_into(section, data, model, warnings)
    if log is not None:
        for w in warnings:
            log.warning("config: %s", w)
    return warnings
