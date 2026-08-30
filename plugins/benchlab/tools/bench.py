"""bench.* tools — import public agent-benchmark tasks as JayNet eval cases.

Flow: bench.sources (what's available/imported) → bench.fetch (clone the
Terminal-Bench catalog into the data-dir cache) → bench.import (write eval
YAMLs to the custom evals dir) → run them in Admin → Eval / the Benchmark tab.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from runtime import paths
from runtime.tool_base import Tool, ToolContext, ToolResult


def _load_importer():
    """Import the plugin's importer.py by file path, ONCE per process (plugin
    modules are loaded via spec_from_file_location, not as a package — same
    pattern as the graphify plugin's runner)."""
    name = "benchlab_plugin_importer"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[1] / "importer.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


importer = _load_importer()

_GIT_TIMEOUT_S = 600


def _tb_cache() -> Path:
    return paths.DATA / "benchlab" / "terminal-bench"


def _tb_tasks_root() -> Path:
    return _tb_cache() / importer.TB_TASKS_SUBDIR


class BenchSources(Tool):
    name = "bench.sources"
    read_only = True
    description = (
        "List the agent-benchmark sources benchlab can import (terminal-bench, "
        "gaia) and how many of their cases are already imported. No network. "
        "Imported cases live in the custom evals dir and appear in Admin → Eval."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        import yaml
        counts = {"tb_lite": 0, "tb_full": 0, "gaia": 0, "other": 0}
        d = paths.CUSTOM_EVALS_DIR
        if d.is_dir():
            for f in sorted(d.glob("*.yaml")):
                if f.stem.startswith("tb-"):
                    # full-mode cases carry a top-level `container:` block —
                    # parse the YAML; a text scan misfires on checker scripts
                    # containing a column-0 "container:" line (audit D4)
                    try:
                        raw = yaml.safe_load(
                            f.read_text(encoding="utf-8", errors="replace"))
                    except yaml.YAMLError:
                        raw = None
                    full = isinstance(raw, dict) and bool(raw.get("container"))
                    counts["tb_full" if full else "tb_lite"] += 1
                elif f.stem.startswith("gaia-"):
                    counts["gaia"] += 1
                else:
                    counts["other"] += 1
        return ToolResult(status="ok", tool_name=self.name, result={
            "sources": [
                {"id": "terminal-bench",
                 "about": "Terminal-Bench core tasks (Apache-2.0), fetched as "
                          "a git clone by bench.fetch; imported container-free "
                          "(mode lite, curated subset) or as podman-container "
                          "cases (mode full, any task)",
                 "needs": "network for bench.fetch; full mode also needs "
                          "podman + network for image builds; no token",
                 "imported_cases": counts["tb_lite"] + counts["tb_full"],
                 "imported_lite": counts["tb_lite"],
                 "imported_full": counts["tb_full"]},
                {"id": "gaia",
                 "about": "GAIA Level-1 validation (gated HF dataset) as "
                          "exact-match cases via the HF HTTP API",
                 "needs": "network + HF_TOKEN env var on the service",
                 "imported_cases": counts["gaia"]},
            ],
            "custom_evals_dir": str(d),
            "other_custom_cases": counts["other"],
            "note": "imported cases appear in Admin → Eval; compare brains in "
                    "the Benchmark tab",
        })


class BenchFetch(Tool):
    name = "bench.fetch"
    description = (
        "Download the Terminal-Bench task catalog: a shallow git clone of "
        "laude-institute/terminal-bench (~170 MB) into the benchlab cache "
        "under the JayNet data dir (uses network; re-run updates it with git "
        "pull). Returns the task count and the container-free candidates "
        "(solvable with python3 stdlib + basic shell). Does NOT import "
        "anything — run bench.import afterwards."
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["terminal-bench"],
                       "description": "Only terminal-bench is fetchable; gaia "
                                      "is pulled on demand by bench.import."},
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "Optional allowlist of task directory "
                                     "names to report on (default: all)."},
        },
        "required": ["source"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        if args.get("source") != "terminal-bench":
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="only source 'terminal-bench' is fetchable")
        cache = _tb_cache()
        cache.parent.mkdir(parents=True, exist_ok=True)
        if (cache / ".git").is_dir():
            cmd = ["git", "-C", str(cache), "pull", "--ff-only"]
        else:
            cmd = ["git", "clone", "--depth", "1", importer.TB_REPO_URL,
                   str(cache)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(),
                                            timeout=_GIT_TIMEOUT_S)
        except TimeoutError:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"git timed out after {_GIT_TIMEOUT_S}s")
        except OSError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"git failed to start: {e}")
        if proc.returncode != 0:
            tail = out.decode("utf-8", errors="replace")[-500:]
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"git failed (exit {proc.returncode}): {tail}")
        root = _tb_tasks_root()
        scan = importer.scan_tb_catalog(root)
        if not scan:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=f"clone succeeded but no tasks found under "
                                    f"{importer.TB_TASKS_SUBDIR}/ — upstream "
                                    f"layout may have changed")
        allow = [str(t) for t in (args.get("tasks") or [])]
        if allow:
            scan = {n: r for n, r in scan.items() if n in set(allow)}
        clean = [n for n, reasons in scan.items() if not reasons]
        curated = [t for t in importer.CURATED_TB_TASKS if t in scan]
        return ToolResult(status="ok", tool_name=self.name, result={
            "cache": str(cache),
            "tasks_total": len(scan),
            "container_free_candidates": clean,
            "curated_default_import_set": curated,
            "note": "bench.import defaults to the curated audited subset; the "
                    "raw candidate list is heuristic — review before importing "
                    "beyond it",
        })


class BenchImport(Tool):
    name = "bench.import"
    description = (
        "Convert benchmark tasks into eval cases written to the custom evals "
        "dir — they appear in Admin → Eval (run them there or via the "
        "Benchmark tab). terminal-bench: uses the local clone from bench.fetch. "
        "mode lite (default): the audited container-free subset, no network, "
        "no podman. mode full: ANY task — builds each task's Dockerfile into "
        "a cached podman image (needs podman + network for the builds) and "
        "the case runs inside that container, close to the official "
        "Terminal-Bench protocol. gaia: downloads Level-1 validation rows + "
        "attachments over the network and needs HF_TOKEN in the service "
        "environment (gated dataset; the token is never logged). Re-import "
        "overwrites only the tb-*/gaia-* cases it owns; other files are "
        "untouched. Not for leaderboard-official numbers — these are "
        "JayNet-condition runs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["terminal-bench", "gaia"],
                       "description": "Which benchmark to import."},
            "mode": {"type": "string", "enum": ["lite", "full"],
                     "description": "terminal-bench only: lite (default) = "
                                    "curated container-free subset; full = "
                                    "any task, run inside its own podman "
                                    "container (builds images; needs podman "
                                    "and network)."},
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "terminal-bench: task directory names "
                                     "(lite default: the curated container-free "
                                     "subset; full default: every task in the "
                                     "catalog). gaia: ignored."},
            "limit": {"type": "integer",
                      "description": "Cap how many cases are imported "
                                     "(gaia default 50; terminal-bench default "
                                     "is the whole selected set)."},
        },
        "required": ["source"],
    }

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        source = args.get("source")
        if source == "terminal-bench":
            mode = str(args.get("mode") or "lite")
            if mode == "full":
                return await asyncio.to_thread(self._import_tb_full, args)
            if mode != "lite":
                return ToolResult(status="error", result=None,
                                  tool_name=self.name,
                                  error="mode must be 'lite' or 'full'")
            return await asyncio.to_thread(self._import_tb, args)
        if source == "gaia":
            return await self._import_gaia(args)
        return ToolResult(status="error", result=None, tool_name=self.name,
                          error="source must be 'terminal-bench' or 'gaia'")

    # -- terminal-bench (local clone, no network) --

    def _import_tb(self, args: dict) -> ToolResult:
        root = _tb_tasks_root()
        if not root.is_dir():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no Terminal-Bench catalog in the cache — "
                                    "run bench.fetch first (it clones the repo)")
        names = [str(t) for t in (args.get("tasks")
                                  or importer.CURATED_TB_TASKS)]
        limit = args.get("limit")
        if isinstance(limit, int) and limit > 0:
            names = names[:limit]
        cases, skipped = [], []
        for n in names:
            try:
                cases.append(importer.tb_task_to_case(
                    root / n, seed_extra=importer.TB_SEED_EXTRA.get(n, ())))
            except importer.SkipTask as e:
                skipped.append({"task": n, "reason": str(e)})
        result = importer.write_cases(cases, paths.CUSTOM_EVALS_DIR)
        result["skipped"] = skipped
        result["note"] = ("cases are in Admin → Eval now; grading is a "
                          "deterministic pytest checker per case")
        return ToolResult(status="ok", tool_name=self.name, result=result)

    # -- terminal-bench FULL mode (podman images; builds need network) --

    def _import_tb_full(self, args: dict) -> ToolResult:
        import shutil
        root = _tb_tasks_root()
        if not root.is_dir():
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="no Terminal-Bench catalog in the cache — "
                                    "run bench.fetch first (it clones the repo)")
        if shutil.which("podman") is None:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error="full mode needs podman (rootless is "
                                    "fine) — lite mode works without it")
        names = [str(t) for t in (args.get("tasks") or [])]
        if not names:
            names = sorted(d.name for d in root.iterdir()
                           if d.is_dir() and (d / "task.yaml").is_file())
        limit = args.get("limit")
        if isinstance(limit, int) and limit > 0:
            names = names[:limit]
        stage_root = paths.DATA / "benchlab" / "tests"
        cases, skipped = [], []
        images = {"built": 0, "cached": 0}
        for n in names:
            task_dir = root / n
            try:
                base = importer.tb_image_tag(task_dir)
                images[importer.build_tb_image(task_dir, base)] += 1
                staged = importer.stage_tb_tests(task_dir, stage_root)
                # pytest + the tests' own deps go on top as a thin layer
                # (official TB installs them via run-tests.sh); the layer tag
                # hashes the deps, so only this seconds-cheap layer rebuilds
                # when they change, never the base image.
                deps = importer.test_deps(
                    {f.relative_to(staged).as_posix(): f.read_text(
                        encoding="utf-8", errors="replace")
                     for f in staged.rglob("*.py")})
                layer = importer.build_test_layer(base, deps)
                cases.append(importer.tb_task_to_case_full(task_dir, layer,
                                                           staged))
            except importer.SkipTask as e:
                skipped.append({"task": n, "reason": str(e)})
        result = importer.write_cases(cases, paths.CUSTOM_EVALS_DIR)
        result["skipped"] = skipped
        result["images"] = images
        result["tests_staged_under"] = str(stage_root)
        result["note"] = (
            "container cases are in Admin → Eval now (tag tb-full); each "
            "runs inside its own podman image (with outbound network, like "
            "official Terminal-Bench) and grades via the task's own "
            "run-tests.sh inside the container (plain pytest + test deps as "
            "fallback). Images are cached — re-imports only rebuild changed "
            "tasks or changed test deps.")
        return ToolResult(status="ok", tool_name=self.name, result=result)

    # -- GAIA (HF HTTP API, token from env) --

    async def _import_gaia(self, args: dict) -> ToolResult:
        token = os.environ.get("HF_TOKEN")
        if not token:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=importer.GAIA_TOKEN_HELP)
        limit = args.get("limit")
        limit = limit if isinstance(limit, int) and limit > 0 else 50
        try:
            rows = await asyncio.to_thread(
                importer.fetch_gaia_rows, token, limit)
        except RuntimeError as e:
            return ToolResult(status="error", result=None, tool_name=self.name,
                              error=str(e))
        cases, skipped = [], []
        for row in rows:
            fname = str(row.get("file_name") or "").strip()
            attachment = None
            try:
                if fname:
                    fpath = str(row.get("file_path") or fname)
                    attachment = await asyncio.to_thread(
                        importer.fetch_gaia_attachment, token, fpath)
                cases.append(importer.gaia_row_to_case(row, attachment, fname))
            except (importer.SkipTask, RuntimeError) as e:
                skipped.append({"task_id": str(row.get("task_id") or "?"),
                                "reason": str(e)})
        result = importer.write_cases(cases, paths.CUSTOM_EVALS_DIR)
        result["skipped"] = skipped
        result["note"] = ("cases are in Admin → Eval now; grading is "
                          "normalized exact match on the FINAL ANSWER marker")
        return ToolResult(status="ok", tool_name=self.name, result=result)
