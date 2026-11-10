#!/usr/bin/env bash
# Serve Qwen3.8 Flash-Next TP=2 directly on the host. JayNet's saber-serve.sh
# supplies the environment and handles graceful process-group shutdown.
set -euo pipefail

REPO=${SABER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
NVME=${SABER_NVME:-/mnt/nvme}
SHARED=${SABER_SHARED:-$REPO/.cache}
PORT=${PORT:-8080}
BUILD=1
RECORD=${SABER_RECORD_ROUTES:-0}

usage() {
  cat <<'EOF'
Usage: scripts/saber-flashnext-native.sh [--no-build] [--record-routes] [engine options]

Common overrides: SABER_REPO, SABER_NVME, PACK, FN_TRUNK, FN_KV, FN_EXPERTS,
FN_AGENT_DEFAULTS, FN_OUT, HIP_DEVICES, ROCM_PATH, SABER_BIN, PREBUILT, PORT,
MAX_CTX, MODEL. Flash-Next requires exactly two GPUs.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    --record-routes) RECORD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done
ARGS=("$@")

PACK=${PACK:-$NVME/saber-packs/qwen38-flashnext-quark-trunk8}
FN_TRUNK=${FN_TRUNK:-w8-hc8-head}
FN_KV=${FN_KV:-fp8}
FN_ELASTIC_KV_MAX=${FN_ELASTIC_KV_MAX:-0.5}
FN_EXPERTS=${FN_EXPERTS:-tp2}
HIP_DEVICES=${HIP_DEVICES:-0,1}
ROCM=${ROCM_PATH:-/opt/rocm}
HIP_LIB=${HIP_LIB:-$ROCM/lib/libamdhip64.so.7}
HSA_LIB=${HSA_LIB:-$ROCM/lib/libhsa-runtime64.so.1}
PREBUILT=${PREBUILT:-$REPO/prebuilt/gfx1201}
SABER=${SABER_BIN:-$REPO/target/release/saber}
FN_OUT=${FN_OUT:-$SHARED/serve-flashnext/run}
KV_DIR=${FN_KV_DIR:-$NVME/saber-kv}
MAX_CTX=${MAX_CTX:-262144}
MODEL=${MODEL:-Qwen3.8-Flash-Next}

has_flag() {
  local wanted=$1 arg
  shift
  for arg in "$@"; do
    case "$arg" in
      "$wanted"|"$wanted="*) return 0 ;;
    esac
  done
  return 1
}

if [ "${FN_AGENT_DEFAULTS:-1}" = 1 ]; then
  DEFAULTS=(
    --fn-kv-lazy 1024
    --fn-prefix-tree-ram-gib 4.5
    --fn-prefill-yield 1
    --fn-ring-idle ''
    --fn-kv-admit-slab-tokens 338592
    --spec-couple on
  )
  for ((i = 0; i < ${#DEFAULTS[@]}; i += 2)); do
    flag=${DEFAULTS[i]}
    value=${DEFAULTS[i + 1]}
    has_flag "$flag" "${ARGS[@]}" && continue
    if { [ "$flag" = --fn-kv-lazy ] || [ "$flag" = --fn-kv-admit-slab-tokens ]; } \
        && has_flag --fn-kv-ram-gib "${ARGS[@]}"; then
      continue
    fi
    ARGS+=("$flag")
    [ -z "$value" ] || ARGS+=("$value")
  done
fi

REC_ARGS=()
if [ "$RECORD" = 1 ]; then
  ROUTES=$NVME/saber-kv/routes/$(date +%F)
  mkdir -p "$ROUTES"
  REC_ARGS=(--fn-route-record "$ROUTES")
  echo "== recording the served routes to $ROUTES" >&2
fi

PROMPT_ARGS=()
PROMPTS=$SHARED/ws-fn3/ref/run1
[ ! -d "$PROMPTS" ] || PROMPT_ARGS=(--prompt-ids "$PROMPTS")

[[ "$HIP_DEVICES" =~ ^[0-9]+,[0-9]+$ ]] \
  || { echo "HIP_DEVICES=$HIP_DEVICES: Flash-Next TP=2 needs exactly two HIP device indices" >&2; exit 2; }
for f in "$HIP_LIB" "$HSA_LIB" "$PACK/manifest.json" \
    "$PACK/rank0/trunk8.pack" "$PACK/rank1/trunk8.pack"; do
  [ -f "$f" ] || { echo "missing $f (run tools/fetch_packs.sh flashnext)" >&2; exit 1; }
done
[ -d "$PREBUILT" ] || { echo "missing prebuilt kernel directory $PREBUILT" >&2; exit 1; }
if [ "$BUILD" = 1 ]; then
  (cd "$REPO" && cargo build --release -p saber-cli)
fi
[ -x "$SABER" ] || { echo "no executable release binary at $SABER" >&2; exit 1; }
mkdir -p "$FN_OUT" "$KV_DIR"

CMD=(
  "$SABER" engine
  --pack "$PACK"
  --fn-trunk "$FN_TRUNK"
  --fn-kv-dtype "$FN_KV"
  --fn-kv-dir "$KV_DIR"
  --fn-vram-elastic-kv-max-gib "$FN_ELASTIC_KV_MAX"
  --fn-experts "$FN_EXPERTS"
  "${PROMPT_ARGS[@]}"
  --tp 2
  --hip-lib "$HIP_LIB"
  --prebuilt "$PREBUILT"
  --fn-out "$FN_OUT"
  --fn-serve "$PORT"
  --fn-max-ctx "$MAX_CTX"
  --fn-served-name "$MODEL"
  --fn-thinking on
  "${REC_ARGS[@]}"
  "${ARGS[@]}"
)

ulimit -c 0
echo "== starting native Flash-Next on :$PORT (TP=2, HIP_VISIBLE_DEVICES=$HIP_DEVICES)" >&2
exec env \
  HIP_VISIBLE_DEVICES="$HIP_DEVICES" \
  GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-3}" \
  CUDA_VISIBLE_DEVICES= \
  TOKENIZERS_PARALLELISM=false \
  PYTHONDONTWRITEBYTECODE=1 \
  LD_LIBRARY_PATH="$ROCM/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "${CMD[@]}"
