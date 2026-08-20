"""Build runner + status store for the graphify plugin.

One graph per project, stored at <project>/graphify-out/ (graphify's own
output layout — incremental rebuilds need its manifest, so the name stays).
The whole directory sits inside the project dir, so deleting the project
deletes the graph with it.

Builds run as asyncio tasks on the web process loop (one per project at a
time), driving the graphify CLI as a subprocess:

    python -m graphify extract <files> --out <project> --token-budget …
    python -m graphify cluster-only <files> --graph <project>/graphify-out/graph.json

Code extraction is local AST (tree-sitter, no LLM). The semantic pass over
docs/PDFs talks to plugins.graphify.model via OPENAI_BASE_URL pointing at the
local LiteLLM proxy — nothing leaves the box unless the admin deliberately
points that alias at a cloud model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GRAPH_DIRNAME = "graphify-out"
_BUILD_TIMEOUT_S = 3600

# (owner, pid) -> asyncio.Task running the current build
_jobs: dict[tuple[str, str], asyncio.Task] = {}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def graph_root(projects_dir: str | Path, owner: str | None, pid: str) -> Path:
    return Path(projects_dir) / (owner or "_token") / pid / GRAPH_DIRNAME


def project_dir(projects_dir: str | Path, owner: str | None, pid: str) -> Path:
    return Path(projects_dir) / (owner or "_token") / pid


def read_status(projects_dir: str | Path, owner: str | None, pid: str) -> dict[str, Any]:
    """The project's graph status; state 'none' when never built."""
    root = graph_root(projects_dir, owner, pid)
    st: dict[str, Any] = {"state": "none", "dirty": False}
    sf = root / "status.json"
    if sf.is_file():
        try:
            st.update(json.loads(sf.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if (root / "graph.json").is_file() and st.get("state") == "none":
        st["state"] = "ready"
    st["building"] = _jobs.get((owner or "_token", pid)) is not None \
        and not _jobs[(owner or "_token", pid)].done()
    if st["building"]:
        st["state"] = "building"
    return st


def write_status(projects_dir: str | Path, owner: str | None, pid: str,
                 **fields: Any) -> None:
    root = graph_root(projects_dir, owner, pid)
    root.mkdir(parents=True, exist_ok=True)
    st = read_status(projects_dir, owner, pid)
    st.pop("building", None)
    st.update(fields)
    (root / "status.json").write_text(json.dumps(st, indent=2), encoding="utf-8")


def mark_dirty(projects_dir: str | Path, owner: str | None, pid: str) -> None:
    """Flag the graph stale (a project file changed). Cheap; no rebuild."""
    if (graph_root(projects_dir, owner, pid) / "graph.json").is_file():
        try:
            write_status(projects_dir, owner, pid, dirty=True)
        except OSError:
            pass


def _graph_counts(graph_json: Path) -> tuple[int, int]:
    try:
        data = json.loads(graph_json.read_text(encoding="utf-8"))
        return len(data.get("nodes") or []), len(data.get("edges") or [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0, 0


def _build_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    base = str((config.get("orchestrator") or {}).get("litellm_base")
               or "http://127.0.0.1:4000").rstrip("/")
    env["OPENAI_BASE_URL"] = base + "/v1"
    # Local servers accept any non-empty key; the proxy may be keyless on
    # localhost (see runtime/model_client._auth_headers).
    env["OPENAI_API_KEY"] = os.environ.get("LITELLM_MASTER_KEY") or "sk-local"
    env["PYTHONUNBUFFERED"] = "1"
    return env


async def _run(cmd: list[str], env: dict[str, str], cwd: Path,
               log_lines: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout is not None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            log_lines.append(line)
            del log_lines[:-40]                    # keep the tail only
        return await proc.wait()
    finally:
        # Cancel/timeout must not orphan the extractor subprocess.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def _build(projects_dir: str | Path, owner: str | None, pid: str,
                 config: dict[str, Any]) -> None:
    pcfg = (config.get("plugins") or {}).get("graphify") or {}
    model = str(pcfg.get("model") or "local-specialist")
    token_budget = str(int(pcfg.get("token_budget") or 4000))
    concurrency = str(int(pcfg.get("max_concurrency") or 2))
    proj = project_dir(projects_dir, owner, pid)
    files = proj / "files"
    root = graph_root(projects_dir, owner, pid)
    env = _build_env(config)
    env["OPENAI_MODEL"] = model
    log_lines: list[str] = []
    try:
        rc = await asyncio.wait_for(_run([
            sys.executable, "-m", "graphify", "extract", str(files),
            "--out", str(proj),
            "--token-budget", token_budget,
            "--max-concurrency", concurrency,
        ], env, proj, log_lines), timeout=_BUILD_TIMEOUT_S)
        if rc != 0:
            raise RuntimeError(f"extract exited {rc}")
        # Report + interactive viz; community naming via LLM only when the
        # admin opts in (plugins.graphify.label_communities).
        cluster_cmd = [
            sys.executable, "-m", "graphify", "cluster-only", str(files),
            "--graph", str(root / "graph.json"),
        ]
        if not pcfg.get("label_communities"):
            cluster_cmd.append("--no-label")
        rc = await asyncio.wait_for(
            _run(cluster_cmd, env, proj, log_lines), timeout=600)
        if rc != 0:
            log.warning("graphify cluster-only exited %d for %s (graph.json "
                        "still usable)", rc, pid)
        nodes, edges = _graph_counts(root / "graph.json")
        write_status(projects_dir, owner, pid,
                     state="ready", dirty=False, finished_at=_now(),
                     nodes=nodes, edges=edges, error="",
                     log_tail="\n".join(log_lines[-10:]))
        log.info("graphify build done for project %s: %d nodes, %d edges",
                 pid, nodes, edges)
    except Exception as e:
        write_status(projects_dir, owner, pid,
                     state="error", finished_at=_now(), error=str(e),
                     log_tail="\n".join(log_lines[-10:]))
        log.error("graphify build failed for project %s: %s", pid, e)


def start_build(projects_dir: str | Path, owner: str | None, pid: str,
                config: dict[str, Any]) -> tuple[bool, str]:
    """Kick off a background build. (False, reason) if one is already running
    or the project/files dir is missing."""
    proj = project_dir(projects_dir, owner, pid)
    if not (proj / "project.json").is_file():
        return False, "no such project"
    if not (proj / "files").is_dir():
        return False, "project has no files yet"
    key = (owner or "_token", pid)
    task = _jobs.get(key)
    if task is not None and not task.done():
        return False, "build already running"
    write_status(projects_dir, owner, pid, state="building", started_at=_now(),
                 error="")
    _jobs[key] = asyncio.create_task(_build(projects_dir, owner, pid, config))
    return True, "build started"


def cancel_build(owner: str | None, pid: str) -> bool:
    task = _jobs.get((owner or "_token", pid))
    if task is not None and not task.done():
        task.cancel()
        return True
    return False
