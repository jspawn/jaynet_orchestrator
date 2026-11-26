#!/usr/bin/env bash
# Compatibility entry point: run Ripple-65B through the unified Saber checkout.
set -euo pipefail
REPO=${SABER_REPO:-/srv/saber-jaynet-dev}
LAUNCHER="$REPO/tools/ripple_serve_native.sh"
[[ -x "$LAUNCHER" ]] || { echo "missing executable: $LAUNCHER" >&2; exit 1; }
exec "$LAUNCHER" "$@"
