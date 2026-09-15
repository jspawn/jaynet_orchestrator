#!/usr/bin/env bash
# eval-delta — start an eval run against the live JayNet admin API.
#
#   eval-delta.sh                 # delta: all cases, stable (3x-pass) skipped,
#                                 #   10% randomly re-included as sentinels
#   eval-delta.sh ID [ID...]      # explicit case ids (skip_stable is bulk-only)
#
# Reads JAYNET_WEB_TOKEN from ~/.config/jaynet.env (override: JAYNET_ENV_FILE,
# JAYNET_ADMIN). Prints the API reply and the run status after a short wait.
set -euo pipefail
ENV_FILE="${JAYNET_ENV_FILE:-$HOME/.config/jaynet.env}"
ADMIN="${JAYNET_ADMIN:-http://127.0.0.1:8071}"
TOKEN="$(grep -m1 '^JAYNET_WEB_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'"'"'")"

if [[ $# -gt 0 ]]; then
    IDS=$(printf ',"%s"' "$@"); IDS="[${IDS:1}]"
    BODY="{\"ids\": $IDS}"
else
    BODY='{"all": true, "skip_stable": true}'
fi

curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "$BODY" "$ADMIN/api/admin/evals/run"
echo
sleep 15
curl -sf -H "Authorization: Bearer $TOKEN" "$ADMIN/api/admin/evals/run-status"
echo
