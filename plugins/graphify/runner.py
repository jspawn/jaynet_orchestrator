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
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GRAPH_DIRNAME = "graphify-out"
_BUILD_TIMEOUT_S = 3600

# (owner, pid) -> asyncio.Task running the current build
_jobs: dict[tuple[str, str], asyncio.Task] = {}

# (owner, pid) -> asyncio.Task of a pending debounced auto-rebuild
_timers: dict[tuple[str, str], asyncio.Task] = {}

# The runtime config, stashed by routes.register (and hot-enable) so the
# on_project_file_changed hook — which gets no config argument — can gate
# and drive auto-rebuilds.
_CONFIG: dict[str, Any] | None = None


def set_config(config: dict[str, Any]) -> None:
    """Stash the live runtime config for the auto-rebuild hook path."""
    global _CONFIG
    _CONFIG = config


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
    key = (owner or "_token", pid)
    task = _jobs.get(key)
    if task is not None and task.done():
        _jobs.pop(key, None)          # prune finished builds; status.json is truth
        task = None
    st["building"] = task is not None
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
    # Atomic write (temp + rename) — same pattern as core state files; a torn
    # status.json must never surface to a concurrent reader.
    tmp = root / "status.json.tmp"
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, root / "status.json")


def mark_dirty(projects_dir: str | Path, owner: str | None, pid: str) -> bool:
    """Flag the graph stale (a project file changed). Cheap; no rebuild.
    Returns True only when a graph exists and was flagged — never-built
    projects stay 'none' (no consent to pay build cost), which the
    auto-rebuild hook uses as its gate."""
    if (graph_root(projects_dir, owner, pid) / "graph.json").is_file():
        try:
            write_status(projects_dir, owner, pid, dirty=True)
            return True
        except OSError:
            pass
    return False


def _graph_counts(graph_json: Path) -> tuple[int, int]:
    try:
        data = json.loads(graph_json.read_text(encoding="utf-8"))
        return len(data.get("nodes") or []), len(data.get("edges") or [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0, 0


def load_graph(groot: Path) -> dict | None:
    """Parsed graph.json ({"nodes": [...], "edges": [...]}) or None when
    absent/unreadable. Accepts both 'edges' and 'links' — networkx
    node-link JSON uses 'links' and some graphify versions emit it."""
    try:
        data = json.loads((Path(groot) / "graph.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None
    if not isinstance(data.get("edges"), list):
        data["edges"] = data.get("links") if isinstance(data.get("links"), list) else []
    return data


# ---- wiki extractor ------------------------------------------------------------
# The upstream CLI maps docs only through its LLM semantic pass — there is no
# deterministic extractor for the JayNet project wiki (<project>/files/wiki/).
# augment_with_wiki adds exactly that: one node per page plus page-link edges,
# appended between extract and cluster-only so wiki nodes get community
# assignments and land in the report/viz for free.

_WIKI_LINK_MD = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_WIKI_LINK_BRACKET = re.compile(r"\[\[([^\]]+)\]\]")


def _kebab(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def augment_with_wiki(graph_json: Path, wiki_dir: Path) -> int:
    """Append wiki-page nodes + link edges to a freshly extracted graph.json.
    Nodes: id 'wiki/<rel-path>', kind 'wiki', file '<rel>.md', title from the
    first '# ' line. Edges: [text](page.md) links (relative .md targets only)
    and [[Page Name]] links (resolved by page stem) between EXISTING pages —
    relation 'references', confidence 'EXTRACTED' (deterministic, unlike the
    LLM-derived doc nodes). Idempotent; returns added node count; 0 and no
    write when there's no wiki or the graph is unreadable — never breaks a
    build."""
    wiki_dir = Path(wiki_dir)
    if not wiki_dir.is_dir():
        return 0
    graph_json = Path(graph_json)
    try:
        data = json.loads(graph_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    nodes = data.setdefault("nodes", [])
    if not isinstance(nodes, list):
        return 0
    edge_key = "edges" if isinstance(data.get("edges"), list) else "links"
    edges = data.setdefault(edge_key, [])
    have_nodes = {str(n.get("id")) for n in nodes}
    have_edges = {(str(e.get("source")), str(e.get("target")),
                   str(e.get("relation"))) for e in edges}

    pages: dict[str, dict] = {}   # node id -> {md, title, text}
    stems: dict[str, str] = {}    # kebab stem -> node id (for [[links]])
    for md in sorted(wiki_dir.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = md.relative_to(wiki_dir).with_suffix("").as_posix()
        title = ""
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        nid = f"wiki/{rel}"
        pages[nid] = {"md": md, "title": title, "text": text}
        stems[_kebab(md.stem)] = nid
    if not pages:
        return 0

    added = 0
    for nid, page in pages.items():
        if nid in have_nodes:
            continue
        nodes.append({"id": nid, "kind": "wiki",
                      "file": page["md"].relative_to(wiki_dir).as_posix(),
                      "title": page["title"] or page["md"].stem})
        have_nodes.add(nid)
        added += 1

    wiki_root = wiki_dir.resolve()
    for nid, page in pages.items():
        targets: set[str] = set()
        for m in _WIKI_LINK_MD.finditer(page["text"]):
            href = m.group(1).split("#", 1)[0]
            if not href.endswith(".md") or ":" in href.split("/", 1)[0]:
                continue                    # external, anchors, mailto:
            resolved = (page["md"].parent / href).resolve()
            try:
                rel = resolved.relative_to(wiki_root).with_suffix("").as_posix()
            except ValueError:
                continue                    # link escapes the wiki dir
            tid = f"wiki/{rel}"
            if tid in pages:
                targets.add(tid)
        for m in _WIKI_LINK_BRACKET.finditer(page["text"]):
            tid = stems.get(_kebab(m.group(1)))
            if tid:
                targets.add(tid)
        targets.discard(nid)                # no self-loops
        for tid in sorted(targets):
            key = (nid, tid, "references")
            if key not in have_edges:
                edges.append({"source": nid, "target": tid,
                              "relation": "references",
                              "confidence": "EXTRACTED"})
                have_edges.add(key)

    tmp = graph_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, graph_json)
    return added


def _build_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    base = str((config.get("orchestrator") or {}).get("litellm_base")
               or "http://127.0.0.1:4000").rstrip("/")
    env["OPENAI_BASE_URL"] = base + "/v1"
    # Local servers accept any non-empty key; the proxy may be keyless on
    # localhost (see runtime/model_client._auth_headers).
    env["OPENAI_API_KEY"] = os.environ.get("LITELLM_MASTER_KEY") or "sk-local"
    env["PYTHONUNBUFFERED"] = "1"
    # Output cap per semantic call — THE speed lever on local models: the
    # extractor generates until this cap, so on a slow dense model a high cap
    # means minutes per doc chunk (measured: 8192 out ≈ 8 min on a 27B dense).
    pcfg = (config.get("plugins") or {}).get("graphify") or {}
    env["GRAPHIFY_MAX_OUTPUT_TOKENS"] = str(int(pcfg.get("max_output_tokens")
                                                  or 8192))
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
        # Deterministic wiki extractor (opt-out via plugins.graphify.
        # wiki_nodes): one node per page + link edges, appended BEFORE
        # cluster-only so wiki nodes get communities and land in the
        # report/viz. Free (no LLM) and never fatal.
        if pcfg.get("wiki_nodes", True):
            try:
                added = augment_with_wiki(root / "graph.json", files / "wiki")
                if added:
                    log_lines.append(f"wiki extractor: +{added} page nodes")
            except Exception as e:
                log.warning("graphify wiki augmentation failed for %s: %s",
                            pid, e)
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
    cancel_rebuild_timer(owner, pid)   # a starting build settles the debt
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


# ---- debounced auto-rebuild --------------------------------------------------
# plugins.graphify.auto_rebuild (default OFF — the semantic pass over docs is
# the most expensive thing the plugin does) + auto_rebuild_delay_s (default
# 120). Each file change re-arms a per-project timer; the build starts only
# after a quiet window. Errors surface via status.json like manual builds and
# are NOT retried until the next change — no hot retry loops.

def schedule_rebuild(projects_dir: str | Path, owner: str | None,
                     pid: str) -> bool:
    """(Re)arm the debounce timer for a project. Returns False when
    auto-rebuild is off or no event loop is running (sync fire contexts —
    the dirty flag is set either way, a manual build still works)."""
    cfg = _CONFIG or {}
    pcfg = (cfg.get("plugins") or {}).get("graphify") or {}
    if not pcfg.get("auto_rebuild"):
        return False
    try:
        delay = max(1, int(pcfg.get("auto_rebuild_delay_s") or 120))
    except (TypeError, ValueError):
        delay = 120
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    key = (owner or "_token", pid)
    old = _timers.pop(key, None)
    if old is not None:
        old.cancel()

    async def _fire() -> None:
        try:
            await asyncio.sleep(delay)
            _timers.pop(key, None)
            if not read_status(projects_dir, owner, pid).get("dirty"):
                return   # a concurrent build already covered these changes
            ok, msg = start_build(projects_dir, owner, pid, cfg)
            if not ok and "already running" in msg:
                # A build is in flight and files kept changing — re-arm so
                # those changes still produce a fresh graph afterwards.
                schedule_rebuild(projects_dir, owner, pid)
        except asyncio.CancelledError:
            pass

    _timers[key] = loop.create_task(_fire())
    return True


def cancel_rebuild_timer(owner: str | None, pid: str) -> None:
    """Drop a pending auto-rebuild (project deleted / manual build)."""
    task = _timers.pop((owner or "_token", pid), None)
    if task is not None:
        task.cancel()
