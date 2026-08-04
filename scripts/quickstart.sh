#!/usr/bin/env bash
# JayNet minimal quick start — automates README "Quick start (minimal, ~15 min)":
# prebuilt CPU llama-server, one small GGUF, the .venv, data dir, and the
# runtime.yaml edit that disables the example host's process autostart.
# Idempotent: safe to re-run.
#
# Usage: quickstart.sh [--yes] [hf-repo]
#   --yes     non-interactive (pull-model will print its file list and stop
#             so you can re-run with an explicit file choice)
#   hf-repo   default: Qwen/Qwen3-4B-GGUF
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_REPO="Qwen/Qwen3-4B-GGUF"
YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)  YES=1; shift ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *)         MODEL_REPO="$1"; shift ;;
    esac
done

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Platform check -----------------------------------------------------------
PLATFORM="$(uname -sm)"
[[ "$PLATFORM" == "Linux x86_64" ]] || die "quickstart only supports Linux x86_64 (got: $PLATFORM) — for anything else see README 'Preparing llama.cpp'"

# --- 1b. Tools we need along the way ----------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 missing — install >= 3.10 via your package manager"
command -v uv >/dev/null 2>&1      || die "uv missing — install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v curl >/dev/null 2>&1    || die "curl missing — install via your package manager"

# --- 2. Prebuilt llama.cpp CPU binary ----------------------------------------------
cd "$SCRIPT_DIR"
if [[ -x bin/llama-server ]]; then
    log "bin/llama-server already present — skipping download"
else
    log "Fetching latest llama.cpp release (ubuntu-x64 CPU build)"
    # Upstream ships the CPU build as llama-*-bin-ubuntu-x64.zip, or as
    # .tar.gz on newer tags — accept both, prefer the zip.
    ASSET_URL="$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
        | python3 -c '
import json, sys
data = json.load(sys.stdin)
assets = [a for a in data["assets"] if a["name"].startswith("llama-")]
for suffix in ("-bin-ubuntu-x64.zip", "-bin-ubuntu-x64.tar.gz"):
    for a in assets:
        if a["name"].endswith(suffix):
            print(a["browser_download_url"]); sys.exit(0)
sys.exit("no llama-*-bin-ubuntu-x64 asset in latest release")')"
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
    SRC_DIR="$(dirname "$(find "$TMP_DIR/x" -name llama-server -type f | head -1)")"
    [[ -n "$SRC_DIR" && -d "$SRC_DIR" ]] || die "llama-server not found inside the release asset"
    mkdir -p bin
    cp -a "$SRC_DIR/." bin/
    chmod +x bin/llama-server
    rm -rf "$TMP_DIR"
    trap - EXIT
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

# --- 5. Data dir ------------------------------------------------------------------------
if [[ -d /srv/data ]]; then
    log "/srv/data already exists"
else
    log "Creating /srv/data (sudo)"
    sudo mkdir -p /srv/data
    sudo chown "$USER" /srv/data
fi

# --- 6. Disable the processes: autostart block in config/runtime.yaml ---------------------
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

# --- 7. How to run ------------------------------------------------------------------------
echo
log "Quick start ready — run these two commands (two terminals):"
echo
echo "  # terminal 1 — the model (port 4000 is where the app looks by default)"
echo "  ./bin/llama-server -m $MODEL_PATH --port 4000 -c 16384"
echo
echo "  # terminal 2 — the app (admin password is generated and logged on first boot)"
echo "  .venv/bin/uvicorn web.server:app --host 127.0.0.1 --port 8071"
echo
echo "  Then open http://127.0.0.1:8071 and log in with the generated admin"
echo "  password from the terminal-2 log."
