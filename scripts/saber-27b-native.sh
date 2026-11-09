#!/usr/bin/env bash
# Serve Qwen3.8-27B directly on the host with shipped gfx1201 code objects.
# Installed as tools/serve_27b_native.sh by build_tools.sh. Extra arguments
# go to `saber serve`; stop through saber-serve.sh for graceful SIGINT handling.
set -euo pipefail

REPO=${SABER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
TP=${TP:-2}
BUILD=1

usage() {
  cat <<'EOF'
Usage: tools/serve_27b_native.sh [--tp 1|2] [--no-build] [saber serve options]

  --tp 1       One GPU; uses the 27b-tp1 packs and DEVICE (default 0).
  --tp 2       Two GPUs; uses the 27b packs and HIP_DEVICES (default 0,1).
  --no-build   Reuse target/release/saber instead of building it first.

Common overrides: ROCM_PATH, SABER_PACKS, SABER_BIN, HOST, PORT, MAX_CTX,
MAX_SEQS, BATCHES, WIDTHS, DRAFT_BLOCK, BUDGET_MIB, MODEL, PREBUILT, DEVICE,
HIP_DEVICES. HOST defaults to loopback.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tp)
      [ $# -ge 2 ] || { echo "--tp needs 1 or 2" >&2; exit 2; }
      TP=$2
      shift 2
      ;;
    --tp=*) TP=${1#*=}; shift ;;
    --no-build) BUILD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

case "$TP" in
  1)
    [ -z "${HIP_DEVICES:-}" ] || { echo "TP=1 takes DEVICE=<one HIP device>, not HIP_DEVICES" >&2; exit 2; }
    DEVICE=${DEVICE:-0}
    [[ "$DEVICE" =~ ^[0-9]+$ ]] || { echo "DEVICE=$DEVICE: TP=1 serves on ONE HIP device index" >&2; exit 2; }
    MAX_CTX=${MAX_CTX:-65536}
    TIER=${TIER:-qwen38-27b-nvfp4-devfinal-tp1-fp8head}
    DRAFT=${DRAFT:-qwen38-dflash2-drafter-tp1-fp8head}
    HIP_DEVICES=$DEVICE
    BATCHES=${BATCHES:-1,2,4}
    MAX_SEQS=${MAX_SEQS:-4}
    FETCH=27b-tp1
    RANKS=(0)
    TP_ARGS=(--engine-tp 1 --engine-device 0)
    ;;
  2)
    HIP_DEVICES=${HIP_DEVICES:-0,1}
    [[ "$HIP_DEVICES" =~ ^[0-9]+,[0-9]+$ ]] || { echo "HIP_DEVICES=$HIP_DEVICES: TP=2 needs exactly two HIP device indices" >&2; exit 2; }
    MAX_CTX=${MAX_CTX:-262144}
    TIER=${TIER:-qwen38-27b-nvfp4-devfinal-fp8head}
    DRAFT=${DRAFT:-qwen38-dflash2-drafter-fp8head}
    BATCHES=${BATCHES:-1,2,3,4,8}
    MAX_SEQS=${MAX_SEQS:-8}
    FETCH=27b
    RANKS=(0 1)
    TP_ARGS=(--engine-tp 2 --engine-device 0 --engine-peer-device 1)
    ;;
  *) echo "--tp $TP: the 27B launcher serves TP=1 or TP=2" >&2; exit 2 ;;
esac

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8080}
PACKS=${SABER_PACKS:-/mnt/nvme/saber-packs}
MODEL=${MODEL:-Qwen3.8}
ROCM=${ROCM_PATH:-/opt/rocm}
HIP_LIB=${HIP_LIB:-$ROCM/lib/libamdhip64.so.7}
HSA_LIB=${HSA_LIB:-$ROCM/lib/libhsa-runtime64.so.1}
PREBUILT=${PREBUILT:-$REPO/prebuilt/gfx1201}
TOKENIZER=${TOKENIZER:-$REPO/.cache/template_corpus.tokenizer.json}
CHAT_TEMPLATE=${CHAT_TEMPLATE:-$REPO/.cache/template_corpus.jinja}
SABER=${SABER_BIN:-$REPO/target/release/saber}

if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet qwen_prod_nvfp4; then
  echo "prod's unit qwen_prod_nvfp4 is ACTIVE; refusing to boot a second server on the cards" >&2
  exit 1
fi
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":$PORT\$"; then
  echo "port $PORT is already in use" >&2
  exit 1
fi
for f in "$HIP_LIB" "$HSA_LIB" "$TOKENIZER" "$CHAT_TEMPLATE"; do
  [ -f "$f" ] || { echo "missing $f (for packs/tokenizer, run tools/fetch_packs.sh $FETCH)" >&2; exit 1; }
done
[ -d "$PREBUILT" ] || { echo "missing prebuilt kernel directory $PREBUILT" >&2; exit 1; }
for rank in "${RANKS[@]}"; do
  for pack in "$PACKS/$TIER/rank$rank.pack" "$PACKS/$DRAFT/rank$rank.pack"; do
    [ -f "$pack" ] || { echo "missing $pack (run tools/fetch_packs.sh $FETCH)" >&2; exit 1; }
  done
done
if [ "$TP" = 1 ] && [ -e "$PACKS/$TIER/rank1.pack" ]; then
  echo "$PACKS/$TIER is a TP=2 pack; TP=1 needs the packs from tools/fetch_packs.sh 27b-tp1" >&2
  exit 1
fi

if [ "$BUILD" = 1 ]; then
  (cd "$REPO" && cargo build --release -p saber-cli)
fi
[ -x "$SABER" ] || { echo "no executable release binary at $SABER" >&2; exit 1; }

CMD=(
  "$SABER" serve --engine real "${TP_ARGS[@]}"
  --hip-lib "$HIP_LIB"
  --pack "$PACKS/$TIER"
  --prebuilt "$PREBUILT"
  --tokenizer "$TOKENIZER"
  --chat-template "$CHAT_TEMPLATE"
  --engine-max-ctx "$MAX_CTX"
  --engine-prefill-rows '96,128,192,256,320,384,512,768,1024,1536,2048'
  --engine-decode-batches "$BATCHES"
  --engine-decode-widths "${WIDTHS:-1,8}"
  --engine-max-seqs "$MAX_SEQS"
  --engine-draft-block "${DRAFT_BLOCK:-8}"
  --engine-draft-pack "$PACKS/$DRAFT"
  --engine-budget-mib "${BUDGET_MIB:-30500}"
  --engine-rectangular-widths
  --served-model-name "$MODEL"
  --host "$HOST"
  --port "$PORT"
  "$@"
)

ulimit -c 0
echo "== starting native Saber on $HOST:$PORT (TP=$TP, HIP_VISIBLE_DEVICES=$HIP_DEVICES, max ctx $MAX_CTX)" >&2
exec env \
  HIP_VISIBLE_DEVICES="$HIP_DEVICES" \
  GPU_MAX_HW_QUEUES="${GPU_MAX_HW_QUEUES:-3}" \
  CUDA_VISIBLE_DEVICES= \
  TOKENIZERS_PARALLELISM=false \
  LD_LIBRARY_PATH="$ROCM/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "${CMD[@]}"
