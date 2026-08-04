#!/usr/bin/env bash
# JayNet full installer — automates docs/install.md steps 0, 3, 4 and 5:
# base-package check, uv Python envs, ~/.config/orchestrator.env with
# generated secrets, systemd --user units. Idempotent: safe to re-run.
#
# Model downloads / llama.cpp builds / preset tuning are NOT done here —
# see docs/install.md steps 1-2.
#
# Flags:
#   --yes         non-interactive, accept defaults (services NOT started)
#   --start       enable + start the systemd units at the end
#   --with-tools  also install requirements-tools.txt into .venv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_ORCH_HOME="/srv/orchestrator"
ENV_FILE="$HOME/.config/orchestrator.env"

YES=0
START=0
WITH_TOOLS=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)     YES=1; shift ;;
        --start)      START=1; shift ;;
        --with-tools) WITH_TOOLS=1; shift ;;
        -h|--help)    sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# y/N prompt; only asked when stdin is a TTY and --yes is absent.
# Default answer is No (non-interactive runs never start services).
confirm() {
    local question="$1" answer
    if [[ $YES -eq 1 || ! -t 0 ]]; then
        return 1
    fi
    read -r -p "$question [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

gen_secret() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

# --- 1. Prereqs ----------------------------------------------------------------
log "Checking base packages"
MISSING=0
if ! command -v git >/dev/null 2>&1; then
    echo "  git: MISSING — install via your package manager, e.g.:" >&2
    echo "    sudo apt install git   |   sudo dnf install git   |   sudo pacman -S git" >&2
    MISSING=1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "  python3: MISSING — install >= 3.10 via your package manager, e.g.:" >&2
    echo "    sudo apt install python3   |   sudo dnf install python3   |   sudo pacman -S python" >&2
    MISSING=1
elif ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "  python3: $(python3 --version 2>&1) found, but >= 3.10 is required" >&2
    MISSING=1
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "  uv: MISSING — install with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    MISSING=1
fi
[[ $MISSING -eq 0 ]] || die "Install the missing base packages above and re-run."
log "Base packages OK (git, $(python3 --version 2>&1), $(uv --version 2>&1))"

# --- 2. Data dir ----------------------------------------------------------------
# Default /srv/data; honor ORCH_DATA from an existing env file.
DATA_DIR="/srv/data"
if [[ -f "$ENV_FILE" ]]; then
    # systemd EnvironmentFile lines may carry inline comments — take the first token.
    existing="$(grep -E '^ORCH_DATA=' "$ENV_FILE" | head -1 | cut -d= -f2- | awk '{print $1}')"
    [[ -n "$existing" ]] && DATA_DIR="$existing"
fi
if [[ -d "$DATA_DIR" ]]; then
    log "Data dir $DATA_DIR already exists"
else
    log "Creating data dir $DATA_DIR (sudo)"
    sudo mkdir -p "$DATA_DIR"
    sudo chown "$USER" "$DATA_DIR"
fi

# --- 3. Python envs (uv) ---------------------------------------------------------
cd "$SCRIPT_DIR"
if [[ -d .venv ]]; then
    log ".venv already exists — reusing"
else
    log "Creating .venv"
    uv venv .venv
fi
log "Installing requirements.txt + requirements-web.txt into .venv"
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-web.txt
if [[ $WITH_TOOLS -eq 1 ]]; then
    log "Installing requirements-tools.txt into .venv (--with-tools)"
    uv pip install --python .venv/bin/python -r requirements-tools.txt
fi
if [[ -d litellmenv ]]; then
    log "litellmenv already exists — reusing"
else
    log "Creating litellmenv"
    uv venv litellmenv
fi
log "Installing requirements-litellm.txt into litellmenv"
uv pip install --python litellmenv/bin/python -r requirements-litellm.txt

# --- 4. Env file -------------------------------------------------------------------
ADMIN_PASSWORD=""
if [[ -f "$ENV_FILE" ]]; then
    log "Env file $ENV_FILE already exists — leaving it untouched"
    if grep -qE '<[^>]+>' "$ENV_FILE"; then
        warn "$ENV_FILE still contains <...> placeholders — edit it before starting services"
    fi
else
    log "Installing $ENV_FILE with generated secrets"
    install -Dm600 "$SCRIPT_DIR/example_configs/orchestrator.env.example" "$ENV_FILE"
    # token_urlsafe(36) = 48 chars, token_urlsafe(24) = 32, token_urlsafe(12) = 16
    SESSION_SECRET="$(gen_secret 36)"
    WEB_TOKEN="$(gen_secret 36)"
    MASTER_KEY="sk-local-$(gen_secret 24)"
    ADMIN_PASSWORD="$(gen_secret 12)"
    sed -i \
        -e "s|^ORCH_SESSION_SECRET=<long-random-string>|ORCH_SESSION_SECRET=${SESSION_SECRET}|" \
        -e "s|^ORCH_WEB_TOKEN=<long-random-token>|ORCH_WEB_TOKEN=${WEB_TOKEN}|" \
        -e "s|^LITELLM_MASTER_KEY=<key>|LITELLM_MASTER_KEY=${MASTER_KEY}|" \
        -e "s|^# ORCH_ADMIN_USER=admin|ORCH_ADMIN_USER=admin|" \
        -e "s|^# ORCH_ADMIN_PASSWORD=change-me-then-remove|ORCH_ADMIN_PASSWORD=${ADMIN_PASSWORD}|" \
        "$ENV_FILE"
    # The template ships /srv/orchestrator paths; fix them when cloned elsewhere.
    if [[ "$SCRIPT_DIR" != "$DEFAULT_ORCH_HOME" ]]; then
        sed -i "s|${DEFAULT_ORCH_HOME}|${SCRIPT_DIR}|g" "$ENV_FILE"
        log "Adjusted ORCH_HOME/ORCH_CONFIG/PYTHONPATH/PATH to $SCRIPT_DIR"
    fi
fi

# --- 5. systemd user units -----------------------------------------------------------
log "Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$SCRIPT_DIR"/systemd/*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
loginctl enable-linger "$USER"
STARTED=0
if [[ $START -eq 1 ]] || confirm "Enable and start litellm-proxy + orchestrator-web now?"; then
    systemctl --user enable --now litellm-proxy orchestrator-web
    STARTED=1
fi

# --- 6. Summary ----------------------------------------------------------------------
echo
log "Setup complete"
echo "  done:  base packages checked, data dir $DATA_DIR, .venv + litellmenv,"
echo "         env file $ENV_FILE (mode 600), systemd units installed, linger on"
if [[ $STARTED -eq 1 ]]; then
    echo "         services litellm-proxy + orchestrator-web enabled and started"
else
    echo "  start: systemctl --user enable --now litellm-proxy orchestrator-web"
fi
echo "  left:  build/download llama.cpp + GGUF models and adjust presets/*.conf"
echo "         to your hardware — docs/install.md steps 1-2"
echo "  then:  browse to http://<host>:8071 (check Admin → Status)"
if [[ -n "$ADMIN_PASSWORD" ]]; then
    echo
    echo "  FIRST-BOOT ADMIN PASSWORD (shown once): $ADMIN_PASSWORD"
    echo "  Log in, then remove the ORCH_ADMIN_* lines from $ENV_FILE."
fi
