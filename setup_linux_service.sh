#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="tracker-network-stock"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONFIG_DIR="/etc/tracker-network-stock"
ENV_FILE="$CONFIG_DIR/bot.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DATA_DIR="${STOCK_BOT_DATA_DIR:-$REPO_DIR/data}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$USER}}"
RUN_GROUP="${RUN_GROUP:-$(id -gn "$RUN_USER")}"

log() {
    printf '[setup] %s\n' "$1"
}

warn() {
    printf '[warn] %s\n' "$1" >&2
}

fail() {
    printf '[error] %s\n' "$1" >&2
    exit 1
}

run_root() {
    if [[ "$EUID" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

show_troubleshooting() {
    cat <<EOF

Troubleshooting commands:
  sudo systemctl status ${SERVICE_NAME}.service
  journalctl -u ${SERVICE_NAME}.service -n 100 --no-pager
  sudo sed -n '1,40p' ${SERVICE_FILE}
  sudo sed -n '1,10p' ${ENV_FILE}
  ${VENV_DIR}/bin/python ${REPO_DIR}/bot.py

Common fixes:
  - If status says bad-setting, the service file is malformed or not overwritten.
  - If Python cannot be found, verify VENV_DIR and ExecStart paths.
  - If the bot exits immediately, check DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in ${ENV_FILE}.
  - If Playwright fails, rerun: ${VENV_DIR}/bin/playwright install --with-deps chromium
EOF
}

require_file() {
    local file_path="$1"
    [[ -f "$file_path" ]] || fail "Missing required file: $file_path"
}

write_env_file() {
    if [[ -f "$ENV_FILE" ]]; then
        log "Using existing env file at $ENV_FILE"
        return
    fi

    if [[ -n "${DISCORD_BOT_TOKEN:-}" && -n "${DISCORD_CHANNEL_ID:-}" ]]; then
        log "Creating env file from current environment variables"
        run_root mkdir -p "$CONFIG_DIR"
        run_root /bin/sh -c "cat > '$ENV_FILE' <<EOF
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
DISCORD_CHANNEL_ID=${DISCORD_CHANNEL_ID}
EOF"
        return
    fi

    log "Creating placeholder env file at $ENV_FILE"
    run_root mkdir -p "$CONFIG_DIR"
    run_root cp "$REPO_DIR/tracker-network-stock.env.example" "$ENV_FILE"
    warn "Edit $ENV_FILE and replace placeholder values before starting the service."
}

validate_env_file() {
    if ! run_root test -f "$ENV_FILE"; then
        fail "Env file not found at $ENV_FILE"
    fi

    if run_root grep -Eq '^DISCORD_BOT_TOKEN=replace-me$|^DISCORD_BOT_TOKEN=$' "$ENV_FILE"; then
        warn "DISCORD_BOT_TOKEN is still a placeholder in $ENV_FILE"
        return 1
    fi

    if run_root grep -Eq '^DISCORD_CHANNEL_ID=123456789012345678$|^DISCORD_CHANNEL_ID=$' "$ENV_FILE"; then
        warn "DISCORD_CHANNEL_ID is still a placeholder in $ENV_FILE"
        return 1
    fi

    return 0
}

write_service_file() {
    log "Writing systemd unit to $SERVICE_FILE"
    run_root /bin/sh -c "cat > '$SERVICE_FILE' <<EOF
[Unit]
Description=Discord Stock Tracker Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=STOCK_BOT_DATA_DIR=${DATA_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${REPO_DIR}/bot.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF"
}

install_runtime() {
    log "Creating virtual environment in $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"

    log "Installing Python dependencies"
    "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

    log "Installing Playwright Chromium dependencies"
    "$VENV_DIR/bin/playwright" install --with-deps chromium
}

start_service() {
    log "Reloading systemd and starting ${SERVICE_NAME}.service"
    run_root systemctl daemon-reload
    run_root systemctl reset-failed
    run_root systemctl enable --now "${SERVICE_NAME}.service"
}

main() {
    require_file "$REPO_DIR/bot.py"
    require_file "$REPO_DIR/requirements.txt"
    require_file "$REPO_DIR/tracker-network-stock.env.example"

    command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python interpreter not found: $PYTHON_BIN"
    command -v systemctl >/dev/null 2>&1 || fail "systemctl is required for this setup"

    log "Repo directory: $REPO_DIR"
    log "Service user/group: $RUN_USER:$RUN_GROUP"

    install_runtime
    write_env_file
    write_service_file

    if ! validate_env_file; then
        show_troubleshooting
        exit 1
    fi

    start_service

    log "Service installed. Current status:"
    run_root systemctl status "${SERVICE_NAME}.service" --no-pager

    cat <<EOF

Setup complete.
Service file: $SERVICE_FILE
Env file: $ENV_FILE
Data directory: $DATA_DIR
EOF
}

trap 'warn "Setup failed."; show_troubleshooting' ERR

main "$@"