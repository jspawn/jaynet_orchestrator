"""Graphify plugin hooks — prompt hint + staleness tracking.

Registered by runtime/plugins.py under the names in runtime.hooks.HOOK_NAMES.
Keep these fast: they fire synchronously on the request path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_runner():
    """Shared runner module — MUST reuse the single sys.modules entry
    (see tools/graph.py:_load_runner); a fresh exec would split the _jobs
    dict and break the duplicate-build guard / cancel-on-delete."""
    name = "graphify_plugin_runner"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parent / "runner.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


runner = _load_runner()

# rag_excerpt parses graph.json on the request path, once per project-bound
# rag.search with hits — the heaviest hook we have. graph.json is written
# atomically (os.replace), so (mtime_ns, size) is a safe cache key; a rebuild
# swaps the file and invalidates naturally. Capped so many projects can't
# grow it without bound.
_GRAPH_CACHE: dict[str, tuple[int, int, dict]] = {}


def _cached_graph(groot: Path) -> dict | None:
    gj = Path(groot) / "graph.json"
    try:
        st = gj.stat()
    except OSError:
        return None
    key = str(gj)
    hit = _GRAPH_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    graph = runner.load_graph(groot)
    if graph is not None:
        if len(_GRAPH_CACHE) >= 32:
            _GRAPH_CACHE.clear()
        _GRAPH_CACHE[key] = (st.st_mtime_ns, st.st_size, graph)
    return graph


def augment_project_context(owner, pid, meta, files_root) -> str | None:
    """One hint line when the project has a graph — the 'query before grep'
    nudge. None (silent) when no graph exists."""
    root = Path(files_root).parent / runner.GRAPH_DIRNAME
    if not (root / "graph.json").is_file():
        return None
    st = runner.read_status(Path(files_root).parent.parent.parent,
                            owner, pid)
    counts = (f"{st['nodes']} nodes, {st['edges']} edges"
              if st.get("nodes") else "counts unknown")
    hint = (f"[Project graph] this project is mapped ({counts}) — prefer "
            f"graph.query / graph.explain / graph.path over reading whole "
            f"files for architecture or 'what connects X to Y' questions.")
    if st.get("state") == "building":
        hint += " A rebuild is currently running."
    elif st.get("dirty"):
        hint += (" Files changed since the last build — the graph may be "
                 "stale; suggest graph.build if answers look off.")
    return hint


def project_tools(owner, pid, meta, files_root) -> list[str]:
    """Keep graph.* reachable in this run's frozen toolset — the keyword
    selector only sees the message text and has no trigger for the `graph`
    namespace, so without this the hint above would advertise tools the
    model can't call. With a graph: all six. Without one: build+status,
    so the agent can offer to map the project."""
    root = Path(files_root).parent / runner.GRAPH_DIRNAME
    if (root / "graph.json").is_file():
        return ["graph.build", "graph.status", "graph.query",
                "graph.explain", "graph.path", "graph.seed_kg"]
    return ["graph.build", "graph.status"]


def rag_excerpt(owner, pid, matches, projects_dir) -> str | None:
    """A compact 1-hop graph neighborhood around the rag.search hits — the
    bridge from the auto-derived project graph into retrieval results.
    Scoping comes from the caller (run context owner/pid + resolved
    projects_dir), never from tool arguments. None when nothing matches —
    rag.search then returns plain chunk hits."""
    graph = _cached_graph(runner.graph_root(projects_dir, owner, pid))
    if not graph:
        return None
    wanted = set()
    for m in matches[:5]:
        base = str(m.get("source") or "").rsplit("/", 1)[-1]
        if base:
            wanted.add(base)
    if not wanted:
        return None
    nodes = {str(n.get("id")): n for n in graph.get("nodes") or []}
    hits = [n for n in nodes.values()
            if str(n.get("file") or "").rsplit("/", 1)[-1] in wanted]
    if not hits:
        return None
    edges = graph.get("edges") or []
    blocks = []
    for n in hits[:3]:
        nid = str(n.get("id"))
        lines = []
        for e in edges:
            s, d = str(e.get("source")), str(e.get("target"))
            if s != nid and d != nid:
                continue
            rel = str(e.get("relation") or e.get("rel") or "links")
            lines.append(f"{nid} -[{rel}]-> {d if s == nid else s}")
            if len(lines) >= 5:
                break
        if lines:
            blocks.append("\n".join(lines))
    if not blocks:
        return None
    text = "[Project graph] structure related to the hits:\n" + "\n".join(blocks)
    return text[:1200]


def on_project_file_changed(owner, pid, path, projects_dir) -> None:
    # projects_dir comes resolved from the fire site — a custom
    # web.projects_dir would make a runtime.paths default silently wrong.
    # mark_dirty only flags projects that HAVE a graph (never-built ones
    # stay 'none') — that doubles as the auto-rebuild consent gate.
    if runner.mark_dirty(projects_dir, owner, pid):
        runner.schedule_rebuild(projects_dir, owner, pid)


def on_project_delete(owner, pid) -> None:
    runner.cancel_build(owner, pid)
    runner.cancel_rebuild_timer(owner, pid)
