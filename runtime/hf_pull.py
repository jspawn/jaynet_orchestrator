"""HuggingFace GGUF pulls — shared core for scripts/pull-model (CLI) and the
admin downloader (web/routes_admin.py).

Stdlib-only (the CLI runs on the system python). Two halves:

- Listing/resolution: validate a repo id, list its downloadable files
  (.gguf models/mmprojs, .jinja chat templates) with sizes (?blobs=true),
  build the resolve URL and the confined target path.
- A tiny job manager for the web side: each download runs in a daemon
  thread streaming to <file>.part (renamed on success, deleted on
  cancel/error), with byte progress the admin UI polls. Jobs live in
  process memory — a restart forgets them, the .part file is the residue.

suggest_preset() turns a finished download into a preset skeleton (name,
.conf body, VRAM estimate) so the admin editor opens prefilled.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from runtime import paths

# ?blobs=true makes the API include per-file sizes in "siblings".
HF_API = "https://huggingface.co/api/models/{repo}?blobs=true"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{file}"

_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/ -]*\.(gguf|jinja)$", re.IGNORECASE)
_CHUNK = 1 << 20


class HfError(ValueError):
    """User-facing failure (bad repo, no GGUFs, HF API error)."""


def _request(url: str) -> urllib.request.Request:
    """HF_TOKEN (from the service env) authenticates API + download calls —
    needed for gated repos, raises anonymous rate limits otherwise."""
    req = urllib.request.Request(url)
    tok = os.environ.get("HF_TOKEN", "").strip()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    return req


def validate_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if not _REPO_RE.match(repo):
        raise HfError(f"invalid repo id {repo!r} — expected 'org/name'")
    return repo


def validate_filename(name: str) -> str:
    name = (name or "").strip()
    if not _FILE_RE.match(name) or ".." in name:
        raise HfError(f"invalid filename {name!r} — only .gguf / .jinja")
    return name


def kind_of(name: str) -> str:
    """'gguf' or 'jinja' — the two file kinds the downloader handles."""
    return "jinja" if name.lower().endswith(".jinja") else "gguf"


def list_files(repo: str) -> list[tuple[str, int | None, str]]:
    """(filename, size|None, kind) for every downloadable file (.gguf models
    and mmprojs, .jinja chat templates) in the repo, sorted by name."""
    repo = validate_repo(repo)
    try:
        with urllib.request.urlopen(_request(HF_API.format(repo=repo)),
                                    timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise HfError(f"HF API returned {e.code} for {repo} — repo exists?")
    except Exception as e:
        raise HfError(f"could not reach HuggingFace: {type(e).__name__}: {e}")
    return sorted((s["rfilename"], s.get("size"), kind_of(s["rfilename"]))
                  for s in data.get("siblings", [])
                  if _FILE_RE.match(s.get("rfilename", "")))


def list_gguf(repo: str) -> list[tuple[str, int | None]]:
    """(filename, size|None) for every .gguf in the repo, sorted by name —
    the CLI view (templates are a web-side extra)."""
    return [(n, s) for n, s, k in list_files(repo) if k == "gguf"]


def resolve_url(repo: str, filename: str) -> str:
    return HF_RESOLVE.format(repo=validate_repo(repo),
                             file=urllib.parse.quote(validate_filename(filename)))


def target_path(repo: str, filename: str) -> Path:
    """<MODELS_DIR>/<repo>/<file> — confined: the name was validated above."""
    dest = (paths.MODELS_DIR / validate_repo(repo)
            / validate_filename(filename)).resolve()
    if not str(dest).startswith(str(paths.MODELS_DIR)):
        raise HfError("target escapes the models dir")
    return dest


def stream_download(repo: str, filename: str, dest: Path,
                    progress=None, cancelled=None) -> Path:
    """Stream to <dest>.part, rename on success. `progress(done, total)` is
    called per chunk; `cancelled()` (truthy → abort) is checked between
    chunks. Raises HfError on failure; the .part file is removed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(_request(resolve_url(repo, filename)),
                                    timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with part.open("wb") as f:
                while True:
                    if cancelled and cancelled():
                        raise HfError("cancelled")
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.rename(dest)
    return dest


# ---- web-side job manager ----------------------------------------------------

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def start_job(repo: str, filename: str) -> dict:
    """Start a background download. Raises HfError when the target exists or
    the same file is already downloading."""
    dest = target_path(repo, filename)
    with _LOCK:
        for j in _JOBS.values():
            if (j["repo"], j["file"]) == (repo, filename) and j["status"] == "running":
                raise HfError(f"{filename} is already downloading")
        if dest.exists():
            raise HfError(f"{filename} already exists at {dest}")
        job = {"id": uuid.uuid4().hex[:12], "repo": repo, "file": filename,
               "dest": str(dest), "status": "running",
               "done_bytes": 0, "total_bytes": 0, "error": None,
               "started": time.time(), "finished": None, "_cancel": False}
        _JOBS[job["id"]] = job

    def _progress(done, total):
        job["done_bytes"], job["total_bytes"] = done, total

    def _run():
        try:
            stream_download(repo, filename, dest, progress=_progress,
                            cancelled=lambda: job["_cancel"])
            job["status"] = "done"
        except Exception as e:
            job["status"] = "cancelled" if job["_cancel"] else "error"
            job["error"] = None if job["_cancel"] else str(e)
        job["finished"] = time.time()

    threading.Thread(target=_run, daemon=True).start()
    return public_job(job)


def public_job(j: dict) -> dict:
    return {k: v for k, v in j.items() if not k.startswith("_")}


def jobs() -> list[dict]:
    with _LOCK:
        return [public_job(j) for j in
                sorted(_JOBS.values(), key=lambda j: j["started"], reverse=True)]


def cancel_job(job_id: str) -> dict | None:
    j = _JOBS.get(job_id)
    if j and j["status"] == "running":
        j["_cancel"] = True
    return public_job(j) if j else None


def dismiss_job(job_id: str) -> bool:
    """Forget a finished job (running ones refuse)."""
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j or j["status"] == "running":
            return False
        del _JOBS[job_id]
        return True


def clean_stale_parts(min_age_s: float = 3600) -> int:
    """Remove *.part leftovers under MODELS_DIR older than min_age_s —
    residue of downloads aborted by a restart or a killed CLI pull. The age
    floor keeps a download running in ANOTHER process (e.g. the CLI while
    the web service boots) safe. Returns the number removed."""
    root = paths.MODELS_DIR
    now = time.time()
    removed = 0
    try:
        walker = root.rglob("*.part")
    except OSError:
        return 0
    for p in walker:
        try:
            if now - p.stat().st_mtime > min_age_s:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# ---- preset suggestion --------------------------------------------------------

_NAME_FALLBACK = re.compile(r"[^a-z0-9]+")


def suggest_name(filename: str) -> str:
    """'Qwen3-0.6B-Q8_0.gguf' → 'qwen3-0-6b-q8-0' (preset-name safe)."""
    stem = re.sub(r"\.gguf$", "", filename, flags=re.IGNORECASE)
    name = _NAME_FALLBACK.sub("-", stem.lower()).strip("-")[:64].strip("-")
    return name or "pulled-model"


def suggest_preset(repo: str, filename: str, *, port: int | None = None) -> dict:
    """Preset skeleton for a finished download: name, .conf body, VRAM
    estimate (file size + ~10% context overhead). The admin editor still
    decides device/alias/slot — this is a starting point, not a launch.

    Sibling files in the same repo are wired in when found: an mmproj*.gguf
    fills MMPROJ (+ offload), a *.jinja fills TOOLS_TEMPLATE. Each is
    reported with a `downloaded` flag so the UI can nudge when the conf
    references a file that isn't pulled yet."""
    repo = validate_repo(repo)
    filename = validate_filename(filename)
    if kind_of(filename) != "gguf":
        raise HfError("preset suggestions only make sense for a .gguf model")
    dest = target_path(repo, filename)
    name = suggest_name(Path(filename).name)
    size_gib = (dest.stat().st_size / (1 << 30)) if dest.exists() else 0.0
    vram = round(size_gib * 1.1, 1) if size_gib else None
    conf_lines = [
        f"# {repo} / {filename} — pulled {time.strftime('%Y-%m-%d')}",
        f"MODEL_PATH={dest}",
        f"ALIAS={name}",
        "HOST=127.0.0.1",
        f"PORT={port or 8080}",
        "GPU_LAYERS=99",
        "THREADS=8",
        "CTX_SIZE=32768",
        "EMBEDDINGS=off",
        "JINJA=yes",
        "FLASH_ATTN=auto",
    ]
    out = {"name": name, "vram_gib": vram, "port": port, "path": str(dest)}
    try:
        siblings = list_files(repo)
    except HfError:
        siblings = []                    # offline/API hiccup — skip extras
    mmproj = next((n for n, _, k in siblings
                   if k == "gguf" and Path(n).name.lower().startswith("mmproj")
                   and n != filename), None)
    if mmproj:
        mp = target_path(repo, mmproj)
        conf_lines += [f"MMPROJ={mp}", "MMPROJ_OFFLOAD=on"]
        out["mmproj"] = {"file": mmproj, "downloaded": mp.exists()}
    template = next((n for n, _, k in siblings if k == "jinja"), None)
    if template:
        tp = target_path(repo, template)
        conf_lines.append(f"TOOLS_TEMPLATE={tp}")
        out["chat_template"] = {"file": template, "downloaded": tp.exists()}
    conf_lines += ["EXTRA_ARGS=", "CACHE_TYPE_K=f16", "CACHE_TYPE_V=f16", ""]
    out["conf"] = "\n".join(conf_lines)
    return out
