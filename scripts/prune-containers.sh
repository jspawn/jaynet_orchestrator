#!/usr/bin/env bash
# Reclaim container storage (readiness audit OPS-3: one benchlab session left
# 244.7 GB of eval images + leaked devbox containers on the data filesystem,
# with nothing reclaiming them).
#
# Safe to run any time:
#   - stops/removes leftover jaynet-devbox-* containers (per-run --rm boxes)
#   - removes containers that already exited
#   - removes UNUSED images (anything no running/stopped container references)
#     EXCEPT the devbox base image and the llama/vllm server images
#   - removes unreferenced named volumes (eval compose stacks)
#
#   scripts/prune-containers.sh           # dry run — shows what would go
#   scripts/prune-containers.sh --apply   # actually reclaim
set -euo pipefail

KEEP_IMAGES="${JAYNET_PRUNE_KEEP:-jaynet-devbox|llama|vllm|ollama}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

echo "== before =="
podman system df || true

run() { if [ "$APPLY" = 1 ]; then echo "+ $*"; "$@"; else echo "[dry] $*"; fi }

# 1. leftover devbox containers (running or dead — they are per-run boxes)
names="$(podman ps -a --filter 'name=jaynet-devbox-' --format '{{.Names}}' || true)"
if [ -n "$names" ]; then
  while IFS= read -r n; do [ -n "$n" ] && run podman rm -f "$n"; done <<< "$names"
fi

# 2. exited containers (eval cases that tore down without --rm)
run podman container prune -f

# 3. unused images, keeping the base images we need to rebuild/run
if [ "$APPLY" = 1 ]; then
  podman images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | while read -r ref id; do
    case "$ref" in
      *jaynet-devbox*|*llama*|*vllm*|*ollama*) : ;;          # keep
      "<none>"|"<none>:<none>") podman rmi "$id" 2>/dev/null || true ;;
      *) podman rmi "$ref" 2>/dev/null || true ;;            # in-use -> refused
    esac
  done
else
  echo "[dry] would remove all images except those matching: $KEEP_IMAGES"
fi

# 4. orphaned volumes (compose eval stacks)
run podman volume prune -f

echo "== after =="
[ "$APPLY" = 1 ] && podman system df || echo "(dry run — re-run with --apply to reclaim)"
