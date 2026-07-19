#!/usr/bin/env bash
# Universal llama-server launcher — one script for any preset in runtime.yaml.
#
# Usage:
#   start-model.sh <preset_name>       # e.g. brain, coder, ornith, dolphin, agents1
#   start-model.sh brain               # reads models.presets.brain from runtime.yaml
#
# The preset name maps to a config block in runtime.yaml → models.presets.<name>,
# which gives us the .conf file path, port, GPU, and alias. The .conf file
# provides the model path, sampling, quantization, and other llama-server params.
#
# This replaces start-brain1.sh, start-brain2.sh, start-coder.sh, and start-llama.sh.

set -euo pipefail

# -- Which preset to launch -------------------------------------------------
PRESET_NAME="${1:-brain}"
_ORCH_HOME="${ORCH_HOME:-/srv/orchestrator}"
_RUNTIME_YAML="${ORCH_CONFIG:-${_ORCH_HOME}/config/runtime.yaml}"

if [[ ! -f "$_RUNTIME_YAML" ]]; then
    echo "Error: runtime.yaml not found at $_RUNTIME_YAML" >&2
    exit 1
fi

# Extract fields from runtime.yaml → models.presets.<name>
# Uses a small Python snippet for reliable YAML parsing.
eval "$(python3 - "$_RUNTIME_YAML" "$PRESET_NAME" << 'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
name = sys.argv[2]
p = (cfg.get("models") or {}).get("presets", {}).get(name)
if not p:
    print(f'echo "Error: preset \"{name}\" not found in runtime.yaml" >&2; exit 1')
    sys.exit(0)
print(f'_PRESET_FILE="{p.get("preset", "")}"')
print(f'_PORT="{p.get("port", "8080")}"')
print(f'_GPU="{p.get("gpu", "0")}"')
print(f'_ALIAS="{p.get("served_id", name)}"')
print(f'_VRAM="{p.get("vram_gib", "")}"')
PYEOF
)"

echo "[*] Preset: $PRESET_NAME"
echo "[*] Config: $_PRESET_FILE"
echo "[*] Port: $_PORT  GPU: $_GPU  Alias: $_ALIAS"

# -- Locate the llama-server binary -----------------------------------------
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    exit 1
fi

# -- Built-in defaults (overridden by preset, then env) ----------------------
HOST="127.0.0.1"
PORT="$_PORT"
CTX_SIZE="131072"
GPU_LAYERS="99"
TEMP="0.6"
TOP_K="20"
TOP_P="0.95"
MIN_P="0"
PRESENCE_PENALTY="0"
REPEAT_PENALTY="1.0"
BATCH_SIZE="2048"
UBATCH_SIZE="512"
CACHE_TYPE_K="q8_0"
CACHE_TYPE_V="q8_0"
FLASH_ATTN="on"
SPLIT_MODE="none"
TENSOR_SPLIT=""
MMPROJ=""
MMPROJ_OFFLOAD="off"
MTP="off"
SPEC_DRAFT_N_MAX="2"
TOOLS_TEMPLATE=""
MODEL_PATH=""
THREADS=""
JINJA="yes"
EMBEDDINGS=""
RERANKING=""
POOLING=""
EXTRA_ARGS=""

# -- Load preset (.conf KEY=value lines) ------------------------------------
if [[ -f "$_PRESET_FILE" ]]; then
    echo "[*] Loading preset: $_PRESET_FILE"
    while IFS='=' read -r key val; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        val="${val%%#*}"     # strip inline comments
        val="$(echo "$val" | xargs)"  # trim whitespace
        case "$key" in
            MODEL_PATH|CTX_SIZE|GPU_LAYERS|TEMP|TOP_K|TOP_P|MIN_P|PRESENCE_PENALTY|\
            REPEAT_PENALTY|BATCH_SIZE|UBATCH_SIZE|FLASH_ATTN|SPLIT_MODE|TENSOR_SPLIT|\
            CACHE_TYPE_K|CACHE_TYPE_V|MMPROJ|MMPROJ_OFFLOAD|MTP|SPEC_DRAFT_N_MAX|\
            TOOLS_TEMPLATE|THREADS|JINJA|EMBEDDINGS|RERANKING|POOLING|EXTRA_ARGS|\
            REASONING_FORMAT|SYSTEM_PROMPT)
                printf -v "$key" "%s" "$val" ;;
            # PORT/HOST/ALIAS/BACKEND/VISIBLE_DEVICES from the preset are
            # IGNORED — runtime.yaml owns port and GPU pinning.
            *) : ;;
        esac
    done < "$_PRESET_FILE"
else
    echo "[!] Preset file not found: $_PRESET_FILE — using defaults." >&2
fi

# -- Validate model ----------------------------------------------------------
if [[ -z "$MODEL_PATH" || ! -f "$MODEL_PATH" ]]; then
    echo "Error: model not found: ${MODEL_PATH:-<empty>}" >&2
    echo "Check MODEL_PATH in the preset file: $_PRESET_FILE" >&2
    exit 1
fi

# -- Chat/tool template -----------------------------------------------------
TEMPLATE_FLAGS=()
if [[ "$JINJA" == "yes" ]]; then
    TEMPLATE_FLAGS=(--jinja)
fi
if [[ -n "$TOOLS_TEMPLATE" && "$TOOLS_TEMPLATE" != "none" ]]; then
    # Resolve relative paths against ORCH_HOME
    [[ "$TOOLS_TEMPLATE" != /* ]] && TOOLS_TEMPLATE="${_ORCH_HOME}/${TOOLS_TEMPLATE}"
    if [[ -f "$TOOLS_TEMPLATE" ]]; then
        TEMPLATE_FLAGS+=(--chat-template-file "$TOOLS_TEMPLATE")
    else
        echo "Warning: template not found: $TOOLS_TEMPLATE — using model's own." >&2
    fi
fi

# -- Vision projector (optional) --------------------------------------------
VISION_FLAGS=()
if [[ -n "$MMPROJ" && "$MMPROJ" != "none" ]]; then
    if [[ -f "$MMPROJ" ]]; then
        VISION_FLAGS=(--mmproj "$MMPROJ")
        [[ "$MMPROJ_OFFLOAD" == "off" ]] && VISION_FLAGS+=(--no-mmproj-offload)
        [[ "${UBATCH_SIZE:-0}" -lt 512 ]] 2>/dev/null && UBATCH_SIZE="512"
    else
        echo "Warning: MMPROJ not found: $MMPROJ — running text-only." >&2
    fi
fi

# -- MTP self-speculative decoding (optional) --------------------------------
SPEC_FLAGS=()
if [[ "$MTP" == "on" ]]; then
    #CACHE_TYPE_K="f16"; CACHE_TYPE_V="f16"
    SPEC_FLAGS=(--spec-type draft-mtp --spec-draft-n-max "$SPEC_DRAFT_N_MAX")
fi

# -- Embedding/reranking flags (for RAG servers) -----------------------------
EMBED_FLAGS=()
[[ "$EMBEDDINGS" == "on" || "$EMBEDDINGS" == "yes" ]] && EMBED_FLAGS+=(--embeddings)
[[ "$RERANKING" == "on" || "$RERANKING" == "yes" ]] && EMBED_FLAGS+=(--reranking --embeddings)
[[ -n "$POOLING" ]] && EMBED_FLAGS+=(--pooling "$POOLING")

# -- GPU visibility ----------------------------------------------------------
if [[ -n "$_GPU" ]]; then
    export HIP_VISIBLE_DEVICES="$_GPU"
    #export ROCR_VISIBLE_DEVICES="$_GPU"
fi
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"

MULTI_GPU_FLAGS=()
if [[ -z "$_GPU" ]]; then
    MULTI_GPU_FLAGS=(--split-mode "${SPLIT_MODE:-layer}")
    [[ -n "$TENSOR_SPLIT" ]] && MULTI_GPU_FLAGS+=(--tensor-split "$TENSOR_SPLIT")
fi

# -- Thread count ------------------------------------------------------------
THREAD_FLAGS=()
[[ -n "$THREADS" && "$THREADS" != "-1" ]] && THREAD_FLAGS=(--threads "$THREADS")

# -- Alias -------------------------------------------------------------------
ALIAS_FLAGS=(--alias "$_ALIAS")

# -- Graceful shutdown -------------------------------------------------------
LLAMA_PID=""
cleanup() {
    echo -e "\n[!] Shutting down llama-server ($PRESET_NAME)..."
    if [[ -n "$LLAMA_PID" ]]; then
        # Grace first: SIGTERM lets llama.cpp release VRAM cleanly; SIGKILL only
        # via a 5s background watchdog, cancelled if the server exits in time.
        kill -TERM "$LLAMA_PID" 2>/dev/null
        ( sleep 5; kill -KILL "$LLAMA_PID" 2>/dev/null ) &
        local _wd=$!
        wait "$LLAMA_PID" 2>/dev/null
        kill "$_wd" 2>/dev/null; wait "$_wd" 2>/dev/null
    fi
    echo "[*] Stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM

# -- Launch ------------------------------------------------------------------
echo "-------------------------------------------------------"
echo "  MODEL: $(basename "$MODEL_PATH")"
echo "  preset: $PRESET_NAME  port: $HOST:$PORT  gpu: ${_GPU:-all}"
echo "  ctx: $CTX_SIZE  layers: $GPU_LAYERS  kv: $CACHE_TYPE_K/$CACHE_TYPE_V"
echo "  alias: $_ALIAS"
[[ ${#VISION_FLAGS[@]} -gt 0 ]] && echo "  vision: $(basename "$MMPROJ")"
[[ "$MTP" == "on" ]] && echo "  mtp: draft-mtp, n-max=$SPEC_DRAFT_N_MAX"
[[ ${#EMBED_FLAGS[@]} -gt 0 ]] && echo "  mode: ${EMBED_FLAGS[*]}"
echo "-------------------------------------------------------"

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
    --cache-type-k "$CACHE_TYPE_K" --cache-type-v "$CACHE_TYPE_V" \
    --flash-attn "$FLASH_ATTN" \
    "${VISION_FLAGS[@]}" \
    "${SPEC_FLAGS[@]}" \
    "${TEMPLATE_FLAGS[@]}" \
    "${EMBED_FLAGS[@]}" \
    "${THREAD_FLAGS[@]}" \
    "${ALIAS_FLAGS[@]}" \
    --metrics \
    $EXTRA_ARGS &
LLAMA_PID=$!
wait "$LLAMA_PID"
