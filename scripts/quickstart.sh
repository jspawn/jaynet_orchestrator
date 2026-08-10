#!/usr/bin/env bash
# JayNet minimal quick start — the throwaway try-out. Automates the README
# "Quick start" section: prebuilt llama-server (Linux x86_64; macOS Apple
# Silicon experimental), one small GGUF, the .venv, a start.sh that runs
# model + app in one terminal, and the runtime.yaml edit that disables the
# example host's process autostart. Asks for the data + models dirs
# (defaults ~/jaynet-data / ~/jaynet-models, write access checked; pre-set
# JAYNET_DATA/JAYNET_MODELS — or legacy ORCH_* — win). Idempotent: safe to re-run.
#
# Usage: quickstart.sh [--yes] [hf-repo]
#   --yes     non-interactive (pull-model will print its file list and stop
#             so you can re-run with an explicit file choice)
#   hf-repo   default: Qwen/Qwen3-1.7B-GGUF
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_REPO="Qwen/Qwen3-1.7B-GGUF"
YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)  YES=1; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *)         MODEL_REPO="$1"; shift ;;
    esac
done

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ask_path <varname> <question> <default> — same behavior as setup.sh:
# interactive prompt (empty = default), re-ask until creatable + writable;
# with --yes the default is used as-is. Expands a leading ~/ .
ask_path() {
    local __var="$1" question="$2" default="$3" answer path attempts=0
    while :; do
        path="$default"
        if [[ $YES -eq 0 && -t 0 ]]; then
            read -r -p "$question [$default] " answer
            [[ -n "$answer" ]] && path="$answer"
            if [[ -z "$path" ]]; then warn "please enter a path"; continue; fi
        fi
        path="${path/#\~/$HOME}"
        if mkdir -p "$path" 2>/dev/null && [[ -w "$path" ]]; then
            printf -v "$__var" '%s' "$path"
            return 0
        fi
        warn "cannot create or write to $path — pick a path your user owns"
        attempts=$((attempts+1))
        if [[ $YES -eq 1 || ! -t 0 || $attempts -ge 3 ]]; then
            die "no writable directory for: $question"
        fi
        default=""
    done
}

# --- 1. Platform check -----------------------------------------------------------
# Maps the platform to the upstream prebuilt llama.cpp asset suffix(es).
PLATFORM="$(uname -sm)"
case "$PLATFORM" in
    "Linux x86_64")  ASSET_SUFFIXES=("-bin-ubuntu-x64.zip" "-bin-ubuntu-x64.tar.gz") ;;
    "Darwin arm64")  ASSET_SUFFIXES=("-bin-macos-arm64.zip")
                     log "macOS Apple Silicon — experimental: Metal build, no firejail sandbox, no services" ;;
    "Darwin x86_64") ASSET_SUFFIXES=("-bin-macos-x64.zip")
                     warn "Intel Mac — legacy path: the prebuilt asset may lag or disappear upstream" ;;
    *) die "quickstart supports Linux x86_64 and macOS (got: $PLATFORM) — for anything else see docs/install.md 'Preparing llama.cpp'" ;;
esac

# --- 1b. Tools we need along the way ----------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 missing — install >= 3.10 via your package manager"
command -v uv >/dev/null 2>&1      || die "uv missing — install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v curl >/dev/null 2>&1    || die "curl missing — install via your package manager"

# --- 1c. Ports ----------------------------------------------------------------------
# SO_REUSEADDR matches what the servers do — without it a just-stopped run
# (sockets in TIME_WAIT) would look like "port in use" for ~60s.
port_free() {
    python3 -c 'import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError: sys.exit(1)
s.close()' "$1"
}
# ask_port <varname> <question> <default> — same spirit as ask_path: interactive
# prompt (empty = default), re-ask until the port is valid AND free; with --yes
# there is no asking, so a taken default is a hard error (die).
ask_port() {
    local __var="$1" question="$2" default="$3" answer port attempts=0
    while :; do
        port="$default"
        if [[ $YES -eq 0 && -t 0 ]]; then
            read -r -p "$question [$default] " answer
            [[ -n "$answer" ]] && port="$answer"
        fi
        if [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1 && "$port" -le 65535 ]]; then
            if port_free "$port"; then
                printf -v "$__var" '%s' "$port"
                return 0
            fi
            warn "port $port is already in use — pick another one"
        else
            warn "not a valid port: $port"
        fi
        attempts=$((attempts+1))
        if [[ $YES -eq 1 || ! -t 0 || $attempts -ge 5 ]]; then
            die "no free port for: $question — free port $default and re-run"
        fi
        default=""
    done
}
# The model port doubles as the app's litellm_base (quickstart runs no LiteLLM
# proxy — llama-server speaks the OpenAI API directly), so a custom model port
# is also written into config/runtime.yaml further down.
ask_port MODEL_PORT "Port for the model (llama-server)" "${JAYNET_LITELLM_PORT:-${ORCH_LITELLM_PORT:-4000}}"
ask_port WEB_PORT   "Port for the web app"              "${JAYNET_WEB_PORT:-${ORCH_WEB_PORT:-8071}}"

# --- 1d. Locations -----------------------------------------------------------------
# Data + models dirs, exported so pull-model and (later) the app see them.
# Pre-set JAYNET_DATA/JAYNET_MODELS (or legacy ORCH_*) win; otherwise ask,
# defaulting to ~/jaynet-*.
ask_path DATA_DIR "Data dir (chats, users, projects)" "${JAYNET_DATA:-${ORCH_DATA:-$HOME/jaynet-data}}"
ask_path MODELS_DIR "Models dir (GGUF files — needs a few GB free)" "${JAYNET_MODELS:-${ORCH_MODELS:-$HOME/jaynet-models}}"
export JAYNET_DATA="$DATA_DIR" JAYNET_MODELS="$MODELS_DIR"
# Legacy names too: preset .conf files expand $ORCH_MODELS textually.
export ORCH_DATA="$DATA_DIR" ORCH_MODELS="$MODELS_DIR"
log "Data dir:   $DATA_DIR"
log "Models dir: $MODELS_DIR"

# --- 2. Prebuilt llama.cpp CPU binary ----------------------------------------------
cd "$SCRIPT_DIR"
if [[ -x bin/llama-server ]]; then
    log "bin/llama-server already present — skipping download"
else
    log "Fetching latest llama.cpp release (${ASSET_SUFFIXES[0]#-bin-} build)"
    # Upstream ships the build as llama-*<suffix> — the platform check above
    # picked the suffix candidates, first match wins.
    ASSET_URL="$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
        | python3 -c '
import json, sys
suffixes = sys.argv[1:]
data = json.load(sys.stdin)
assets = [a for a in data["assets"] if a["name"].startswith("llama-")]
for suffix in suffixes:
    for a in assets:
        if a["name"].endswith(suffix):
            print(a["browser_download_url"]); sys.exit(0)
sys.exit("no llama-*%s asset in latest release" % suffixes[0])' "${ASSET_SUFFIXES[@]}")"
    log "Downloading $ASSET_URL"
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    curl -fSL -o "$TMP_DIR/asset" "$ASSET_URL"
    mkdir -p "$TMP_DIR/x"
    if [[ "$ASSET_URL" == *.zip ]]; then
        command -v unzip >/dev/null 2>&1 || die "unzip missing — install via your package manager, e.g.: sudo apt install unzip | sudo dnf install unzip | sudo pacman -S unzip"
        unzip -q "$TMP_DIR/asset" -d "$TMP_DIR/x"
    else
        tar -xzf "$TMP_DIR/asset" -C "$TMP_DIR/x"
    fi
    # The archive nests the binaries (build/bin/…); copy the directory that
    # contains llama-server wholesale — its shared libs sit next to it.
    # NB: find prints nothing when the asset lacks llama-server, and
    # dirname "" is "." — a bare -d check would pass and copy the whole cwd.
    SERVER_BIN="$(find "$TMP_DIR/x" -name llama-server -type f | head -1)"
    [[ -n "$SERVER_BIN" && -f "$SERVER_BIN" ]] || die "llama-server not found inside the release asset"
    SRC_DIR="$(dirname "$SERVER_BIN")"
    mkdir -p bin
    cp -Rp "$SRC_DIR/." bin/   # -Rp: portable form of -a (BSD/macOS cp has no -a)
    chmod +x bin/llama-server
    rm -rf "$TMP_DIR"
    trap - EXIT
fi

# --- 2b. Shared library check (Linux) -----------------------------------------------
# The prebuilt binary dynamically links e.g. libgomp (OpenMP), which stock
# Ubuntu/WSL does not ship — fail here with the apt/pacman hint instead of
# letting start.sh die on a linker error. Runs every time (cheap), not only
# after a fresh download. macOS: libs are covered by the OS, skip.
if [[ "$PLATFORM" == "Linux x86_64" ]]; then
    MISSING_LIBS="$(ldd bin/llama-server 2>/dev/null | awk '/not found/{print $1}' | sort -u | tr '\n' ' ')"
    if [[ -n "${MISSING_LIBS// /}" ]]; then
        die "llama-server is missing shared libraries: $MISSING_LIBS
     Ubuntu/Debian: sudo apt install libgomp1     (covers libgomp.so.1)
     Arch:          sudo pacman -S gcc-libs
     Then re-run scripts/quickstart.sh"
    fi
fi

# --- 3. One small GGUF --------------------------------------------------------------
PULL_MODEL="$SCRIPT_DIR/scripts/pull-model"
[[ -x "$PULL_MODEL" ]] || die "scripts/pull-model not found or not executable — it should ship with this repo"
log "Downloading a model from $MODEL_REPO (pick a file when prompted)"
PULL_OUT="$(mktemp)"
PULL_ARGS=("$MODEL_REPO")
[[ $YES -eq 1 ]] && PULL_ARGS+=("--yes")
# tee: pull-model's menu/listing stays visible; we parse MODEL_PATH afterwards.
# PIPESTATUS keeps pull-model's exit code under pipefail.
rc=0
"$PULL_MODEL" "${PULL_ARGS[@]}" 2>&1 | tee "$PULL_OUT" || rc=${PIPESTATUS[0]}
[[ $rc -eq 0 ]] || die "scripts/pull-model failed (exit $rc) — pick a file from the list above and re-run: $PULL_MODEL $MODEL_REPO <file>"
MODEL_PATH="$(grep '^MODEL_PATH=' "$PULL_OUT" | tail -1 | cut -d= -f2-)"
rm -f "$PULL_OUT"
[[ -n "$MODEL_PATH" ]] || die "could not parse MODEL_PATH from scripts/pull-model output"

# --- 4. Python env ---------------------------------------------------------------------
if [[ -d .venv ]]; then
    log ".venv already exists — reusing"
else
    log "Creating .venv"
    uv venv .venv
fi
log "Installing requirements.txt + requirements-web.txt into .venv"
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-web.txt

# --- 5. Disable the processes: autostart block in config/runtime.yaml ---------------------
# The shipped config auto-launches the example host's model presets, which don't
# exist here. Comment out the top-level `processes:` block (through its indented
# children) — text edit only, comments elsewhere must survive.
RUNTIME_YAML="$SCRIPT_DIR/config/runtime.yaml"
if ! grep -q '^processes:' "$RUNTIME_YAML"; then
    log "processes: block already disabled — skipping"
else
    log "Commenting out the processes: block in config/runtime.yaml (backup: runtime.yaml.bak)"
    [[ -f "$RUNTIME_YAML.bak" ]] || cp "$RUNTIME_YAML" "$RUNTIME_YAML.bak"
    awk '
        /^processes:/   { inblock=1; print "# " $0; next }
        inblock && /^$/ { print; next }              # blank line: keep, stay in block
        inblock && /^ / { print "# " $0; next }      # indented child line
        inblock         { inblock=0 }                # next top-level line ends the block
        { print }
    ' "$RUNTIME_YAML.bak" > "$RUNTIME_YAML"
fi

# Point the app at the chosen model port — in quickstart mode there is no
# LiteLLM proxy, llama-server speaks the OpenAI API straight to the app, and
# runtime.yaml's orchestrator.litellm_base wins over the env default (:4000).
if [[ "$MODEL_PORT" != "4000" ]]; then
    log "Setting orchestrator.litellm_base to port $MODEL_PORT in config/runtime.yaml"
    [[ -f "$RUNTIME_YAML.bak" ]] || cp "$RUNTIME_YAML" "$RUNTIME_YAML.bak"
    sed -i "s|^  litellm_base: .*|  litellm_base: http://127.0.0.1:$MODEL_PORT|" "$RUNTIME_YAML"
fi

# --- 6. start.sh ------------------------------------------------------------------------
# One command to run both processes: the model in the background, the app in
# the foreground; Ctrl+C (or any exit) takes the model down via the trap.
# Paths are baked in at generation time — re-run quickstart.sh to regenerate.
START_SH="$SCRIPT_DIR/start.sh"
log "Writing $START_SH"
cat > "$START_SH" <<EOF
#!/usr/bin/env bash
# Generated by scripts/quickstart.sh — starts the model (llama-server) and
# the JayNet web app in ONE terminal. Ctrl+C stops both.
set -euo pipefail
cd "$SCRIPT_DIR"
export JAYNET_HOME="$SCRIPT_DIR"
export JAYNET_DATA="$DATA_DIR"
export JAYNET_MODELS="$MODELS_DIR"
# Legacy names too: preset .conf files expand \$ORCH_MODELS textually.
export ORCH_DATA="$DATA_DIR" ORCH_MODELS="$MODELS_DIR"
export JAYNET_LITELLM_PORT="$MODEL_PORT" JAYNET_WEB_PORT="$WEB_PORT"
export ORCH_LITELLM_PORT="$MODEL_PORT" ORCH_WEB_PORT="$WEB_PORT"

# Refuse to die halfway: both ports must be free before we start anything.
# SO_REUSEADDR matches what the servers do — without it a just-stopped run
# (sockets in TIME_WAIT) would look like "port in use" for ~60s.
port_free() {
    python3 -c 'import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError: sys.exit(1)
s.close()' "\$1"
}
for PORT in $MODEL_PORT $WEB_PORT; do
    if ! port_free "\$PORT"; then
        echo "ERROR: port \$PORT is already in use — is another JayNet instance still running?" >&2
        echo "       Free it and retry, or re-run scripts/quickstart.sh to pick different ports." >&2
        exit 1
    fi
done

echo "==> Starting the model (llama-server, port $MODEL_PORT) ..."
./bin/llama-server -m "$MODEL_PATH" --port $MODEL_PORT -c 16384 &
LLAMA_PID=\$!
cleanup() {
    trap - EXIT INT TERM   # run once, not on both INT and EXIT
    echo
    echo "==> Stopping llama-server ..."
    kill "\$LLAMA_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Waiting for the model to load ..."
READY=0
for _ in \$(seq 1 120); do
    if curl -sf http://127.0.0.1:$MODEL_PORT/health >/dev/null 2>&1; then READY=1; break; fi
    if ! kill -0 "\$LLAMA_PID" 2>/dev/null; then
        echo "ERROR: llama-server exited — see its messages above" >&2
        exit 1
    fi
    sleep 1
done
if [[ "\$READY" -ne 1 ]]; then
    echo "ERROR: model did not become ready within 120s" >&2
    exit 1
fi

echo "==> Model is up — JayNet starting on http://127.0.0.1:$WEB_PORT"
echo "    (the admin password is generated and logged below on first boot)"
.venv/bin/uvicorn web.server:app --host 127.0.0.1 --port $WEB_PORT
EOF
chmod +x "$START_SH"

# --- 7. How to run ------------------------------------------------------------------------
echo
log "Quick start ready — run it with:"
echo
echo "  ./start.sh"
echo
echo "  Then open http://127.0.0.1:$WEB_PORT and log in with the generated admin"
echo "  password from the log. Ctrl+C stops both the app and the model."
