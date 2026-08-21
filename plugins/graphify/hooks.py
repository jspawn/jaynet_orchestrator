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
    model can't call. With a graph: all five. Without one: build+status,
    so the agent can offer to map the project."""
    root = Path(files_root).parent / runner.GRAPH_DIRNAME
    if (root / "graph.json").is_file():
        return ["graph.build", "graph.status", "graph.query",
                "graph.explain", "graph.path"]
    return ["graph.build", "graph.status"]


def on_project_file_changed(owner, pid, path, projects_dir) -> None:
    # projects_dir comes resolved from the fire site — a custom
    # web.projects_dir would make a runtime.paths default silently wrong.
    runner.mark_dirty(projects_dir, owner, pid)


def on_project_delete(owner, pid) -> None:
    runner.cancel_build(owner, pid)
