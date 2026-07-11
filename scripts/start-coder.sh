#!/usr/bin/env bash
# Launch the dedicated CODER llama-server on GPU 1 (:8080) — the target for
# code.delegate. Mirrors scripts/start-llama.sh (the brain), but:
#   * pinned to GPU 1 (CODER_GPU=1) so it never contends with the brain on GPU 0
#   * its OWN preset var (CODER_PRESET) — it must NOT reuse the brain's PRESET
#   * coding-tuned sampling defaults (low temp, no presence penalty)
#
# IMPORTANT: code.delegate runs a tool-using sub-agent ON this model, so the
# coder must emit OpenAI-style tool calls. We therefore serve it with the same
# tool-calling chat template as the brain by default (CODER_TOOLS_TEMPLATE);
# set that to "none" only if the model's embedded --jinja template already does
# tools correctly. A coder that can't speak tool calls will fail every delegation.
#
# Runs in the FOREGROUND, clean SIGTERM on stop (no orphaned GPU process).
# Reuses the ROCm llama-server from build_tools.sh — compiles nothing.

set -euo pipefail

# -- Invariants (not overridable by the preset) -----------------------------
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
PORT="${CODER_PORT:-8080}"                    # clear of brain(8090)/litellm(4000)
HOST="${HOST:-127.0.0.1}"
# Chat/tool template default — per model, so each preset may carry its own
# (TOOLS_TEMPLATE=...). Env CODER_TOOLS_TEMPLATE overrides the preset; "none"
# uses the model's embedded template. (Default + env applied around the preset.)
TOOLS_TEMPLATE="${ORCH_HOME:-/srv/orchestrator}/config/qwen3-tools.jinja"

# -- Preset + model (CODER-specific; defaults overridable by env) ------------
# Preset selection. Honor ORCH_CODER_PRESET — the var the orchestrator app and
# ~/.config/orchestrator.env actually set — so the launcher and app agree on the
# coder model. CODER_PRESET remains an explicit override; the built-in default is
# the last resort.
PRESET="${CODER_PRESET:-${ORCH_CODER_PRESET:-/srv/llama/presets/Qwopus3.6-27B-Coder-MTP-Q5_K_M.conf}}"
MODEL_PATH="${CODER_MODEL_PATH:-/srv/models/Qwopus3.6-27B-Coder-MTP-Q5_K_M-GGUF/Qwopus3.6-27B-Coder-MTP-Q5_K_M.gguf}"

# -- Coding-tuned defaults (used if the preset omits a key) -----------------
CTX_SIZE="32768"                              # start safe; MTP forces f16 KV (big)
GPU_LAYERS="99"
TEMP="0.3"; TOP_K="20"; TOP_P="0.95"; MIN_P="0"
PRESENCE_PENALTY="0"; REPEAT_PENALTY="1.0"    # code repeats tokens legitimately
BATCH_SIZE="2048"; UBATCH_SIZE="512"
FLASH_ATTN="on"
CACHE_TYPE_K="f16"; CACHE_TYPE_V="f16"        # symmetric -> HIP fused flash-attn
MTP="on"; SPEC_DRAFT_N_MAX="2"                # MTP head; n-max 2 safest for MoE

# -- GPU pin (gfx1201/RDNA4). GPU 1 by default; keeps GPU 0 for the brain. ---
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"
CODER_GPU="${CODER_GPU:-1}"

# -- Load preset (KEY=value; same allow-listed parser as the brain) ---------
if [[ -f "$PRESET" ]]; then
    echo "[*] Loading coder preset: $PRESET"
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        case "$key" in
            CTX_SIZE|GPU_LAYERS|TEMP|TOP_K|TOP_P|MIN_P|PRESENCE_PENALTY|\
            REPEAT_PENALTY|BATCH_SIZE|UBATCH_SIZE|FLASH_ATTN|\
            CACHE_TYPE_K|CACHE_TYPE_V|MTP|SPEC_DRAFT_N_MAX|TOOLS_TEMPLATE)
                printf -v "$key" "%s" "$val" ;;
            MODEL_PATH)
                [[ -z "${CODER_MODEL_PATH:-}" ]] && printf -v MODEL_PATH "%s" "$val" ;;
            # PORT/HOST/GPU/ALIAS in the preset are ignored: this service pins its
            # own port + GPU, and LiteLLM owns the alias.
            *) : ;;
        esac
    done < "$PRESET"
else
    echo "[!] Coder preset not found ($PRESET) — using built-in defaults." >&2
fi

# Precedence: env override > preset > built-in default. Empty (e.g. a preset
# saved by llama-serve.sh) falls back to the default rather than erroring; set
# "none" explicitly to serve the model's embedded template.
TOOLS_TEMPLATE="${CODER_TOOLS_TEMPLATE:-$TOOLS_TEMPLATE}"
[[ -z "$TOOLS_TEMPLATE" ]] && TOOLS_TEMPLATE="${ORCH_HOME:-/srv/orchestrator}/config/qwen3-tools.jinja"

# -- Validate binary + model ------------------------------------------------
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    echo "Build it with: /srv/llama/build_tools.sh llama rocm" >&2
    exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
    echo "Error: coder model not found at $MODEL_PATH" >&2
    echo "Set CODER_MODEL_PATH=/srv/models/<file>.gguf (or fix the preset)." >&2
    exit 1
fi

# -- MTP self-speculative decoding ------------------------------------------
SPEC_FLAGS=()
if [[ "$MTP" == "on" ]]; then
    CACHE_TYPE_K="f16"; CACHE_TYPE_V="f16"    # quantized KV collapses draft acceptance
    SPEC_FLAGS=(--spec-type draft-mtp --spec-draft-n-max "$SPEC_DRAFT_N_MAX")
fi

# -- Single-GPU pin: one visible device, so no split / main-gpu needed ------
# Use exactly ONE visibility knob. Setting BOTH HIP_VISIBLE_DEVICES and
# ROCR_VISIBLE_DEVICES composes: ROCR filters to the chosen GPU FIRST (it
# becomes index 0), then HIP indexes INTO that filtered set — so HIP=1 lands
# past the end and you get "no ROCm-capable device detected" (CPU fallback).
# HIP_VISIBLE_DEVICES alone makes ggml's HIP backend enumerate all GPUs and pick
# index $CODER_GPU directly. (The brain only survives the both-set pattern
# because it targets GPU 0, where 0/0 is a no-op.)
unset ROCR_VISIBLE_DEVICES
export HIP_VISIBLE_DEVICES="$CODER_GPU"

# -- Tool-calling chat template ---------------------------------------------
TEMPLATE_FLAGS=(--jinja)
if [[ "$TOOLS_TEMPLATE" != "none" ]]; then
    if [[ ! -f "$TOOLS_TEMPLATE" ]]; then
        echo "Error: tool-calling template not found: $TOOLS_TEMPLATE" >&2
        echo "Fix CODER_TOOLS_TEMPLATE, or set it to 'none' to use the model's own." >&2
        exit 1
    fi
    TEMPLATE_FLAGS+=(--chat-template-file "$TOOLS_TEMPLATE")
fi

# -- Graceful shutdown ------------------------------------------------------
LLAMA_PID=""
cleanup() {
    echo -e "\n[!] Shutting down coder llama-server..."
    [[ -n "$LLAMA_PID" ]] && kill -TERM "$LLAMA_PID" 2>/dev/null
    [[ -n "$LLAMA_PID" ]] && wait "$LLAMA_PID" 2>/dev/null
    echo "[*] Stopped cleanly."
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "-------------------------------------------------------"
echo "  CODER (llama-server)  [code.delegate target]"
echo "  model:   $(basename "$MODEL_PATH")"
echo "  port:    $HOST:$PORT     gpu: $CODER_GPU"
echo "  ctx:     $CTX_SIZE   layers: $GPU_LAYERS   kv: $CACHE_TYPE_K/$CACHE_TYPE_V"
echo "  sampling: temp=$TEMP top_k=$TOP_K top_p=$TOP_P presence=$PRESENCE_PENALTY"
[[ "$MTP" == "on" ]] && echo "  mtp:     draft-mtp, n-max=$SPEC_DRAFT_N_MAX"
echo "  tools:   $([[ "$TOOLS_TEMPLATE" == none ]] && echo 'model template (--jinja)' || basename "$TOOLS_TEMPLATE")"
echo "  Ctrl-C to stop"
echo "-------------------------------------------------------"

"$LLAMA_BIN" \
    --model "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX_SIZE" \
    --n-gpu-layers "$GPU_LAYERS" \
    --temp "$TEMP" --top-k "$TOP_K" --top-p "$TOP_P" --min-p "$MIN_P" \
    --presence-penalty "$PRESENCE_PENALTY" --repeat-penalty "$REPEAT_PENALTY" \
    --batch-size "$BATCH_SIZE" --ubatch-size "$UBATCH_SIZE" \
    --cache-type-k "$CACHE_TYPE_K" --cache-type-v "$CACHE_TYPE_V" \
    --flash-attn "$FLASH_ATTN" \
    "${SPEC_FLAGS[@]}" \
    "${TEMPLATE_FLAGS[@]}" \
    --metrics &
LLAMA_PID=$!
wait "$LLAMA_PID"
