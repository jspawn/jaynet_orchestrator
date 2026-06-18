#!/usr/bin/env bash
# Launch the orchestrator's llama-server (the local "brain") on :8090.
#
# Runs in the FOREGROUND and exits gracefully on Ctrl-C (clean SIGTERM to
# llama-server, no orphaned process pegging a GPU). Single-purpose: starts one
# daemon. (The LiteLLM proxy is started separately — by orch-up.sh during manual
# debugging, or by its own systemd unit later.)
#
# Reuses the ROCm llama-server built by /srv/llama/build_tools.sh — compiles
# nothing. After a pacman -Syu that touches ROCm/kernel/Mesa, rebuild via
# build_tools.sh and restart.
#
# Model + sampling params come from a llama-serve.sh PRESET (.conf), so you can
# tune the model interactively with llama-serve.sh ("save as model default"),
# and this service consumes the exact same config. Port, GPU pin, and the
# tool-calling chat template are orchestrator INVARIANTS and override the preset.

set -euo pipefail

# -- Orchestrator invariants (NOT overridable by the preset) ----------------
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
PORT=8090                                    # clear of serve.sh(8080)/sd(8081)
HOST="${HOST:-127.0.0.1}"
TOOLS_TEMPLATE="/srv/orchestrator/config/qwen3-tools.jinja"

# -- Preset (llama-serve.sh format) -----------------------------------------
PRESET="${PRESET:-/srv/llama/presets/Qwen3.6-35B-A3B-Uncensored-Genesis-MTP-APEX.conf}"
MODEL_PATH="${MODEL_PATH:-/srv/models/LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-V2-APEX-MTP-GGUF/Qwen3.6-35B-A3B-Uncensored-Genesis-MTP-APEX.gguf}"

# -- Defaults (used if the preset omits a key) ------------------------------
CTX_SIZE="32768"
GPU_LAYERS="99"
TEMP="0.6"; TOP_K="20"; TOP_P="0.95"; MIN_P="0"
PRESENCE_PENALTY="0"; REPEAT_PENALTY="1.0"
BATCH_SIZE="2048"; UBATCH_SIZE="512"
FLASH_ATTN="on"
SPLIT_MODE=""; TENSOR_SPLIT=""

# -- GPU pin (gfx1201/RDNA4). Pin GPU0 by default; ORCH_GPU="" = both --------
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"
ORCH_GPU="${ORCH_GPU:-0}"

# -- Load preset (KEY=value lines, same parser as llama-serve.sh) -----------
if [[ -f "$PRESET" ]]; then
    echo "[*] Loading preset: $PRESET"
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        # Only accept known keys, so a preset can't inject arbitrary vars.
        case "$key" in
            CTX_SIZE|GPU_LAYERS|TEMP|TOP_K|TOP_P|MIN_P|PRESENCE_PENALTY|\
            REPEAT_PENALTY|BATCH_SIZE|UBATCH_SIZE|FLASH_ATTN|SPLIT_MODE|TENSOR_SPLIT)
                printf -v "$key" "%s" "$val" ;;
            # PORT/HOST/BACKEND/VISIBLE_DEVICES from the preset are deliberately
            # IGNORED — the orchestrator pins its own port and GPU.
            *) : ;;
        esac
    done < "$PRESET"
else
    echo "[!] Preset not found ($PRESET) — using built-in defaults." >&2
fi

# -- Validate binary + model ------------------------------------------------
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    echo "Build it with: /srv/llama/build_tools.sh llama rocm" >&2
    exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
    echo "Error: model not found at $MODEL_PATH" >&2
    echo "Check the path, or set MODEL_PATH=/srv/models/<file>.gguf" >&2
    exit 1
fi

# -- Apply GPU visibility ---------------------------------------------------
if [[ -n "$ORCH_GPU" ]]; then
    export HIP_VISIBLE_DEVICES="$ORCH_GPU"
    export ROCR_VISIBLE_DEVICES="$ORCH_GPU"
fi
MULTI_GPU_FLAGS=()
if [[ -z "$ORCH_GPU" ]]; then
    MULTI_GPU_FLAGS=(--split-mode "${SPLIT_MODE:-layer}")
    [[ -n "$TENSOR_SPLIT" ]] && MULTI_GPU_FLAGS+=(--tensor-split "$TENSOR_SPLIT")
fi

# -- Graceful shutdown ------------------------------------------------------
LLAMA_PID=""
cleanup() {
    echo -e "\n[!] Shutting down llama-server..."
    [[ -n "$LLAMA_PID" ]] && kill -TERM "$LLAMA_PID" 2>/dev/null
    [[ -n "$LLAMA_PID" ]] && wait "$LLAMA_PID" 2>/dev/null
    echo "[*] Stopped cleanly."
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "-------------------------------------------------------"
echo "  ORCHESTRATOR BRAIN (llama-server)"
echo "  model:   $(basename "$MODEL_PATH")"
echo "  port:    $HOST:$PORT"
echo "  ctx:     $CTX_SIZE   layers: $GPU_LAYERS"
echo "  gpu:     ${ORCH_GPU:-both}"
echo "  sampling: temp=$TEMP top_k=$TOP_K top_p=$TOP_P"
echo "  Ctrl-C to stop"
echo "-------------------------------------------------------"

# Background + wait (not exec) so the trap can fire on Ctrl-C.
"$LLAMA_BIN" \
    --model "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX_SIZE" \
    --n-gpu-layers "$GPU_LAYERS" \
    "${MULTI_GPU_FLAGS[@]}" \
    --temp "$TEMP" --top-k "$TOP_K" --top-p "$TOP_P" --min-p "$MIN_P" \
    --presence-penalty "$PRESENCE_PENALTY" --repeat-penalty "$REPEAT_PENALTY" \
    --batch-size "$BATCH_SIZE" --ubatch-size "$UBATCH_SIZE" \
    --flash-attn "$FLASH_ATTN" \
    --jinja \
    --chat-template-file "$TOOLS_TEMPLATE" \
    --metrics &
LLAMA_PID=$!
wait "$LLAMA_PID"
