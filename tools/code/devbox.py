"""devbox — per-run toolchain containers for code.run.

The firejail sandbox only has what the HOST has installed, so "compile this
Rust" failed even though the harness can write the code. When the operator
enables `tools.code.devbox` (image built once via scripts/devbox-build.sh),
code.run executes inside a podman container that carries the major coding
environments instead of the host-limited firejail wrapper.

Lifecycle (no loop hooks needed — self-managing):
- ONE container PER RUN, named jaynet-devbox-<run_id>: started lazily on the
  run's first devbox call, `--rm` (gone when stopped), the run's work_root
  bind-mounted at /work, its tmp_root at /tmp/run, and any extra_roots (e.g.
  the owner's upload dir when the run has attachments) at their identical
  host path — exactly the roots code.run may touch, so confinement matches
  the firejail path and skill/prompt paths work verbatim.
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
import tempfile
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
    ("jaynet-devbox-nuget", "/root/.nuget/packages"),
)
_image_ok: bool | None = None       # latched on SUCCESS only (see _image_built)


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
    orchestrator's secrets either).

    Output goes to temp FILES, not pipes, and we wait for process EXIT, not
    pipe EOF: `podman run -d`'s detached conmon inherits the pipe write-ends
    and holds them open for the container's whole lifetime, so a
    communicate()-style read only returns when the timeout fires — live,
    every fresh devbox "timed out" at exactly 60s, fell back to firejail,
    and orphaned the actually-started container (the "12x21 took 78s"
    bug). Waiting on exit is immune: the CLI exits once the container is
    created, regardless of what its grandchildren hold open."""
    env = scrub_env(dict(os.environ))
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = await asyncio.create_subprocess_exec(
            "podman", *args,
            stdout=out_f, stderr=err_f,
            env=env, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass            # raced: podman exited as the timeout fired
            await proc.wait()
            return 124, "", f"podman {' '.join(args[:2])} timed out"
        out_f.seek(0)
        err_f.seek(0)
        return (proc.returncode or 0,
                out_f.read().decode("utf-8", "replace"),
                err_f.read().decode("utf-8", "replace"))


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


_REAP_TASKS: set = set()


def _schedule_reap(ctx: ToolContext) -> None:
    """Fire-and-forget reaper WITH a strong reference — an unreferenced
    create_task can be garbage-collected mid-pass (the documented asyncio
    footgun), which is how a fleet of devboxes + 141 stale state files
    survived days of runs. One reaper at a time: overlapping passes would
    just duplicate the podman stops."""
    if any(not t.done() for t in _REAP_TASKS):
        return
    task = asyncio.create_task(reap_idle(ctx))
    _REAP_TASKS.add(task)
    task.add_done_callback(_REAP_TASKS.discard)


async def reap_idle(ctx: ToolContext) -> None:
    """Stop devbox containers idle past the TTL. Best-effort: failures are
    logged, never raised — reaping is hygiene, not correctness.

    Two pass families (readiness audit BE-1):
    - state-file pass: stop stale containers we booked. The state file is
      removed when the stop succeeded OR when the container no longer
      exists at all (its --rm already cleaned up — a kept ghost file made
      every later sweep pay a failing stop; 102 ghosts ≈ 60s of serialized
      podman calls starving the storage lock on a run's first exec). It is
      kept only when the stop FAILED but the container may still be live —
      deleting it after a failed stop orphaned 146 live containers that
      nothing could ever reap again.
    - prefix sweep: stop any jaynet-devbox-* container WITHOUT a state file
      (crashed web process, previously lost bookkeeping). They are per-run
      --rm containers; stopping removes them."""
    ttl = int(cfg(ctx).get("idle_ttl_s", 1800) or 1800)
    now = time.time()
    try:
        states = list(_state_dir(ctx).glob(f"{_CONTAINER_PREFIX}*.json"))
    except OSError:
        return
    known: set[str] = set()
    for f in states:
        try:
            st = json.loads(f.read_text())
            last = float(st.get("last_use") or 0)
        except (OSError, ValueError, TypeError):
            last = 0
        name = st.get("name") or f.stem
        known.add(name)
        if now - last < ttl:
            continue
        rc, _, err = await _podman("stop", "-t", "2", name)
        if rc == 0:
            log.info("devbox: reaped idle container %s", name)
            try:
                f.unlink()
            except OSError:
                pass
        elif ("no such container" in err.lower()
              or "no container with name or id" in err.lower()):
            # Ghost: the container is already gone (its --rm cleaned up, or
            # a host reboot did) — the state file is pure debt.
            log.info("devbox: state file for %s outlived its container — "
                     "dropping it", name)
            try:
                f.unlink()
            except OSError:
                pass
        else:
            # Keep the state file: without it a LIVE container whose stop
            # failed is unreapable.
            log.warning("devbox: stop of idle container %s failed (rc=%s) — "
                        "keeping its state file for the next pass", name, rc)
    # Orphan sweep: containers matching the prefix with no live state file.
    rc, out, _ = await _podman("ps", "-a", "--filter",
                               f"name={_CONTAINER_PREFIX}",
                               "--format", "{{.Names}}")
    if rc != 0:
        return
    # Re-read the state files NOW: `known` from pass start is stale by the
    # time the stops above finished, and a container created mid-pass (the
    # ensure() that scheduled this reaper creates one!) would otherwise be
    # reaped as a false orphan — the live code-bugfix eval lost its devbox
    # exactly this way ("no such container" on the next exec).
    try:
        known = set()
        for f in _state_dir(ctx).glob(f"{_CONTAINER_PREFIX}*.json"):
            try:
                known.add(json.loads(f.read_text()).get("name") or f.stem)
            except (OSError, ValueError, TypeError):
                known.add(f.stem)
    except OSError:
        pass
    for name in out.split():
        if name in known or not name.startswith(_CONTAINER_PREFIX):
            continue
        # Young containers are not orphans — a state file may simply not be
        # written yet. Only stop what predates this pass minus the TTL.
        rc2, started, _ = await _podman("inspect", "-f",
                                        "{{.State.StartedAt}}", name)
        st = _parse_podman_time(started) if rc2 == 0 else None
        if st is None or now - st < ttl:
            # Unknown age fails SAFE: never stop what we can't date — the
            # old code stopped on parse failure, and podman's StartedAt
            # ("2026-09-24 04:09:09.7… +0200 CEST") never parsed, so EVERY
            # stateless box was stopped on sight (live: ghost-container
            # run-killers ~30s into fanout runs).
            continue
        log.info("devbox: reaping orphaned container %s (no state file)", name)
        await _podman("stop", "-t", "2", name)


def _parse_podman_time(s: str) -> float | None:
    """Epoch for podman's timestamp format, None when unparseable.
    Podman prints "2026-09-24 04:09:09.791582676 +0200 CEST" — the trailing
    zone NAME breaks fromisoformat; the numeric offset before it suffices."""
    from datetime import datetime
    s = s.strip().replace("Z", "+00:00")
    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and not parts[1].strip("+-").isdigit():
        s = parts[0]                       # drop the named zone ("CEST")
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, OverflowError):
        return None


async def _image_built(ctx: ToolContext) -> bool:
    global _image_ok
    # Latch SUCCESS only: an operator who enables the devbox before building
    # the image (reverse of the documented order) gets the container on the
    # next call after the build, no web-process restart needed. A missing
    # image just costs one extra `image inspect` per call — the run falls
    # back to firejail anyway.
    if _image_ok:
        return True
    image = str(cfg(ctx).get("image") or "jaynet-devbox:latest")
    rc, _, _ = await _podman("image", "inspect", image)
    _image_ok = rc == 0
    if not _image_ok:
        log.warning("devbox: image '%s' not built — run "
                    "scripts/devbox-build.sh (falling back to firejail)",
                    image)
    return bool(_image_ok)


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
    # Hygiene on EVERY ensure (reuse included — a long stretch of reuses or
    # quiet periods used to mean the reaper never ran at all).
    _schedule_reap(ctx)
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
    if rc == 0:
        # Stale same-name container (died mid-run; its --rm cleanup never
        # ran, so the name stays taken and `podman run` below would collide
        # — the live "container start failed ... already in use" downgrade
        # path, readiness audit BE-1). Remove it and start fresh.
        log.info("devbox: removing stale container %s before restart", name)
        await _podman("rm", "-f", name)

    # Fresh container for this run. --rm: stopping removes it (reaper or
    # host reboot cleans up; nothing accumulates).
    image = str(cfg(ctx).get("image") or "jaynet-devbox:latest")
    argv = ["run", "-d", "--name", name, "--rm",
            "--security-opt", "no-new-privileges",
            "-v", f"{work_root}:{_WORK_DIR}:rw"]
    tmp_root = getattr(ctx, "tmp_root", None)
    if tmp_root:
        argv += ["-v", f"{Path(tmp_root).resolve()}:{_TMP_DIR}:rw"]
    # Extra roots (e.g. the owner's upload dir when the run has attachments,
    # or a /llmwiki wiki dir) mount at their IDENTICAL host path, so paths in
    # prompts and skills work verbatim inside the container.
    for r in (getattr(ctx, "extra_roots", None) or []):
        rp = str(Path(r).resolve())
        argv += ["-v", f"{rp}:{rp}:rw"]
    for vol, dest in _CACHE_VOLUMES:
        argv += ["-v", f"{vol}:{dest}:rw"]
    if not network:
        argv += ["--network", "none"]
    argv += [image, "sleep", "infinity"]
    # Pre-book the state file BEFORE the container exists: a concurrent
    # reaper's orphan sweep otherwise catches the stateless box in the
    # run→touch window and stops it — the next exec then hits
    # "no such container" (the live fanout ghost-killer).
    _touch(ctx, name, work_root, network=network)
    rc, _, err = await _podman(*argv, timeout=60)
    if rc != 0 and "already in use" in err:
        # Raced with a container that took our name between inspect and run
        # (or a stale one inspect couldn't see) — remove and retry ONCE.
        log.info("devbox: name %s already in use — removing and retrying", name)
        await _podman("rm", "-f", name)
        rc, _, err = await _podman(*argv, timeout=60)
    if rc != 0:
        try:
            (_state_dir(ctx) / f"{name}.json").unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("devbox: container start failed (%s) — falling back to "
                    "firejail", err.strip()[:200])
        return None
    _touch(ctx, name, work_root, network=network)
    return {"name": name, "workdir": _WORK_DIR, "tmpdir": _TMP_DIR,
            "network": network}


def map_cwd(ctr: dict, cwd: Path, ctx: ToolContext) -> str:
    """Host cwd → in-container path. The container mounts the run's work_root
    (at /work), its tmp_root (at /tmp/run) and any extra_roots (at their
    identical host path) — anything else is a bug in the caller's
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
    for r in (getattr(ctx, "extra_roots", None) or []):
        r = Path(r).resolve()
        if cwd == r or r in cwd.parents:
            return str(cwd)          # extra roots: identical in-container path
    raise PermissionError(f"cwd {cwd} is outside the devbox container's mounts")


async def attempt(args: dict, ctx: ToolContext, cwd: Path, command: str,
                  timeout: int, max_lines: int, max_chars: int,
                  ) -> tuple[ToolResult | None, str | None]:
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

    # fs.* show the model HOST paths — translate them to the container mounts
    # or `cd <host work_root>` fails inside the box and gets retried forever.
    from runtime.tool_base import translate_container_command
    mounts = [(str(Path(ctx.work_root).resolve()), ctr["workdir"])]
    if getattr(ctx, "tmp_root", None):
        mounts.append((str(Path(ctx.tmp_root).resolve()), ctr["tmpdir"]))
    command, mapped = translate_container_command(command, mounts)

    cmd = ["exec", "--workdir", ctr_cwd]
    env_args = dict(cfg(ctx).get("env") or {})
    env_args.update({k: str(v) for k, v in (args.get("env") or {}).items()})
    for k, v in env_args.items():
        cmd += ["--env", f"{k}={v}"]
    # coreutils `timeout` bounds the command INSIDE the container; the
    # wait_for below is the backstop against a wedged podman itself.
    cmd += [ctr["name"], "timeout", str(timeout), "bash", "-c", command]
    rc, out, err = await _podman(*cmd, timeout=timeout + 15)
    if rc != 0 and ("no such container" in err.lower()
                    or "no container with name or id" in err.lower()):
        # Ghost container: reaped mid-run (judge pause > idle TTL) or lost to
        # the orphan race — the state file lied. Drop it and retry ONCE with
        # a fresh box; without this the run spins on phantom sandbox failures
        # (live: code-bugfix turn 2, 15+ iterations of flailing).
        log.info("devbox: %s vanished mid-run — recreating and retrying",
                 ctr["name"])
        try:
            (_state_dir(ctx) / f"{ctr['name']}.json").unlink(missing_ok=True)
        except OSError:
            pass
        ctr = await ensure(ctx)
        if ctr is None:
            return None, ("devbox container vanished mid-run and recreation "
                          "failed; ran with the classic sandbox instead")
        if ctr["network"] and getattr(ctx, "private_taint", False):
            await _podman("network", "disconnect", "podman", ctr["name"])
            ctr["network"] = False
        ctr_cwd = map_cwd(ctr, cwd, ctx)
        cmd = ["exec", "--workdir", ctr_cwd]
        for k, v in env_args.items():
            cmd += ["--env", f"{k}={v}"]
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
    if mapped:
        _m = dict(mounts)
        result["path_note"] = ("host paths in the command were translated: "
                               + ", ".join(f"{h} → {_m[h]}" for h in mapped)
                               + " (the devbox mounts your workspace there)")
    if timed_out:
        return ToolResult(status="error", result=result,
                          error=f"execution timeout after {timeout}s"), None
    return ToolResult(status="ok", result=result), None
