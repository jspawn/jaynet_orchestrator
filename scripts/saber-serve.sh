#!/usr/bin/env bash
# Run Saber natively, or attach to its detached container as a fallback.
# Env comes from runtime/launch_argv.py; extra argv goes to upstream's server.
set -uo pipefail

SABER_REPO="${SABER_REPO:-}"
NATIVE_LAUNCHER="$SABER_REPO/tools/serve_27b_native.sh"
RUNTIME="${SABER_RUNTIME:-auto}"
if [[ "$RUNTIME" != auto && "$RUNTIME" != native && "$RUNTIME" != container ]]; then
    echo "saber: SABER_RUNTIME must be auto, native or container" >&2
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

if [[ "$RUNTIME" != container && -x "$NATIVE_LAUNCHER" ]]; then
    if ! grep -qF -- 'HOST=${HOST:-127.0.0.1}' "$NATIVE_LAUNCHER"; then
        echo "saber: native launcher lacks the loopback default; run build_tools.sh saber rocm" >&2
        exit 1
    fi
    command -v setsid >/dev/null 2>&1 || { echo "saber: setsid not found" >&2; exit 1; }
    command -v python3 >/dev/null 2>&1 || { echo "saber: python3 not found" >&2; exit 1; }
    NATIVE_PID=""
    cleanup_native() {
        trap - EXIT TERM INT
        if [[ -n "$NATIVE_PID" ]] && kill -0 "$NATIVE_PID" >/dev/null 2>&1; then
            # JayNet stops with SIGTERM; Saber's graceful shutdown is SIGINT.
            kill -INT -- "-$NATIVE_PID" >/dev/null 2>&1 || true
        fi
        [[ -z "$NATIVE_PID" ]] || wait "$NATIVE_PID" >/dev/null 2>&1 || true
    }
    trap cleanup_native EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    # A separate process group lets us interrupt both TP ranks without also
    # signaling this lifecycle wrapper or an unrelated server. Async shells
    # inherit SIGINT as ignored, so Python restores the default before exec.
    setsid --wait python3 -c \
        'import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execvp(sys.argv[1], sys.argv[1:])' \
        "$NATIVE_LAUNCHER" "${TP_ARGS[@]}" --no-build "$@" &
    NATIVE_PID=$!
    wait "$NATIVE_PID"
    rc=$?
    exit "$rc"
fi
if [[ "$RUNTIME" == native ]]; then
    echo "saber: native launcher not executable: $NATIVE_LAUNCHER" >&2
    exit 1
fi

LAUNCHER="$SABER_REPO/tools/serve_27b.sh"
if [[ -z "$SABER_REPO" || ! -x "$LAUNCHER" ]]; then
    echo "saber: upstream launcher not executable: $LAUNCHER" >&2
    exit 1
fi
if ! grep -qF -- '--host ${HOST:-127.0.0.1} --port $PORT' "$LAUNCHER"; then
    echo "saber: upstream lacks the loopback-host patch; run build_tools.sh saber rocm" >&2
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
