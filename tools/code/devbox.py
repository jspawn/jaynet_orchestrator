"""devbox — per-run toolchain containers for code.run.

The firejail sandbox only has what the HOST has installed, so "compile this
Rust" failed even though the harness can write the code. When the operator
enables `tools.code.devbox` (image built once via scripts/devbox-build.sh),
code.run executes inside a podman container that carries the major coding
environments instead of the host-limited firejail wrapper.

Lifecycle (no loop hooks needed — self-managing):
- ONE container PER RUN, named jaynet-devbox-<run_id>: started lazily on the
  run's first devbox call, `--rm` (gone when stopped), the run's work_root
  bind-mounted at /work and its tmp_root at /tmp/run — exactly the roots
  code.run may touch, so confinement matches the firejail path.
- Dependency caches (cargo registry, go module cache, npm cache) live on
  SHARED named volumes — downloads survive the per-run containers, so
  iterative builds stay fast.
- Idle reaper: every ensure() stops containers whose last use is older than
  idle_ttl_s (default 30 min). A crashed web process just leaves them until
  the next call reaps.
- Network: toolchains need registries for real work, so the container is
  networked by default (config `network`) — EXCEPT on private-tainted runs,
  where it is always started with --network=none so a snippet cannot curl
  workspace data out (same privacy posture as the cloud gate).
- Degradation: podman missing, image not built, container start failing →
  the caller falls back to the classic firejail path with a note; a run
  never breaks because the devbox isn't there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from runtime.tool_base import ToolContext, ToolResult, scrub_env

log = logging.getLogger(__name__)

_CONTAINER_PREFIX = "jaynet-devbox-"
_WORK_DIR = "/work"
_TMP_DIR = "/tmp/run"
# Shared dependency caches → container paths (see containers/devbox/Containerfile).
_CACHE_VOLUMES = (
    ("jaynet-devbox-cargo", "/usr/local/cargo/registry"),
    ("jaynet-devbox-gocache", "/go/pkg/mod"),
    ("jaynet-devbox-npmcache", "/root/.npm"),
)
_image_ok: bool | None = None       # once per process: is the image built?


def cfg(ctx: ToolContext) -> dict:
    return (ctx.config.get("tools", {}).get("code", {}) or {}).get("devbox", {}) or {}


def enabled(ctx: ToolContext) -> bool:
    return bool(cfg(ctx).get("enabled", False))


def _state_dir(ctx: ToolContext) -> Path:
    from runtime import paths
    d = paths.DATA / "devbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def container_name(ctx: ToolContext) -> str:
    return _CONTAINER_PREFIX + str(ctx.request_id or "run")[:12]


def _network(ctx: ToolContext) -> bool:
    """Container gets network unless the operator cut it OR the conversation
    is private-tainted (exfil guard — a tainted workspace + open network is a
    curl away from a leak)."""
    if getattr(ctx, "private_taint", False):
        return False
    return bool(cfg(ctx).get("network", True))


async def _podman(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """One podman client call with a scrubbed env (the client never sees the
    orchestrator's secrets either)."""
    env = scrub_env(dict(os.environ))
    proc = await asyncio.create_subprocess_exec(
        "podman", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"podman {' '.join(args[:2])} timed out"
    return (proc.returncode or 0,
            out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _touch(ctx: ToolContext, name: str, work_root: str,
           network: bool | None = None) -> None:
    """Record last-use (+ the container's ACTUAL network state, authoritative
    for reuse). network=None preserves the previously recorded value."""
    f = _state_dir(ctx) / f"{name}.json"
    if network is None:
        try:
            network = bool(json.loads(f.read_text()).get("network", True))
        except (OSError, ValueError, TypeError):
            network = True
    try:
        f.write_text(json.dumps({"name": name, "work_root": work_root,
                                 "network": network,
                                 "last_use": time.time()}))
    except OSError:
        pass


def _recorded_network(ctx: ToolContext, name: str) -> bool:
    """The network state the container was STARTED with (or last cut to) —
    not the current taint's wish. The running container's real state."""
    try:
        return bool(json.loads((_state_dir(ctx) / f"{name}.json")
                               .read_text()).get("network", True))
    except (OSError, ValueError, TypeError):
        return True


async def reap_idle(ctx: ToolContext) -> None:
    """Stop devbox containers idle past the TTL. Best-effort: failures are
    logged, never raised — reaping is hygiene, not correctness."""
    ttl = int(cfg(ctx).get("idle_ttl_s", 1800) or 1800)
    now = time.time()
    try:
        states = list(_state_dir(ctx).glob(f"{_CONTAINER_PREFIX}*.json"))
    except OSError:
        return
    for f in states:
        try:
            st = json.loads(f.read_text())
            last = float(st.get("last_use") or 0)
        except (OSError, ValueError, TypeError):
            last = 0
        if now - last < ttl:
            continue
        name = st.get("name") or f.stem
        rc, _, _ = await _podman("stop", "-t", "2", name)
        if rc == 0:
            log.info("devbox: reaped idle container %s", name)
        try:
            f.unlink()
        except OSError:
            pass


async def _image_built(ctx: ToolContext) -> bool:
    global _image_ok
    if _image_ok is None:
        image = str(cfg(ctx).get("image") or "jaynet-devbox:latest")
        rc, _, _ = await _podman("image", "inspect", image)
        _image_ok = rc == 0
        if not _image_ok:
            log.warning("devbox: image '%s' not built — run "
                        "scripts/devbox-build.sh (falling back to firejail)",
                        image)
    return _image_ok


async def ensure(ctx: ToolContext) -> dict | None:
    """A running devbox container for THIS run: {name, workdir, tmpdir,
    network}. None when the devbox can't run (no podman, image missing,
    start failed) — the caller falls back to the classic sandbox."""
    if not getattr(ctx, "work_root", None):
        return None
    if shutil.which("podman") is None:
        return None
    if not await _image_built(ctx):
        return None
    name = container_name(ctx)
    work_root = str(Path(ctx.work_root).resolve())
    network = _network(ctx)

    rc, out, _ = await _podman("inspect", "-f", "{{.State.Running}}", name)
    if rc == 0 and out.strip() == "true":
        _touch(ctx, name, work_root)
        # Reuse: report the network the container ACTUALLY has (start-time
        # state, possibly cut since), never the current taint's wish —
        # attempt() reconciles a live cut when the run tainted meanwhile.
        return {"name": name, "workdir": _WORK_DIR, "tmpdir": _TMP_DIR,
                "network": _recorded_network(ctx, name)}

    # Fresh container for this run. --rm: stopping removes it (reaper or
    # host reboot cleans up; nothing accumulates).
    image = str(cfg(ctx).get("image") or "jaynet-devbox:latest")
    argv = ["run", "-d", "--name", name, "--rm",
            "--security-opt", "no-new-privileges",
            "-v", f"{work_root}:{_WORK_DIR}:rw"]
    tmp_root = getattr(ctx, "tmp_root", None)
    if tmp_root:
        argv += ["-v", f"{Path(tmp_root).resolve()}:{_TMP_DIR}:rw"]
    for vol, dest in _CACHE_VOLUMES:
        argv += ["-v", f"{vol}:{dest}:rw"]
    if not network:
        argv += ["--network", "none"]
    argv += [image, "sleep", "infinity"]
    rc, _, err = await _podman(*argv, timeout=60)
    if rc != 0:
        log.warning("devbox: container start failed (%s) — falling back to "
                    "firejail", err.strip()[:200])
        return None
    _touch(ctx, name, work_root, network=network)
    # Hygiene on every start: reap containers from runs long gone.
    asyncio.create_task(reap_idle(ctx))
    return {"name": name, "workdir": _WORK_DIR, "tmpdir": _TMP_DIR,
            "network": network}


def map_cwd(ctr: dict, cwd: Path, ctx: ToolContext) -> str:
    """Host cwd → in-container path. The container mounts exactly the run's
    work_root and tmp_root, so anything else is a bug in the caller's
    confinement, not a path to translate."""
    cwd = cwd.resolve()
    work_root = Path(ctx.work_root).resolve()
    if cwd == work_root or work_root in cwd.parents:
        rel = cwd.relative_to(work_root)
        return ctr["workdir"] if str(rel) == "." else f"{ctr['workdir']}/{rel}"
    tmp_root = getattr(ctx, "tmp_root", None)
    if tmp_root:
        tmp_root = Path(tmp_root).resolve()
        if cwd == tmp_root or tmp_root in cwd.parents:
            rel = cwd.relative_to(tmp_root)
            return ctr["tmpdir"] if str(rel) == "." else f"{ctr['tmpdir']}/{rel}"
    raise PermissionError(f"cwd {cwd} is outside the devbox container's mounts")


async def attempt(args: dict, ctx: ToolContext, cwd: Path, command: str,
                  timeout: int, max_lines: int, max_chars: int,
                  tail_fn) -> tuple[ToolResult | None, str | None]:
    """Try the command in this run's devbox container. (None, note) means the
    devbox is unavailable — the caller falls back to its classic sandbox and
    shows the note. Otherwise a finished ToolResult."""
    ctr = await ensure(ctx)
    if ctr is None:
        return None, ("devbox unavailable (podman/image/container start — see "
                      "logs); ran with the classic sandbox instead")
    # Taint can arrive AFTER the container started (run compiles first, reads
    # a private file later): a running container keeps its start-time
    # network. Cut it live — the taint rule is per-call, not per-start.
    if ctr["network"] and getattr(ctx, "private_taint", False):
        rc, _, _ = await _podman("network", "disconnect", "podman",
                                 ctr["name"])
        ctr["network"] = False
        if rc != 0:
            log.warning("devbox: network disconnect failed for %s — treating "
                        "as cut anyway (may already be down)", ctr["name"])
        _touch(ctx, ctr["name"], str(Path(ctx.work_root).resolve()),
               network=False)
    try:
        ctr_cwd = map_cwd(ctr, cwd, ctx)
    except PermissionError as e:
        return None, f"devbox skipped: {e}"

    cmd = ["exec", "--workdir", ctr_cwd]
    env_args = dict(cfg(ctx).get("env") or {})
    env_args.update({k: str(v) for k, v in (args.get("env") or {}).items()})
    for k, v in env_args.items():
        cmd += ["--env", f"{k}={v}"]
    # coreutils `timeout` bounds the command INSIDE the container; the
    # wait_for below is the backstop against a wedged podman itself.
    cmd += [ctr["name"], "timeout", str(timeout), "bash", "-c", command]
    rc, out, err = await _podman(*cmd, timeout=timeout + 15)
    _touch(ctx, ctr["name"], str(Path(ctx.work_root).resolve()))

    timed_out = rc == 124
    from tools.code.run import _tail  # same bounding as the host path
    out_t, out_trunc = _tail(out, max_lines, max_chars)
    err_t, err_trunc = _tail(err, max_lines, max_chars)
    note = None
    if not ctr["network"]:
        note = ("devbox network: OFF (private conversation — dependency "
                "downloads unavailable; use vendored/local deps)")
    result = {
        "cwd": str(cwd), "container_cwd": ctr_cwd,
        "exit_code": None if timed_out else rc, "ok": not timed_out and rc == 0,
        "stdout": out_t, "stderr": err_t,
        "truncated": out_trunc or err_trunc,
        "sandbox": "devbox", "container": ctr["name"],
        "network": ctr["network"],
    }
    if note:
        result["note"] = note
    if timed_out:
        return ToolResult(status="error", result=result,
                          error=f"execution timeout after {timeout}s"), None
    return ToolResult(status="ok", result=result), None
