#!/usr/bin/env bash
# Launch the dedicated CODER llama-server on GPU 1 (:8080) — the target for
# code.delegate. Mirrors scripts/start-llama.sh (the brain), but:
#   * pinned to GPU 1 (ORCH_CODER_GPU=1) so it never contends with the brain on GPU 0
#   * its OWN preset var (ORCH_CODER_PRESET) — it must NOT reuse the brain's ORCH_BRAIN_PRESET
#   * coding-tuned sampling defaults (low temp, no presence penalty)
#
# Env scheme (parallel with the brain): everything this service reads from the
# environment is namespaced ORCH_CODER_*, mirroring ORCH_BRAIN_* for start-llama.sh.
# Set ORCH_CODER_PRESET in ~/.config/orchestrator.env to choose the model; if it
# is unset the built-in defaults below load (now Ornith, not the old Qwopus coder).
#
# IMPORTANT: code.delegate runs a tool-using sub-agent ON this model, so the
# coder must emit OpenAI-style tool calls. By default we serve it with the same
# tool-calling chat template as the brain (ORCH_CODER_TOOLS_TEMPLATE); set that to
# "none" to use the model's OWN embedded --jinja template instead. Ornith ships a
# reasoning/tool template of its own — if delegations mis-parse <think> or tool
# calls don't fire, set ORCH_CODER_TOOLS_TEMPLATE=none. A coder that can't speak
# tool calls will fail every delegation.
#
# Runs in the FOREGROUND, clean SIGTERM on stop (no orphaned GPU process).
# Reuses the ROCm llama-server from build_tools.sh — compiles nothing.

set -euo pipefail

# -- Invariants (not overridable by the preset) -----------------------------
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
PORT="${ORCH_CODER_PORT:-8080}"               # clear of brain(8090)/litellm(4000)
HOST="${ORCH_CODER_HOST:-127.0.0.1}"
TOOLS_TEMPLATE="${ORCH_CODER_TOOLS_TEMPLATE:-/srv/orchestrator/config/qwen3-tools.jinja}"

# -- Preset + model (CODER-specific; defaults overridable by env) ------------
PRESET="${ORCH_CODER_PRESET:-/srv/llama/presets/ornith-1.0-35b-Q6_K.conf}"
MODEL_PATH="${ORCH_CODER_MODEL_PATH:-/srv/models/ornith-1.0-35b-Q6_K.gguf}"

# -- Coding-tuned defaults (used if the preset omits a key) -----------------
# Ornith-1.0-35B is a REASONING coder (Qwen3.5-MoE). It has NO MTP draft head,
# so MTP stays off and KV is q8_0 (NOT f16) to fit a usable ctx beside the
# ~28.5GB Q6_K weights on one 32GB card.
CTX_SIZE="32768"                              # start safe; raise as VRAM allows
GPU_LAYERS="99"
TEMP="0.3"; TOP_K="20"; TOP_P="0.95"; MIN_P="0"
PRESENCE_PENALTY="0"; REPEAT_PENALTY="1.0"    # code repeats tokens legitimately
BATCH_SIZE="2048"; UBATCH_SIZE="512"
FLASH_ATTN="on"
CACHE_TYPE_K="q8_0"; CACHE_TYPE_V="q8_0"      # symmetric -> HIP fused flash-attn
MTP="off"; SPEC_DRAFT_N_MAX="2"               # Ornith has no MTP head; off by default

# -- GPU pin (gfx1201/RDNA4). GPU 1 by default; keeps GPU 0 for the brain. ---
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"
ORCH_CODER_GPU="${ORCH_CODER_GPU:-1}"

# -- Load preset (KEY=value; same allow-listed parser as the brain) ---------
if [[ -f "$PRESET" ]]; then
    echo "[*] Loading coder preset: $PRESET"
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        case "$key" in
            CTX_SIZE|GPU_LAYERS|TEMP|TOP_K|TOP_P|MIN_P|PRESENCE_PENALTY|\
            REPEAT_PENALTY|BATCH_SIZE|UBATCH_SIZE|FLASH_ATTN|\
            CACHE_TYPE_K|CACHE_TYPE_V|MTP|SPEC_DRAFT_N_MAX)
                printf -v "$key" "%s" "$val" ;;
            MODEL_PATH)
                [[ -z "${ORCH_CODER_MODEL_PATH:-}" ]] && printf -v MODEL_PATH "%s" "$val" ;;
            # PORT/HOST/GPU/ALIAS in the preset are ignored: this service pins its
            # own port + GPU, and LiteLLM owns the alias.
            *) : ;;
        esac
    done < "$PRESET"
else
    echo "[!] Coder preset not found ($PRESET) — using built-in defaults." >&2
fi

# -- Validate binary + model ------------------------------------------------
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    echo "Build it with: /srv/llama/build_tools.sh llama rocm" >&2
    exit 1
fi
if [[ ! -f "$MODEL_PATH" ]]; then
    echo "Error: coder model not found at $MODEL_PATH" >&2
    echo "Set ORCH_CODER_MODEL_PATH=/srv/models/<file>.gguf (or fix the preset)." >&2
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
# index $ORCH_CODER_GPU directly. (The brain only survives the both-set pattern
# because it targets GPU 0, where 0/0 is a no-op.)
unset ROCR_VISIBLE_DEVICES GPU_DEVICE_ORDINAL
export HIP_VISIBLE_DEVICES="$ORCH_CODER_GPU"

# -- Tool-calling chat template ---------------------------------------------
TEMPLATE_FLAGS=(--jinja)
if [[ "$TOOLS_TEMPLATE" != "none" ]]; then
    if [[ ! -f "$TOOLS_TEMPLATE" ]]; then
        echo "Error: tool-calling template not found: $TOOLS_TEMPLATE" >&2
        echo "Fix ORCH_CODER_TOOLS_TEMPLATE, or set it to 'none' to use the model's own." >&2
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
echo "  port:    $HOST:$PORT     gpu: $ORCH_CODER_GPU"
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
