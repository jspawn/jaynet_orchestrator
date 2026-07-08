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
PORT=8090                                    # brain1; brain1=8091, litellm=4000, rag=8095/8096
HOST="${HOST:-127.0.0.1}"

# -- Preset (llama-serve.sh format) -----------------------------------------
# Preset selection. Honor ORCH_BRAIN_PRESET — the same var the orchestrator app
# and ~/.config/orchestrator.env use — so the launcher and app never disagree on
# which model is served. PRESET stays as an explicit override; the built-in
# default is the last resort.
PRESET="${PRESET:-${ORCH_BRAIN1_PRESET:-/srv/orchestrator/presets/Qwen3.6-35B-A3B-Uncensored-Genesis-APEX_b1.conf}}"
MODEL_PATH="${MODEL_PATH:-/srv/models/LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-V2-APEX-MTP-GGUF/Qwen3.6-35B-A3B-Uncensored-Genesis-MTP-APEX.gguf}"

# -- Defaults (used if the preset omits a key) ------------------------------
CTX_SIZE="32768"
GPU_LAYERS="99"
TEMP="0.6"; TOP_K="20"; TOP_P="0.95"; MIN_P="0"
PRESENCE_PENALTY="0"; REPEAT_PENALTY="1.0"
BATCH_SIZE="2048"; UBATCH_SIZE="512"
FLASH_ATTN="on"
SPLIT_MODE=""; TENSOR_SPLIT=""
CACHE_TYPE_K="f16"; CACHE_TYPE_V="f16"        # symmetric -> HIP fused flash-attn
MMPROJ=""; MMPROJ_OFFLOAD="on"                # vision projector (optional)
MTP="off"; SPEC_DRAFT_N_MAX="2"               # self-speculative decoding
# Chat/tool template — per model, so each preset can carry its own. The preset
# may set TOOLS_TEMPLATE=/path/to.jinja; env ORCH_BRAIN_TOOLS_TEMPLATE overrides
# the preset; "none" means use the model's own embedded template (--jinja only).
TOOLS_TEMPLATE="/srv/orchestrator/config/qwen3-tools.jinja"

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
            REPEAT_PENALTY|BATCH_SIZE|UBATCH_SIZE|FLASH_ATTN|SPLIT_MODE|TENSOR_SPLIT|\
            CACHE_TYPE_K|CACHE_TYPE_V|MMPROJ|MMPROJ_OFFLOAD|MTP|SPEC_DRAFT_N_MAX|\
            TOOLS_TEMPLATE)
                printf -v "$key" "%s" "$val" ;;
            # MODEL_PATH from the preset IS honored (the brain follows the preset's
            # model) unless overridden by the environment.
            MODEL_PATH)
                [[ -z "${MODEL_PATH_ENV:-}" ]] && printf -v MODEL_PATH "%s" "$val" ;;
            # PORT/HOST/BACKEND/VISIBLE_DEVICES/ALIAS from the preset are
            # deliberately IGNORED — the orchestrator pins its own port and GPU,
            # and LiteLLM owns the alias.
            *) : ;;
        esac
    done < "$PRESET"
else
    echo "[!] Preset not found ($PRESET) — using built-in defaults." >&2
fi

# Precedence: env override > preset > built-in default. An empty value — e.g. a
# preset saved by llama-serve.sh for a model that uses its own template — falls
# back to the orchestrator default rather than erroring. Set "none" explicitly
# to serve the model's embedded template.
TOOLS_TEMPLATE="${ORCH_BRAIN_TOOLS_TEMPLATE:-$TOOLS_TEMPLATE}"
[[ -z "$TOOLS_TEMPLATE" ]] && TOOLS_TEMPLATE="/srv/orchestrator/config/qwen3-tools.jinja"

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

# -- Vision projector (optional) --------------------------------------------
# If the preset names an MMPROJ, the brain must serve it or the orchestrator
# (which reads the same preset and concludes vision=on) will forward images to
# a server that rejects them. Fail loudly on a bad path rather than coming up
# silently text-only.
VISION_FLAGS=()
if [[ -n "$MMPROJ" && "$MMPROJ" != "none" ]]; then
    if [[ ! -f "$MMPROJ" ]]; then
        echo "Error: MMPROJ set in preset but file not found: $MMPROJ" >&2
        echo "Fix the preset's MMPROJ path, or clear it for a text-only brain." >&2
        exit 1
    fi
    VISION_FLAGS=(--mmproj "$MMPROJ")
    # MMPROJ_OFFLOAD=off keeps the CLIP encoder on CPU. On gfx1201 this both
    # dodges the "GPU never idles after mmproj" bug AND avoids the immature HIP
    # vision kernels — slower image encode, but the safer correctness path.
    [[ "$MMPROJ_OFFLOAD" == "off" ]] && VISION_FLAGS+=(--no-mmproj-offload)
    # Image tokens need a micro-batch >= 512 or llama.cpp asserts mid-inference.
    [[ "${UBATCH_SIZE:-0}" -lt 512 ]] 2>/dev/null && UBATCH_SIZE="512"
fi

# -- MTP self-speculative decoding (optional) -------------------------------
SPEC_FLAGS=()
if [[ "$MTP" == "on" ]]; then
    # Quantized KV collapses draft acceptance -> force full precision.
    CACHE_TYPE_K="f16"; CACHE_TYPE_V="f16"
    SPEC_FLAGS=(--spec-type draft-mtp --spec-draft-n-max "$SPEC_DRAFT_N_MAX")
fi

# -- Chat/tool template (per-model; from preset, env, or default) -----------
# code.delegate runs a tool-using sub-agent on the brain, so the template must
# render tool calls. "none" => the model's own embedded template (--jinja only).
TEMPLATE_FLAGS=(--jinja)
if [[ "$TOOLS_TEMPLATE" != "none" ]]; then
    if [[ ! -f "$TOOLS_TEMPLATE" ]]; then
        echo "Error: chat template not found: $TOOLS_TEMPLATE" >&2
        echo "Set TOOLS_TEMPLATE in the preset (or ORCH_BRAIN_TOOLS_TEMPLATE), or 'none' for the model's own." >&2
        exit 1
    fi
    TEMPLATE_FLAGS+=(--chat-template-file "$TOOLS_TEMPLATE")
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
echo "  ctx:     $CTX_SIZE   layers: $GPU_LAYERS   kv: $CACHE_TYPE_K/$CACHE_TYPE_V"
echo "  gpu:     ${ORCH_GPU:-both}"
echo "  sampling: temp=$TEMP top_k=$TOP_K top_p=$TOP_P"
echo "  tmpl:    $([[ "$TOOLS_TEMPLATE" == none ]] && echo 'model embedded (--jinja)' || basename "$TOOLS_TEMPLATE")"
if [[ ${#VISION_FLAGS[@]} -gt 0 ]]; then
    echo "  vision:  $(basename "$MMPROJ")  (encoder on $([[ "$MMPROJ_OFFLOAD" == off ]] && echo CPU || echo GPU))"
else
    echo "  vision:  off (text only)"
fi
[[ "$MTP" == "on" ]] && echo "  mtp:     draft-mtp, n-max=$SPEC_DRAFT_N_MAX"
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
    --cache-type-k "$CACHE_TYPE_K" --cache-type-v "$CACHE_TYPE_V" \
    --flash-attn "$FLASH_ATTN" \
    "${VISION_FLAGS[@]}" \
    "${SPEC_FLAGS[@]}" \
    "${TEMPLATE_FLAGS[@]}" \
    --metrics &
LLAMA_PID=$!
wait "$LLAMA_PID"
