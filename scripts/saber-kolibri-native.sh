#!/usr/bin/env bash
# Compatibility entry point: run Kolibri-1 through the unified Saber checkout.
set -euo pipefail
REPO=${SABER_REPO:-/srv/saber-jaynet-dev}
LAUNCHER="$REPO/tools/kolibri_serve_native.sh"
[[ -x "$LAUNCHER" ]] || { echo "missing executable: $LAUNCHER" >&2; exit 1; }
exec "$LAUNCHER" "$@"
