#!/usr/bin/env bash
# brain-swap — point a model slot at a preset and restart the process.
#
#   brain-swap.sh <preset-name> [slot]     # slot defaults to brain
#
# Verifies the served model afterwards (polls the slot's port). Reads
# JAYNET_WEB_TOKEN from ~/.config/jaynet.env (override: JAYNET_ENV_FILE,
# JAYNET_ADMIN). Rollback is the same command with the old preset name —
# presets are never deleted by a swap.
set -euo pipefail
PRESET="${1:?usage: brain-swap.sh <preset-name> [slot]}"
SLOT="${2:-brain}"
ENV_FILE="${JAYNET_ENV_FILE:-$HOME/.config/jaynet.env}"
ADMIN="${JAYNET_ADMIN:-http://127.0.0.1:8071}"
TOKEN="$(grep -m1 '^JAYNET_WEB_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d "\"'")"

echo ">> slot $SLOT -> $PRESET"
curl -sf -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"updates\": {\"$SLOT\": \"$PRESET\"}}" "$ADMIN/api/admin/preset-slots" \
    | python3 -c "import json,sys; print('   slots:', json.load(sys.stdin)['slots'])"

echo ">> restarting $SLOT"
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
    "$ADMIN/api/admin/processes/$SLOT/restart" > /dev/null

# preset port comes from the presets payload; poll until it serves
PORT=$(curl -sf -H "Authorization: Bearer $TOKEN" "$ADMIN/api/admin/presets" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(p['port'] for p in d['presets'] if p['name']=='$PRESET'))")
echo ">> waiting for :$PORT to serve"
for i in $(seq 1 20); do
    sleep 10
    SERVED=$(curl -sf -m 5 "http://127.0.0.1:$PORT/v1/models" \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null) \
        && { echo ">> serving: $SERVED"; exit 0; }
    echo "   ... loading (${i}0s)"
done
echo "!! not serving after 200s — check: admin → Processes → $SLOT logs" >&2
exit 1
