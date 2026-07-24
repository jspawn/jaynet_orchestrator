#!/usr/bin/env bash
# Universal llama-server launcher — one script for every serving path.
#
# Two modes:
#   start-model.sh <name>        Catalog preset, resolved via the preset DB
#                                (runtime/preset_store.py; seeded from
#                                runtime.yaml models.presets). The catalog owns
#                                port/GPU/alias; the .conf supplies model +
#                                sampling. (process_manager, systemd units)
#   start-model.sh --preset FILE Headless .conf mode (serve.* tools; replaces
#                                llama-serve.sh --preset): the .conf owns
#                                port/GPU/alias via its PORT, VISIBLE_DEVICES,
#                                ALIAS, HOST keys.
#   ... --dry-run                Print the resolved command + env, don't launch.
#
# This replaces start-brain1.sh, start-brain2.sh, start-coder.sh, start-llama.sh
# and the headless role of /srv/llama/llama-serve.sh (which remains as the
# interactive TUI only).

set -euo pipefail

# -- Arg parsing ---------------------------------------------------------------
_ORCH_HOME="${ORCH_HOME:-/srv/orchestrator}"
_RUNTIME_YAML="${ORCH_CONFIG:-${_ORCH_HOME}/config/runtime.yaml}"
MODE="name"
PRESET_NAME=""
_PRESET_FILE=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --preset)    MODE="file"; _PRESET_FILE="${2:?--preset needs a FILE}"; shift 2 ;;
        --preset=*)  MODE="file"; _PRESET_FILE="${1#*=}"; shift ;;
        --dry-run|-d) DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
        *)           PRESET_NAME="$1"; shift ;;
    esac
done
[[ "$MODE" == "name" && -z "$PRESET_NAME" ]] && PRESET_NAME="brain"

# -- Slot ownership: name mode resolves the preset catalog (DB; seeded from --
# -- runtime.yaml on first use) ----------------------------------------------
_PORT=""; _GPU=""; _ALIAS=""; _VRAM=""
if [[ "$MODE" == "name" ]]; then
    if [[ ! -f "$_RUNTIME_YAML" ]]; then
        echo "Error: runtime.yaml not found at $_RUNTIME_YAML" >&2
        exit 1
    fi
    export ORCH_CONFIG="$_RUNTIME_YAML"
    eval "$(python3 "$_ORCH_HOME/runtime/preset_store.py" resolve "$PRESET_NAME")"
fi

# -- Locate the llama-server binary --------------------------------------------
LLAMA_BIN="${LLAMA_BIN:-/srv/llama/llama.cpp-rocm/build/bin/llama-server}"
if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "Error: llama-server not found at $LLAMA_BIN" >&2
    exit 1
fi

# -- Built-in defaults (overridden by preset, then env) ------------------------
HOST="127.0.0.1"                 # loopback only — the proxy reaches it here
PORT="${_PORT:-8080}"
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

# -- Load preset (.conf KEY=value lines) ---------------------------------------
_F_PORT=""; _F_HOST=""; _F_ALIAS=""; _F_VISIBLE_DEVICES=""
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
            # Slot keys are captured, not applied — name mode ignores them
            # (runtime.yaml owns the slot); --preset mode applies them below.
            PORT|HOST|ALIAS|VISIBLE_DEVICES)
                printf -v "_F_$key" "%s" "$val" ;;
            *) : ;;
        esac
    done < "$_PRESET_FILE"
else
    echo "Error: preset file not found: $_PRESET_FILE" >&2
    exit 1
fi

# -- File mode: the .conf owns the slot -----------------------------------------
if [[ "$MODE" == "file" ]]; then
    PORT="${_F_PORT:-$PORT}"
    HOST="${_F_HOST:-$HOST}"
    _GPU="$_F_VISIBLE_DEVICES"
    _ALIAS="${_F_ALIAS:-$(basename "${MODEL_PATH:-model}" .gguf)}"
    PRESET_NAME="$(basename "$_PRESET_FILE" .conf)"
fi

# -- Validate model --------------------------------------------------------------
if [[ -z "$MODEL_PATH" || ! -f "$MODEL_PATH" ]]; then
    echo "Error: model not found: ${MODEL_PATH:-<empty>}" >&2
    echo "Check MODEL_PATH in the preset file: $_PRESET_FILE" >&2
    exit 1
fi

# -- Chat/tool template ----------------------------------------------------------
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

# -- Vision projector (optional) -------------------------------------------------
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

# -- MTP self-speculative decoding (optional) -------------------------------------
# KV type stays as the preset says — q8_0 KV + MTP measures fine on this box
# (acceptance 0.4–0.7), unlike llama-serve.sh's blanket f16 forcing.
SPEC_FLAGS=()
if [[ "$MTP" == "on" ]]; then
    SPEC_FLAGS=(--spec-type draft-mtp --spec-draft-n-max "$SPEC_DRAFT_N_MAX")
fi

# -- Embedding/reranking flags (for RAG servers) ----------------------------------
EMBED_FLAGS=()
[[ "$EMBEDDINGS" == "on" || "$EMBEDDINGS" == "yes" ]] && EMBED_FLAGS+=(--embeddings)
[[ "$RERANKING" == "on" || "$RERANKING" == "yes" ]] && EMBED_FLAGS+=(--reranking --embeddings)
[[ -n "$POOLING" ]] && EMBED_FLAGS+=(--pooling "$POOLING")

# -- GPU visibility ----------------------------------------------------------------
if [[ -n "$_GPU" ]]; then
    export HIP_VISIBLE_DEVICES="$_GPU"
fi
export GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-1}"

MULTI_GPU_FLAGS=()
if [[ -z "$_GPU" ]]; then
    MULTI_GPU_FLAGS=(--split-mode "${SPLIT_MODE:-layer}")
    [[ -n "$TENSOR_SPLIT" ]] && MULTI_GPU_FLAGS+=(--tensor-split "$TENSOR_SPLIT")
fi

# -- Thread count --------------------------------------------------------------------
THREAD_FLAGS=()
[[ -n "$THREADS" && "$THREADS" != "-1" ]] && THREAD_FLAGS=(--threads "$THREADS")

# -- Alias ---------------------------------------------------------------------------
ALIAS_FLAGS=(--alias "$_ALIAS")

# -- Assemble the command -------------------------------------------------------------
CMD=("$LLAMA_BIN"
    --model "$MODEL_PATH"
    --host "$HOST"
    --port "$PORT"
    --ctx-size "$CTX_SIZE"
    --n-gpu-layers "$GPU_LAYERS"
    "${MULTI_GPU_FLAGS[@]}"
    --temp "$TEMP" --top-k "$TOP_K" --top-p "$TOP_P" --min-p "$MIN_P"
    --presence-penalty "$PRESENCE_PENALTY" --repeat-penalty "$REPEAT_PENALTY"
    --batch-size "$BATCH_SIZE" --ubatch-size "$UBATCH_SIZE"
    --cache-type-k "$CACHE_TYPE_K" --cache-type-v "$CACHE_TYPE_V"
    --flash-attn "$FLASH_ATTN"
    "${VISION_FLAGS[@]}"
    "${SPEC_FLAGS[@]}"
    "${TEMPLATE_FLAGS[@]}"
    "${EMBED_FLAGS[@]}"
    "${THREAD_FLAGS[@]}"
    "${ALIAS_FLAGS[@]}"
    --metrics
    $EXTRA_ARGS)

echo "-------------------------------------------------------"
echo "  MODEL: $(basename "$MODEL_PATH")"
echo "  preset: $PRESET_NAME ($MODE mode)  port: $HOST:$PORT  gpu: ${_GPU:-all}"
echo "  ctx: $CTX_SIZE  layers: $GPU_LAYERS  kv: $CACHE_TYPE_K/$CACHE_TYPE_V"
echo "  alias: $_ALIAS"
[[ ${#VISION_FLAGS[@]} -gt 0 ]] && echo "  vision: $(basename "$MMPROJ")"
[[ "$MTP" == "on" ]] && echo "  mtp: draft-mtp, n-max=$SPEC_DRAFT_N_MAX"
[[ ${#EMBED_FLAGS[@]} -gt 0 ]] && echo "  mode: ${EMBED_FLAGS[*]}"
echo "-------------------------------------------------------"

if [[ "$DRY_RUN" == "1" ]]; then
    [[ -n "${HIP_VISIBLE_DEVICES:-}" ]] && echo "HIP_VISIBLE_DEVICES=$HIP_VISIBLE_DEVICES"
    printf '%q ' "${CMD[@]}"; echo
    exit 0
fi

# -- Graceful shutdown ------------------------------------------------------------------
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

"${CMD[@]}" &
LLAMA_PID=$!
wait "$LLAMA_PID"
