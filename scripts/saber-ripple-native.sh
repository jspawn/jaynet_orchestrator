#!/usr/bin/env bash
# Serve Ripple-65B Preview TP=2 with its DFlash2 drafter on the host.
# JayNet's saber-serve.sh supplies the environment and handles graceful TP2 stop.
set -euo pipefail

REPO=${SABER_REPO:?set SABER_REPO to the Ripple-capable Saber checkout}
PACK=${RIPPLE_PACK:?set RIPPLE_PACK to the target TP2 pack}
DRAFT=${RIPPLE_DRAFT:?set RIPPLE_DRAFT to the DFlash2 TP2 pack}
CHECKPOINT=${RIPPLE_CHECKPOINT:?set RIPPLE_CHECKPOINT to the target checkpoint}
PREBUILT=${PREBUILT:-$REPO/prebuilt/gfx1201}
SABER=${RIPPLE_SERVE_BIN:-$REPO/target/release/ripple-serve}
HIP_DEVICES=${HIP_DEVICES:-0,1}
ROCM=${ROCM_PATH:-/opt/rocm}
MAX_CTX=${MAX_CTX:-131072}
MAX_ROWS=${MAX_ROWS:-64}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8080}
SERVED_NAME=${SERVED_NAME:-Ripple-65B-Preview}
BUILD=1
USE_DRAFT=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0; shift ;;
    --target-only) USE_DRAFT=0; shift ;;
    -h|--help)
      echo "Usage: scripts/saber-ripple-native.sh [--no-build] [--target-only] [ripple-serve options]"
      exit 0 ;;
    *) break ;;
  esac
done

[[ "$HIP_DEVICES" =~ ^[0-9]+,[0-9]+$ ]] \
  || { echo "Ripple TP=2 needs exactly two HIP device indices" >&2; exit 2; }
for path in "$PACK/manifest.json" "$PACK/rank0.pack" "$PACK/rank1.pack" \
    "$DRAFT/manifest.json" "$DRAFT/rank0.pack" "$DRAFT/rank1.pack" \
    "$CHECKPOINT/config.json" "$CHECKPOINT/tokenizer.json" "$CHECKPOINT/chat_template.jinja"; do
  [ -f "$path" ] || { echo "missing $path" >&2; exit 1; }
done
[ -d "$PREBUILT" ] || { echo "missing prebuilt kernel directory $PREBUILT" >&2; exit 1; }
if [ "$BUILD" = 1 ]; then
  (cd "$REPO" && cargo build --release -p saber-cli --bin ripple-serve)
fi
[ -x "$SABER" ] || { echo "no executable Ripple server at $SABER" >&2; exit 1; }
"$ROCM/bin/rocm-smi" --showpids

CMD=("$SABER" --pack "$PACK" --checkpoint "$CHECKPOINT" --prebuilt "$PREBUILT"
     --max-ctx "$MAX_CTX" --max-rows "$MAX_ROWS" --host "$HOST" --port "$PORT"
     --served-name "$SERVED_NAME")
if [ "$USE_DRAFT" = 1 ]; then CMD+=(--draft "$DRAFT"); fi
CMD+=("$@")

ulimit -c 0
echo "== starting Ripple on $HOST:$PORT (TP=2, HIP_VISIBLE_DEVICES=$HIP_DEVICES, max ctx $MAX_CTX)" >&2
exec env \
  HIP_VISIBLE_DEVICES="$HIP_DEVICES" \
  CUDA_VISIBLE_DEVICES= \
  TOKENIZERS_PARALLELISM=false \
  LD_LIBRARY_PATH="$ROCM/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "${CMD[@]}"
