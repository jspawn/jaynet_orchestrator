"""Canonical install and data paths — the single source of truth.

Every Python module that needs a path under the install tree or the data
directory imports from here instead of hardcoding ``/srv/orchestrator/…``.

Two env vars drive everything (set in ~/.config/orchestrator.env):

    ORCH_HOME  — the install root  (code, config, venv, presets, skills)
    ORCH_DATA  — runtime state     (DBs, uploads, outputs, projects, scratch)

Both have backward-compatible defaults so nothing breaks if the env isn't
sourced (e.g. a quick ``python -c …`` on the CLI).
"""

from __future__ import annotations

import os
from pathlib import Path

# ---- roots ----------------------------------------------------------------

HOME: Path = Path(os.environ.get("ORCH_HOME", "/srv/orchestrator")).resolve()
DATA: Path = Path(os.environ.get("ORCH_DATA", "/srv/orchestrator/data")).resolve()

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

# ---- network defaults (not paths, but also duplicated everywhere) ---------

LITELLM_BASE: str = "http://127.0.0.1:4000"
