#!/usr/bin/env bash
# Keep JayNet's process attached to Saber's detached server container.
# Env comes from runtime/launch_argv.py; extra argv goes to upstream's server.
set -uo pipefail

SABER_REPO="${SABER_REPO:-}"
LAUNCHER="$SABER_REPO/tools/serve_27b.sh"
if [[ -z "$SABER_REPO" || ! -x "$LAUNCHER" ]]; then
    echo "saber: upstream launcher not executable: $LAUNCHER" >&2
    exit 1
fi
if ! grep -qF -- '--host ${HOST:-127.0.0.1} --port $PORT' "$LAUNCHER"; then
    echo "saber: upstream lacks the loopback-host patch; run build_tools.sh saber rocm" >&2
    exit 1
fi

TP_ARGS=()
if [[ -n "${DEVICE:-}" ]]; then
    TP_ARGS=(--tp 1)
elif [[ -n "${HIP_DEVICES:-}" ]]; then
    TP_ARGS=(--tp 2)
else
    echo "saber: set DEVICE=<card> for TP=1 or HIP_DEVICES=<card,card> for TP=2" >&2
    exit 1
fi

CNAME="${CNAME:-saber-jaynet-${PORT:-8080}}"
CTR="${SABER_CONTAINER:-}"
if [[ -z "$CTR" ]]; then
    if command -v podman >/dev/null 2>&1; then
        CTR=podman
    elif command -v docker >/dev/null 2>&1; then
        CTR=docker
    else
        echo "saber: neither podman nor docker is installed" >&2
        exit 1
    fi
fi

LOG_PID=""
WAIT_PID=""
CLEANED=0
cleanup() {
    [[ "$CLEANED" == "1" ]] && return
    CLEANED=1
    trap - EXIT TERM INT
    "$LAUNCHER" "${TP_ARGS[@]}" stop >/dev/null 2>&1 || true
    [[ -z "$WAIT_PID" ]] || kill "$WAIT_PID" >/dev/null 2>&1 || true
    [[ -z "$LOG_PID" ]] || kill "$LOG_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# build_tools.sh's saber-loopback-host patch makes upstream honor HOST;
# clap rejects passing a duplicate --host here.
"$LAUNCHER" "${TP_ARGS[@]}" --no-build "$@"
rc=$?
[[ "$rc" == "0" ]] || exit "$rc"

"$CTR" logs -f "$CNAME" &
LOG_PID=$!
"$CTR" wait "$CNAME" &
WAIT_PID=$!
wait "$WAIT_PID"
rc=$?
engine_rc="$("$CTR" inspect -f '{{.State.ExitCode}}' "$CNAME" 2>/dev/null)"
if [[ ! "$engine_rc" =~ ^[0-9]+$ ]]; then
    engine_rc="$rc"
fi
exit "$engine_rc"
