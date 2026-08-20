"""Graphify plugin hooks — prompt hint + staleness tracking.

Registered by runtime/plugins.py under the names in runtime.hooks.HOOK_NAMES.
Keep these fast: they fire synchronously on the request path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "graphify_plugin_runner_hooks",
        Path(__file__).resolve().parent / "runner.py")
    mod = importlib.util.module_from_spec(spec)
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
    hint = (f"[Knowledge graph] this project is mapped ({counts}) — prefer "
            f"graph.query / graph.explain / graph.path over reading whole "
            f"files for architecture or 'what connects X to Y' questions.")
    if st.get("state") == "building":
        hint += " A rebuild is currently running."
    elif st.get("dirty"):
        hint += (" Files changed since the last build — the graph may be "
                 "stale; suggest graph.build if answers look off.")
    return hint


def on_project_file_changed(owner, pid, path) -> None:
    from runtime import paths
    projects_dir = paths.PROJECTS_DIR
    runner.mark_dirty(projects_dir, owner, pid)


def on_project_delete(owner, pid) -> None:
    runner.cancel_build(owner, pid)
