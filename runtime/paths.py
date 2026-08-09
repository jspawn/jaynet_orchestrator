"""Canonical install and data paths — the single source of truth.

Every Python module that needs a path under the install tree or the data
directory imports from here instead of hardcoding ``/srv/orchestrator/…``.

Three env vars drive everything (set in ~/.config/jaynet.env; JAYNET_* names,
ORCH_* still read as a fallback — see runtime/env.py):

    JAYNET_HOME   — the install root  (code, config, venv, presets, skills)
    JAYNET_DATA   — runtime state     (DBs, uploads, outputs, projects, scratch)
    JAYNET_MODELS — GGUF model files  (pulled via scripts/pull-model)

Plus JAYNET_LITELLM_PORT / JAYNET_LITELLM_BASE for the proxy's address (below).

All have backward-compatible defaults so nothing breaks if the env isn't
sourced (e.g. a quick ``python -c …`` on the CLI).
"""

from __future__ import annotations

from pathlib import Path

from runtime.env import env

# ---- roots ----------------------------------------------------------------

HOME: Path = Path(env("ORCH_HOME", "/srv/orchestrator")).resolve()
DATA: Path = Path(env("ORCH_DATA", "/srv/orchestrator/data")).resolve()
MODELS_DIR: Path = Path(env("ORCH_MODELS", str(HOME / "models"))).resolve()

# ---- derived: install tree ------------------------------------------------

CONFIG:       Path = HOME / "config" / "runtime.yaml"
VENV_BIN:     Path = HOME / ".venv" / "bin"
VENV_PYTHON:  Path = VENV_BIN / "python"
SKILLS_DIR:   Path = HOME / "skills"
PRESETS_DIR:  Path = HOME / "presets"

# ---- derived: data --------------------------------------------------------

TRACE_DB:     Path = DATA / "trace.db"
CHATS_DB:     Path = DATA / "chats.db"
USERS_DB:     Path = DATA / "users.db"
RAG_DB:       Path = DATA / "rag.db"
RESEARCH_DB:  Path = DATA / "research.db"
MEMORY_DB:    Path = DATA / "memory.db"
UPLOADS_DIR:  Path = DATA / "uploads"
OUTPUTS_DIR:  Path = DATA / "outputs"
PROJECTS_DIR: Path = DATA / "projects"
WIKI_DIR:     Path = DATA / "wiki"
SCRATCH_DIR:  Path = DATA / "chat-scratch"
SANDBOX_DIR:  Path = DATA / "code-sandbox"
TEST_RUNS:    Path = DATA / "test-runs"
JOBS_DIR:     Path = DATA / "jobs"
SERVE_DIR:    Path = DATA / "serve"
WORK_DIR:     Path = DATA / "work"

# ---- customization area (Studio) -------------------------------------------
# Admin-created skills/chains/tools/connectors, layered over the repo
# built-ins (custom wins on name clash). Lives under DATA so it survives
# git-pull deploys. Dirs are created lazily on first write — absence simply
# means the custom layer is empty.

CUSTOM_DIR:        Path = DATA / "custom"
CUSTOM_SKILLS_DIR: Path = CUSTOM_DIR / "skills"       # <name>/SKILL.md (+resources)
CUSTOM_CHAINS_DIR: Path = CUSTOM_DIR / "chains"       # <name>.yaml
CUSTOM_TOOLS_DIR:  Path = CUSTOM_DIR / "tools"        # <ns>/<verb>.py
CUSTOM_CONN_DIR:   Path = CUSTOM_DIR / "connectors"   # <name>.yaml (declarative)
CUSTOM_EVALS_DIR:  Path = CUSTOM_DIR / "evals"        # <id>.yaml (eval test cases)

# Eval harness state (runtime/eval_store.py): results + improvement proposals.
EVAL_DB: Path = DATA / "eval.db"

# ---- network defaults (not paths, but also duplicated everywhere) ---------

# The LiteLLM proxy's base URL. JAYNET_LITELLM_BASE wins (remote proxy);
# otherwise derived from JAYNET_LITELLM_PORT (the systemd unit passes the same
# var to litellm --port). NOTE: runtime.yaml's orchestrator.litellm_base
# takes precedence over this — update it too (or remove it) when moving the
# proxy off :4000.
LITELLM_BASE: str = (
    env("ORCH_LITELLM_BASE")
    or f"http://127.0.0.1:{env('ORCH_LITELLM_PORT', '4000')}")
