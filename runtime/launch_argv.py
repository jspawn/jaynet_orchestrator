"""launch_argv — translate a preset .conf into vllm/radiance argv + env.

The preset conf stays the canonical store (one KEY=VALUE set for every
backend); this module is the per-backend renderer the launcher hands off to
when the preset's binary has registry type vllm/radiance (llama argv stays in
scripts/start-model.sh). Switching a preset's binary to another backend type
is therefore the translation: shared keys (model, ctx, GPU cards, KV cache,
chat template, extra args) land in each backend's own flags, honored-nowhere
keys stay in the conf, inert, and every dropped key is named in a warning so
the admin sees exactly what the switch suspends.

Stdlib-only: start-model.sh runs this with the system python3.

CLI (name mode via start-model.sh; --dry-run prints instead of exec):

    launch_argv.py --conf FILE --type {vllm,radiance} --binary PATH
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


def build_argv(backend: str, conf: dict[str, str], *, binary: str,
               device_env: str, host: str, port: str, gpu: str,
               alias: str) -> tuple[list[str], dict[str, str], list[str]]:
    """(argv, env, warnings) for backend ∈ {vllm, radiance}.

    gpu is the catalog placement: "" = CPU (refused — both are GPU engines),
    "0" = pin one card, "0,1" = spread over N cards (--tensor-parallel-size /
    --tp N). env carries the device pinning; warnings name every dropped key.
    """
    if backend not in ("vllm", "radiance"):
        raise ValueError(f"unknown backend {backend!r}")
    gpu = (gpu or "").strip()
    if not gpu:
        raise ValueError(f"{backend} is a GPU engine — the preset's device "
                         "may not be empty (CPU); pick a llama binary instead")
    model = (conf.get("MODEL_PATH") or "").strip()
    if not model:
        raise ValueError("MODEL_PATH may not be empty")
    if not os.path.isfile(model) and not _REPO_RE.match(model):
        raise ValueError(f"model not found: {model} (not a file, not an "
                         "owner/name HF repo id)")

    warns: list[str] = []
    for key in _KNOWN_LLAMA_ONLY:
        if conf.get(key):
            warns.append(f"{key} dropped: llama-only, no {backend} equivalent")
    extra = shlex.split(conf.get("EXTRA_ARGS") or "")
    cards = [c for c in gpu.split(",") if c.strip()]

    argv = [binary]
    env: dict[str, str] = {}
    if device_env:
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
    else:  # radiance
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
    return argv + extra, env, warns


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conf", required=True)
    ap.add_argument("--type", required=True, choices=("vllm", "radiance"),
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
