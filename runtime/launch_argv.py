"""launch_argv — translate a preset .conf into non-llama argv + env.

The preset conf stays the canonical store (one KEY=VALUE set for every
backend); this module is the per-backend renderer the launcher hands off to
when the preset's binary has registry type vllm/radiance/saber (llama argv
stays in scripts/start-model.sh). Switching a preset's binary to another
backend type is therefore the translation: shared keys land in each backend's
own flags, honored-nowhere keys stay in the conf, inert, and every dropped key
is named in a warning so the admin sees exactly what the switch suspends.

Stdlib-only: start-model.sh runs this with the system python3.

CLI (name mode via start-model.sh; --dry-run prints instead of exec):

    launch_argv.py --conf FILE --type {vllm,radiance,saber} --binary PATH
                   --device-env VAR --host H --port P --gpu LIST
                   --alias NAME [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys

# Keys this renderer consumes (both backends unless noted). Everything else a
# conf may carry is llama-only and dropped with a warning when set.
_KNOWN_LLAMA_ONLY = (
    "GPU_LAYERS", "TEMP", "TOP_K", "TOP_P", "MIN_P", "PRESENCE_PENALTY",
    "REPEAT_PENALTY", "BATCH_SIZE", "UBATCH_SIZE", "FLASH_ATTN", "SPLIT_MODE",
    "TENSOR_SPLIT", "MMAP", "CACHE_TYPE_K", "CACHE_TYPE_V", "MMPROJ",
    "MMPROJ_OFFLOAD", "MTP", "MTP_MODEL", "SPEC_DRAFT_N_MAX", "THREADS",
    "JINJA", "EMBEDDINGS", "RERANKING", "POOLING", "REASONING_FORMAT",
    "REASONING_BUDGET", "REASONING_EFFORT", "WHISPER", "MEDIA_MARKER",
)
# Slot keys the conf may carry (owned by the catalog/caller, never flags).
_SLOT_KEYS = ("PORT", "HOST", "ALIAS", "VISIBLE_DEVICES", "LLAMA_BIN",
              "DEVICE_ENV", "SERVER")

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def parse_conf(path: str) -> dict[str, str]:
    """KEY=VALUE lines; inline comments stripped, $ORCH_MODELS/$JAYNET_MODELS
    expanded textually — mirrors scripts/start-model.sh's parser."""
    models = os.environ.get("ORCH_MODELS") or os.environ.get("JAYNET_MODELS") or ""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, val = line.partition("=")
            if not sep or not key.strip() or key.strip().startswith("#"):
                continue
            val = val.split("#", 1)[0].strip()
            for var in ("${ORCH_MODELS}", "$ORCH_MODELS",
                        "${JAYNET_MODELS}", "$JAYNET_MODELS"):
                val = val.replace(var, models)
            out[key.strip()] = val
    return out


def _saber_occamy_env(conf: dict[str, str], cards: list[str], *, host: str,
                      port: str, alias: str) -> dict[str, str]:
    """Validate the separate Occamy fork's packs and render its launcher env."""
    if len(cards) != 2:
        raise ValueError("saber Occamy needs two consecutive GPUs (TP=2)")
    try:
        first, second = (int(c) for c in cards)
    except ValueError:
        raise ValueError("saber Occamy needs two consecutive GPUs (TP=2)") from None
    if second != first + 1:
        raise ValueError("saber Occamy needs two consecutive GPUs (TP=2)")
    paths = {
        "OCCAMY_FP8": (conf.get("MODEL_PATH") or "").strip(),
        "OCCAMY_LAYERS": (conf.get("OCCAMY_LAYERS") or "").strip(),
        "OCCAMY_GLOBALS": (conf.get("OCCAMY_GLOBALS") or "").strip(),
        "OCCAMY_CHECKPOINT": (conf.get("OCCAMY_CHECKPOINT") or "").strip(),
    }
    for key, path in paths.items():
        if not path or not os.path.isdir(path):
            raise ValueError(f"saber Occamy directory not found: {key}={path or '<unset>'}")
    if not os.path.isfile(os.path.join(paths["OCCAMY_CHECKPOINT"], "config.json")):
        raise ValueError("saber Occamy checkpoint has no config.json")
    mtp = (conf.get("MTP_MODEL") or "").strip()
    if mtp and not os.path.isfile(mtp):
        raise ValueError(f"saber Occamy MTP file not found: {mtp}")
    max_requests = conf.get("MAX_NUM_SEQS") or "1"
    if mtp and max_requests != "1":
        raise ValueError("saber Occamy MTP needs MAX_NUM_SEQS=1")
    repo = (conf.get("SABER_REPO") or "").strip()
    binary = (conf.get("SABER_BIN") or "").strip()
    if repo and not os.path.isdir(repo):
        raise ValueError(f"saber Occamy repository not found: {repo}")
    if binary and not os.path.isfile(binary):
        raise ValueError(f"saber Occamy binary not found: {binary}")
    env = {
        "PORT": str(port), "HOST": host, "SABER_MODEL": "occamy",
        **paths, "OCCAMY_MTP": mtp,
        "MODEL": alias or "occamy-1.0", "CNAME": f"saber-jaynet-{port}",
        "HIP_DEVICES": ",".join(cards), "DEVICE": str(first),
        "MAX_REQUESTS": max_requests,
        "SPEC_DEPTH": conf.get("SPEC_DRAFT_N_MAX") or "1",
    }
    if repo:
        env["SABER_REPO"] = repo
    if binary:
        env["SABER_BIN"] = binary
    if conf.get("CTX_SIZE"):
        env["MAX_CTX"] = conf["CTX_SIZE"]
    return env


def _saber_env(conf: dict[str, str], cards: list[str], *, host: str,
               port: str, alias: str) -> dict[str, str]:
    """Validate a managed Saber pack and render its native launcher's env."""
    model = (conf.get("MODEL_PATH") or "").strip()
    if conf.get("SABER_MODEL") == "occamy":
        return _saber_occamy_env(conf, cards, host=host, port=port, alias=alias)

    if conf.get("SABER_MODEL") == "flashnext":
        if len(cards) != 2:
            raise ValueError("saber Flash-Next needs two GPUs (TP=2)")
        if not os.path.isdir(model):
            raise ValueError(f"saber Flash-Next pack directory not found: {model}")
        for relative in ("manifest.json", "rank0/trunk8.pack", "rank1/trunk8.pack"):
            if not os.path.isfile(os.path.join(model, relative)):
                raise ValueError(f"saber Flash-Next pack file not found: {model}/{relative}")
        env = {
            "PORT": str(port), "HOST": host, "SABER_MODEL": "flashnext",
            "PACK": model, "SABER_NVME": os.path.dirname(os.path.dirname(
                os.path.abspath(model))),
            "MODEL": alias or "Qwen3.8-Flash-Next",
            "CNAME": f"saber-jaynet-{port}", "HIP_DEVICES": ",".join(cards),
        }
        if conf.get("CTX_SIZE"):
            env["MAX_CTX"] = conf["CTX_SIZE"]
        return env

    if len(cards) not in (1, 2):
        raise ValueError("saber 27B needs one GPU (TP=1) or two GPUs (TP=2)")
    draft = (conf.get("MTP_MODEL") or "").strip()
    if not model or not draft:
        raise ValueError("saber needs MODEL_PATH (target pack directory) and "
                         "MTP_MODEL (DFlash2 pack directory)")
    if not os.path.isdir(model) or not os.path.isdir(draft):
        raise ValueError(f"saber pack directory not found: {model} or {draft}")
    if os.path.dirname(os.path.abspath(model)) != os.path.dirname(os.path.abspath(draft)):
        raise ValueError("saber target and DFlash2 packs must share one parent directory")
    tp1 = len(cards) == 1
    for pack in (model, draft):
        if ("-tp1-" in os.path.basename(pack)) != tp1:
            raise ValueError("saber TP=1 needs -tp1- packs; TP=2 needs the two-rank packs")
        ranks = ("rank0.pack",) if tp1 else ("rank0.pack", "rank1.pack")
        for rank in ranks:
            if not os.path.isfile(os.path.join(pack, rank)):
                raise ValueError(f"saber pack file not found: {pack}/{rank}")
    env = {
        "PORT": str(port), "HOST": host,
        "SABER_PACKS": os.path.dirname(os.path.abspath(model)),
        "TIER": os.path.basename(model), "DRAFT": os.path.basename(draft),
        "MODEL": alias or "Qwen3.8", "CNAME": f"saber-jaynet-{port}",
    }
    if conf.get("CTX_SIZE"):
        env["MAX_CTX"] = conf["CTX_SIZE"]
    if conf.get("MAX_NUM_SEQS"):
        env["MAX_SEQS"] = conf["MAX_NUM_SEQS"]
    if tp1:
        env["DEVICE"] = cards[0]
    else:
        env["HIP_DEVICES"] = ",".join(cards)
    return env


def build_argv(backend: str, conf: dict[str, str], *, binary: str,
               device_env: str, host: str, port: str, gpu: str,
               alias: str) -> tuple[list[str], dict[str, str], list[str]]:
    """(argv, env, warnings) for backend ∈ {vllm, radiance, saber}.

    gpu is the catalog placement: "" = CPU (refused — all are GPU engines),
    "0" = pin one card, "0,1" = spread over two cards. env carries the
    backend's device pinning; warnings name every dropped key.
    """
    if backend not in ("vllm", "radiance", "saber"):
        raise ValueError(f"unknown backend {backend!r}")
    gpu = (gpu or "").strip()
    if not gpu:
        raise ValueError(f"{backend} is a GPU engine — the preset's device "
                         "may not be empty (CPU); pick a llama binary instead")
    model = (conf.get("MODEL_PATH") or "").strip()
    if not model:
        raise ValueError("MODEL_PATH may not be empty")
    if backend != "saber" and not os.path.isfile(model) and not _REPO_RE.match(model):
        raise ValueError(f"model not found: {model} (not a file, not an "
                         "owner/name HF repo id)")

    warns: list[str] = []
    for key in _KNOWN_LLAMA_ONLY:
        saber_key = backend == "saber" and (
            key == "MTP_MODEL"
            or (conf.get("SABER_MODEL") == "occamy" and key == "SPEC_DRAFT_N_MAX"))
        if conf.get(key) and not saber_key:
            warns.append(f"{key} dropped: llama-only, no {backend} equivalent")
    extra = shlex.split(conf.get("EXTRA_ARGS") or "")
    cards = [c for c in gpu.split(",") if c.strip()]

    argv = [binary]
    env: dict[str, str] = {}
    if device_env and backend != "saber":
        env[device_env] = gpu

    if backend == "vllm":
        argv += ["serve", model, "--host", host, "--port", str(port),
                 "--tensor-parallel-size", str(len(cards))]
        if conf.get("CTX_SIZE"):
            argv += ["--max-model-len", conf["CTX_SIZE"]]
        if conf.get("KV_CACHE_DTYPE"):
            argv += ["--kv-cache-dtype", conf["KV_CACHE_DTYPE"]]
        if conf.get("GPU_MEM_UTIL"):
            argv += ["--gpu-memory-utilization", conf["GPU_MEM_UTIL"]]
        if conf.get("MAX_NUM_SEQS"):
            argv += ["--max-num-seqs", conf["MAX_NUM_SEQS"]]
        if conf.get("TOOLS_TEMPLATE"):
            argv += ["--chat-template", conf["TOOLS_TEMPLATE"]]
        if alias:
            argv += ["--served-model-name", alias]
    elif backend == "radiance":
        argv += ["--model", model, "--host", host, "--port", str(port),
                 "--tp", str(len(cards))]
        if conf.get("CTX_SIZE"):
            argv += ["--max-model-len", conf["CTX_SIZE"]]
        if conf.get("KV_CACHE_DTYPE"):
            argv += ["--kv-cache-dtype", conf["KV_CACHE_DTYPE"]]
        if conf.get("MAX_NUM_SEQS"):
            argv += ["--max-num-seqs", conf["MAX_NUM_SEQS"]]
        if conf.get("GPU_MEM_UTIL"):
            warns.append("GPU_MEM_UTIL dropped: vllm-only, radiance budgets "
                         "memory itself (--gpu-headroom-mib via EXTRA_ARGS)")
        if conf.get("TOOLS_TEMPLATE"):
            warns.append("TOOLS_TEMPLATE dropped: a .rad container carries "
                         "its own chat template")
        if alias:
            warns.append(f"ALIAS {alias!r} not settable: radiance reports "
                         "its own model id (match served_id to it)")
    else:
        env = _saber_env(conf, cards, host=host, port=port, alias=alias)
        for key in ("KV_CACHE_DTYPE", "GPU_MEM_UTIL", "TOOLS_TEMPLATE"):
            if conf.get(key):
                warns.append(f"{key} dropped: no saber 27B equivalent")
    return argv + extra, env, warns


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conf", required=True)
    ap.add_argument("--type", required=True, choices=("vllm", "radiance", "saber"),
                    dest="backend")
    ap.add_argument("--binary", required=True)
    ap.add_argument("--device-env", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="8080")
    ap.add_argument("--gpu", default="")
    ap.add_argument("--alias", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        conf = parse_conf(args.conf)
        cmd, env, warns = build_argv(
            args.backend, conf, binary=args.binary,
            device_env=args.device_env, host=args.host, port=args.port,
            gpu=args.gpu, alias=args.alias)
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("-------------------------------------------------------")
    print(f"  MODEL: {os.path.basename(conf.get('MODEL_PATH', '?'))}")
    print(f"  backend: {args.backend}  port: {args.host}:{args.port}  "
          f"gpu: {args.gpu}")
    print(f"  bin: {os.path.basename(args.binary)}")
    for w in warns:
        print(f"  ! {w}")
    print("-------------------------------------------------------")
    if args.dry_run:
        for k, v in env.items():
            print(f"{k}={shlex.quote(v)}")
        print(shlex.join(cmd))
        return 0
    if not os.access(args.binary, os.X_OK):
        print(f"Error: server binary not executable: {args.binary}",
              file=sys.stderr)
        return 1
    os.execvpe(args.binary, cmd, {**os.environ, **env})
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
