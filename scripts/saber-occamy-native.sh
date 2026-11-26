#!/usr/bin/env bash
# Compatibility entry point: run Occamy through the unified Saber checkout.
set -euo pipefail
REPO=${SABER_REPO:-/srv/saber-jaynet-dev}
LAUNCHER="$REPO/tools/serve_occamy_native.sh"
[[ -x "$LAUNCHER" ]] || { echo "missing executable: $LAUNCHER" >&2; exit 1; }
exec "$LAUNCHER" "$@"
