#!/usr/bin/env bash
# Manual-phase convenience: bring the whole local stack up in ONE terminal,
# tear it ALL down with a single Ctrl-C. For debugging before you create the
# systemd units. Once the units exist, use `orchstart`/`orchstop` instead and
# stop using this script.
#
# Starts:
#   1. LiteLLM proxy        (background, :4000)
#   2. orchestrator brain   (foreground, llama-server :8090 via start-llama.sh)
# Ctrl-C stops both, proxy last.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${ORCH_ENV:-$HOME/.config/orchestrator.env}"
LITELLM_BIN="/srv/orchestrator/litellmenv/bin/litellm"
LITELLM_CFG="/srv/orchestrator/config/litellm.yaml"
PROXY_LOG="${PROXY_LOG:-/tmp/litellm-proxy.log}"

# -- Load API keys + master key ---------------------------------------------
if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
else
    echo "Error: env file not found at $ENV_FILE (API keys live there)" >&2
    exit 1
fi
[[ -x "$LITELLM_BIN" ]] || { echo "Error: litellm not found at $LITELLM_BIN" >&2; exit 1; }

PROXY_PID=""
cleanup() {
    echo -e "\n[!] Tearing down stack..."
    # start-llama.sh (foreground) gets the Ctrl-C itself and stops its own
    # llama-server; here we just stop the proxy we backgrounded.
    if [[ -n "$PROXY_PID" ]]; then
        echo "[!] Stopping LiteLLM proxy (pid $PROXY_PID)..."
        kill -TERM "$PROXY_PID" 2>/dev/null
        wait "$PROXY_PID" 2>/dev/null
    fi
    echo "[*] Stack down."
    exit 0
}
trap cleanup SIGINT SIGTERM

# -- 1. LiteLLM proxy in the background -------------------------------------
echo "[>] Starting LiteLLM proxy on :4000 (log: $PROXY_LOG)..."
"$LITELLM_BIN" --config "$LITELLM_CFG" --host 127.0.0.1 --port 4000 \
    > "$PROXY_LOG" 2>&1 &
PROXY_PID=$!

# Wait for the proxy to answer before launching the brain.
echo -n "[>] Waiting for proxy"
for i in {1..30}; do
    if curl -sf "http://127.0.0.1:4000/health/readiness" >/dev/null 2>&1 \
       || curl -sf "http://127.0.0.1:4000/v1/models" \
            -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-}" >/dev/null 2>&1; then
        echo " — ready."
        break
    fi
    # If the proxy died during startup, surface its log and bail.
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        echo " — FAILED."
        echo "[!] Proxy exited during startup. Last log lines:" >&2
        tail -n 20 "$PROXY_LOG" >&2
        exit 1
    fi
    echo -n "."; sleep 1
done

# -- 2. Orchestrator brain in the foreground --------------------------------
# start-brain1.sh installs its own SIGINT trap; when you Ctrl-C, it stops
# llama-server and returns here, then our trap stops the proxy.
echo "[i] Tools are owned by the runtime, not these servers. To see what the"
echo "    orchestrator will load, run (in another shell, anytime):"
echo "      /srv/orchestrator/.venv/bin/python /srv/orchestrator/scripts/orch --list-tools"
echo "[>] Starting orchestrator brain (foreground)..."
"$HERE/start-brain1.sh" || true

# If start-brain1.sh exits on its own (e.g. error), still tear down the proxy.
cleanup
