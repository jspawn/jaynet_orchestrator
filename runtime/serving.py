"""Model-server lifecycle — the engine behind the `serve.*` tools.

Promotes the gpu-serve skill's manual `job.start` recipe into a managed thing:
launch a llama-server (a second brain, an embedder, a reranker) pinned to a GPU,
wait until it answers, track it in a registry, and tear it down to free VRAM.
Filesystem is the source of truth (one dir per server under `state_dir/<name>/`),
mirroring the job runner so liveness survives an orchestrator restart.

Stdlib + httpx only. The detached-launch mechanics deliberately match
`tools/job/runner.py` (own session, GPU_MAX_HW_QUEUES, optional rdna4-env source).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:  # treat zombies as dead (same guard the job runner uses)
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        return True


def pid_cmdline(pid: int | None) -> str:
    """/proc/<pid>/cmdline as one string ('' if unreadable). Linux only. Used to
    verify a recorded pid still belongs to THIS server before signaling its
    group — pids get recycled, and server.json can survive restarts indefinitely."""
    if not pid:
        return ""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


# ----------------------------- registry --------------------------------------

def _server_dir(state_dir: str | Path, name: str) -> Path:
    return Path(state_dir) / name


def read_server(state_dir: str | Path, name: str) -> dict | None:
    p = _server_dir(state_dir, name) / "server.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_server(state_dir: str | Path, entry: dict) -> None:
    d = _server_dir(state_dir, entry["name"])
    d.mkdir(parents=True, exist_ok=True)
    # Atomic (tmp + replace): a crash mid-write must not strand a running
    # server invisible to the serve layer (audit D2).
    tmp = d / "server.json.tmp"
    tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    tmp.replace(d / "server.json")


def delete_server(state_dir: str | Path, name: str) -> None:
    p = _server_dir(state_dir, name) / "server.json"
    try:
        p.unlink()
    except OSError:
        pass


def list_servers(state_dir: str | Path) -> list[dict]:
    root = Path(state_dir)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        e = read_server(state_dir, d.name) if d.is_dir() else None
        if e:
            out.append(e)
    return out


def taken_ports(state_dir: str | Path) -> set[int]:
    return {int(e["port"]) for e in list_servers(state_dir) if e.get("port")}


# ----------------------------- ports & vram -----------------------------------

def pick_free_port(preferred: int, reserved: set[int], host: str = "127.0.0.1") -> int:
    port = preferred
    for _ in range(200):
        if port not in reserved and _port_free(host, port):
            return port
        port += 1
    raise RuntimeError("no free port found")


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def read_vram(ctx) -> list[dict] | None:
    """[{index, card, used_gib, total_gib, free_gib}] per GPU, or None if it
    can't be read (then headroom checks are advisory)."""
    try:
        from tools.gpu.status import _parse_rocm_smi, _resolve
        smi = _resolve("rocm-smi", ctx) or _resolve("rocm_smi", ctx)
        if not smi:
            return None
        raw = subprocess.run([smi, "--showmeminfo", "vram", "--json"],
                             capture_output=True, text=True, timeout=20).stdout
        out = []
        for g in _parse_rocm_smi(raw):
            card = g.get("card", "")
            idx = int("".join(ch for ch in card if ch.isdigit()) or -1)
            used, total = g.get("vram_used_gib"), g.get("vram_total_gib")
            free = round(total - used, 2) if (used is not None and total is not None) else None
            out.append({"index": idx, "card": card, "used_gib": used,
                        "total_gib": total, "free_gib": free})
        return out or None
    except Exception:
        return None


def gpu_free_gib(ctx, gpu: str) -> float | None:
    vram = read_vram(ctx)
    if not vram:
        return None
    try:
        want = int(str(gpu).split(",")[0])   # first GPU if a list
    except ValueError:
        return None
    for g in vram:
        if g["index"] == want:
            return g["free_gib"]
    return None


# ----------------------------- launch -----------------------------------------

def launch_server(state_dir: str | Path, name: str, command: str, *, cwd: str,
                  gpu: str | None, source_env: bool, env_setup: str | None,
                  env_extra: dict | None = None) -> dict:
    """Start `command` detached, pinned to `gpu`. Mirrors the job runner: own
    session (survives parent), GPU_MAX_HW_QUEUES=1, optional rdna4-env source.
    Returns {pid, log_dir, stdout, stderr}."""
    d = _server_dir(state_dir, name)
    d.mkdir(parents=True, exist_ok=True)
    Path(cwd).mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.setdefault("GPU_MAX_HW_QUEUES", "1")
    if gpu is not None:
        env["HIP_VISIBLE_DEVICES"] = str(gpu)
    env.update({k: str(v) for k, v in (env_extra or {}).items()})

    source_line = ""
    if source_env and env_setup:
        # JAYNET_/ORCH_ dual read for $VAR references in the config value
        if "JAYNET_LLAMA" not in os.environ and "ORCH_LLAMA" in os.environ:
            os.environ["JAYNET_LLAMA"] = os.environ["ORCH_LLAMA"]
        env_setup = os.path.expanduser(os.path.expandvars(env_setup))
        if Path(env_setup).exists():
            source_line = f"source {shlex.quote(env_setup)}\n"

    run_sh = d / "run.sh"
    run_sh.write_text(
        "#!/usr/bin/env bash\nset -o pipefail\n"
        f"{source_line}exec {command}\n")
    run_sh.chmod(0o755)

    stdout_log, stderr_log = d / "stdout.log", d / "stderr.log"
    with stdout_log.open("wb") as out, stderr_log.open("wb") as err:
        proc = subprocess.Popen(["bash", str(run_sh)], cwd=cwd, env=env,
                                stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    return {"pid": proc.pid, "log_dir": str(d),
            "stdout": str(stdout_log), "stderr": str(stderr_log),
            "gpus": env.get("HIP_VISIBLE_DEVICES")}


def stop_server(entry: dict, grace_s: float = 6.0) -> bool:
    """SIGTERM the server's process group, then SIGKILL if it lingers.

    Pid-reuse guard: launch_server starts `bash <log_dir>/run.sh`, so before any
    signal we require that run.sh path in /proc/<pid>/cmdline. On mismatch the
    pid was recycled by an unrelated process — refuse to kill (return False) and
    let the caller clear the stale registry entry."""
    pid = entry.get("pid")
    if not pid or not pid_alive(pid):
        return False
    marker = str(Path(entry["log_dir"]) / "run.sh") if entry.get("log_dir") else ""

    def identity_ok() -> bool:
        return bool(marker) and marker in pid_cmdline(int(pid))

    if not identity_ok():
        return False
    try:
        pgid = os.getpgid(int(pid))
    except OSError:
        pgid = int(pid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    if identity_ok():   # re-verify before escalating — the pid may have changed hands
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return True


def tail(path: str | Path, n: int = 20) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


# ----------------------------- health & model id ------------------------------

async def wait_healthy(base_url: str, timeout_s: float, pid: int | None = None) -> bool:
    deadline = time.time() + timeout_s
    async with httpx.AsyncClient(timeout=5) as c:
        while time.time() < deadline:
            if pid is not None and not pid_alive(pid):
                return False
            for path in ("/health", "/v1/models"):
                try:
                    r = await c.get(base_url + path)
                    if r.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(1.0)
    return False


async def health_now(base_url: str) -> dict:
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            r = await c.get(base_url + "/health")
            return {"ok": r.status_code == 200, "code": r.status_code,
                    "body": r.text[:200]}
        except httpx.HTTPError as e:
            return {"ok": False, "code": None, "body": str(e)[:200]}


class EndpointAuth(Exception):
    """The endpoint answered 401/403 — either no api_key_env is configured on
    the preset, or the key it names is wrong."""


async def query_model_ids(base_url: str, api_key: str | None = None) -> list[str] | None:
    """All model ids the server reports on /v1/models (None when unreachable
    or not OpenAI-shaped). llama-server serves exactly one; vLLM/Ollama may
    list several — callers matching a preset's served_id should scan them all.
    `api_key` sends a Bearer header for keyed adopted servers.
    Raises EndpointAuth on 401/403 so callers can say "key required" instead of
    misreporting the endpoint as empty."""
    headers = {"Authorization": "Bearer " + api_key} if api_key else None
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            r = await c.get(base_url + "/v1/models", headers=headers)
            if r.status_code in (401, 403):
                raise EndpointAuth(
                    f"{base_url} requires an API key (HTTP {r.status_code})")
            data = r.json().get("data", [])
            return [m["id"] for m in data
                    if isinstance(m, dict) and m.get("id")]
        except (httpx.HTTPError, json.JSONDecodeError):
            return None


async def query_model_id(base_url: str) -> str | None:
    ids = await query_model_ids(base_url)
    return ids[0] if ids else None


# ----------------------------- LiteLLM registration ---------------------------

async def litellm_register(admin_base: str, key: str, alias: str,
                           base_url: str, served_id: str) -> tuple[bool, str]:
    """Best-effort: register a served model as a LiteLLM alias so the existing
    call path (`agent.spawn(model=alias)`, `llm.call`) can reach it. Returns
    (ok, detail-or-model_id). Depends on the proxy allowing runtime model adds."""
    body = {"model_name": alias,
            "litellm_params": {"model": f"openai/{served_id}",
                               "api_base": base_url + "/v1", "api_key": "sk-local"}}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(admin_base + "/model/new", json=body,
                             headers={"Authorization": "Bearer " + key})
            if r.status_code < 300:
                mid = ""
                try:
                    mid = (r.json().get("model_info") or {}).get("id", "")
                except json.JSONDecodeError:
                    pass
                return True, mid
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
        except httpx.HTTPError as e:
            return False, str(e)[:160]


async def litellm_deregister(admin_base: str, key: str, model_id: str) -> bool:
    if not model_id:
        return False
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(admin_base + "/model/delete", json={"id": model_id},
                             headers={"Authorization": "Bearer " + key})
            return r.status_code < 300
        except httpx.HTTPError:
            return False
