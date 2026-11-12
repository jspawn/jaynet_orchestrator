#!/usr/bin/env bash
# Serve Occamy FP8 experts + BF16 dense/shared/KV on two consecutive GPUs.
# JayNet's saber-serve.sh supplies the environment and handles graceful TP2 stop.
set -euo pipefail

REPO=${SABER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    -h|--help)
      echo "Usage: scripts/saber-occamy-native.sh [--no-build] [occamy-exec options]"
      exit 0 ;;
    *) break ;;
  esac
done

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8090}
FP8=${OCCAMY_FP8:?set OCCAMY_FP8 to the FP8 routed-expert pack}
LAYERS=${OCCAMY_LAYERS:?set OCCAMY_LAYERS to the BF16 all-layer pack}
GLOBALS=${OCCAMY_GLOBALS:?set OCCAMY_GLOBALS to the BF16 globals pack}
CHECKPOINT=${OCCAMY_CHECKPOINT:?set OCCAMY_CHECKPOINT to the BF16 checkpoint}
MTP=${OCCAMY_MTP:-}
HIP_DEVICES=${HIP_DEVICES:-0,1}
DEVICE=${DEVICE:-0}
MAX_CTX=${MAX_CTX:-262144}
MAX_REQUESTS=${MAX_REQUESTS:-1}
SPEC_DEPTH=${SPEC_DEPTH:-1}
MODEL=${MODEL:-occamy-1.0}
ROCM=${ROCM_PATH:-/opt/rocm}
HIP_LIB=${HIP_LIB:-$ROCM/lib/libamdhip64.so.7}
PREBUILT=${PREBUILT:-$REPO/prebuilt/gfx1201}
SABER=${SABER_BIN:-$REPO/target/release/saber}

[[ "$HIP_DEVICES" =~ ^[0-9]+,[0-9]+$ ]] \
  || { echo "Occamy TP=2 needs exactly two consecutive HIP device indices" >&2; exit 2; }
[[ "$DEVICE" =~ ^[0-9]+$ ]] || { echo "Occamy rank-0 DEVICE must be numeric" >&2; exit 2; }
[ "$DEVICE" = "${HIP_DEVICES%,*}" ] \
  || { echo "DEVICE must be the first HIP_DEVICES card" >&2; exit 2; }
for d in "$FP8" "$LAYERS" "$GLOBALS" "$CHECKPOINT" "$PREBUILT"; do
  [ -d "$d" ] || { echo "missing directory $d" >&2; exit 1; }
done
for f in "$CHECKPOINT/config.json" "$CHECKPOINT/tokenizer.json" \
    "$CHECKPOINT/chat_template.jinja" "$HIP_LIB"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done
if [ -n "$MTP" ]; then
  [ -f "$MTP" ] || { echo "missing MTP head $MTP" >&2; exit 1; }
  [ "$MAX_REQUESTS" = 1 ] || { echo "Occamy MTP needs MAX_REQUESTS=1" >&2; exit 1; }
fi
if [ "$BUILD" = 1 ]; then
  (cd "$REPO" && cargo build --release -p saber-cli)
fi
[ -x "$SABER" ] || { echo "no executable Occamy-capable binary at $SABER" >&2; exit 1; }

CMD=(
  "$SABER" occamy-exec
  --layers "$LAYERS"
  --globals "$GLOBALS"
  --fp8-experts "$FP8"
  --checkpoint "$CHECKPOINT"
  --prebuilt "$PREBUILT"
  --device "$DEVICE"
  --max-ctx "$MAX_CTX"
  --max-rows 64
  --max-requests "$MAX_REQUESTS"
  --spec-depth "$SPEC_DEPTH"
  --host "$HOST"
  --serve "$PORT"
  --served-name "$MODEL"
  --hip-lib "$HIP_LIB"
)
if [ -n "$MTP" ]; then
  CMD+=(--mtp "$MTP")
fi
CMD+=("$@")

ulimit -c 0
echo "== starting Occamy FP8 on $HOST:$PORT (TP=2, HIP_VISIBLE_DEVICES=$HIP_DEVICES, max ctx $MAX_CTX)" >&2
exec env \
  HIP_VISIBLE_DEVICES="$HIP_DEVICES" \
  GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-3}" \
  CUDA_VISIBLE_DEVICES= \
  LD_LIBRARY_PATH="$ROCM/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "${CMD[@]}"
