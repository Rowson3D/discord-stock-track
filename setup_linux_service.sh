#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="tracker-network-stock"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONFIG_DIR="/etc/tracker-network-stock"
ENV_FILE="$CONFIG_DIR/bot.env"
ENV_TEMPLATE_FILE="$REPO_DIR/tracker-network-stock.env.example"
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

is_placeholder_token() {
    local value="${1:-}"
    [[ -z "$value" || "$value" == "replace-me" || "$value" == "YOUR_BOT_TOKEN_HERE" ]]
}

is_valid_channel_id() {
    local value="${1:-}"
    [[ "$value" =~ ^[0-9]{17,20}$ ]] && [[ "$value" != "123456789012345678" ]]
}

has_prompt_tty() {
    [[ -r /dev/tty && -w /dev/tty ]]
}

extract_env_value_root() {
    local key="$1"
    run_root /bin/sh -c "grep -E '^${key}=' '$ENV_FILE' | head -n 1 | cut -d= -f2-" 2>/dev/null || true
}

show_discord_setup_guide() {
    cat <<EOF

Discord setup checklist:
  1. Create a bot in https://discord.com/developers/applications
  2. Enable Message Content Intent in the Bot tab
  3. Invite the bot with: View Channels, Send Messages, Embed Links
  4. Copy the bot token
  5. Copy the target channel ID (Developer Mode must be enabled in Discord)
EOF
}

prompt_bot_token() {
    local current_value="${1:-}"
    local value

    while true; do
        if ! is_placeholder_token "$current_value"; then
            printf 'Discord bot token [press Enter to keep existing]: ' > /dev/tty
        else
            printf 'Discord bot token: ' > /dev/tty
        fi

        IFS= read -r -s value < /dev/tty || fail "Unable to read Discord bot token"
        printf '\n' > /dev/tty

        if [[ -z "$value" ]]; then
            value="$current_value"
        fi

        if ! is_placeholder_token "$value"; then
            printf '%s' "$value"
            return
        fi

        warn "Discord bot token cannot be empty or a placeholder."
    done
}

prompt_channel_id() {
    local current_value="${1:-}"
    local value

    while true; do
        if is_valid_channel_id "$current_value"; then
            printf 'Discord channel ID [press Enter to keep existing]: ' > /dev/tty
        else
            printf 'Discord channel ID: ' > /dev/tty
        fi

        IFS= read -r value < /dev/tty || fail "Unable to read Discord channel ID"

        if [[ -z "$value" ]]; then
            value="$current_value"
        fi

        if is_valid_channel_id "$value"; then
            printf '%s' "$value"
            return
        fi

        warn "Discord channel ID must be a numeric Discord snowflake."
    done
}

write_env_contents() {
    local token_value="$1"
    local channel_value="$2"

    run_root mkdir -p "$CONFIG_DIR"
    run_root /bin/sh -c "cat > '$ENV_FILE' <<EOF
DISCORD_BOT_TOKEN=${token_value}
DISCORD_CHANNEL_ID=${channel_value}
EOF"
    run_root chmod 600 "$ENV_FILE"
}

write_env_file() {
    local env_token="${DISCORD_BOT_TOKEN:-}"
    local env_channel="${DISCORD_CHANNEL_ID:-}"
    local file_token=""
    local file_channel=""

    if ! is_placeholder_token "$env_token" && is_valid_channel_id "$env_channel"; then
        log "Writing env file from current environment variables"
        write_env_contents "$env_token" "$env_channel"
        return
    fi

    if [[ -f "$ENV_FILE" ]]; then
        file_token="$(extract_env_value_root DISCORD_BOT_TOKEN)"
        file_channel="$(extract_env_value_root DISCORD_CHANNEL_ID)"

        if ! is_placeholder_token "$file_token" && is_valid_channel_id "$file_channel"; then
            log "Using existing env file at $ENV_FILE"
            return
        fi

        warn "Existing env file at $ENV_FILE is incomplete; collecting Discord settings."
    fi

    has_prompt_tty || fail "Missing Discord configuration. Export DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID before running setup, or rerun from an interactive shell."

    show_discord_setup_guide

    if is_placeholder_token "$env_token"; then
        env_token="$file_token"
    fi
    if ! is_valid_channel_id "$env_channel"; then
        env_channel="$file_channel"
    fi

    env_token="$(prompt_bot_token "$env_token")"
    env_channel="$(prompt_channel_id "$env_channel")"

    log "Writing env file to $ENV_FILE"
    write_env_contents "$env_token" "$env_channel"
}

validate_env_file() {
    if ! run_root test -f "$ENV_FILE"; then
        fail "Env file not found at $ENV_FILE"
    fi

    local token_value
    local channel_value

    token_value="$(extract_env_value_root DISCORD_BOT_TOKEN)"
    channel_value="$(extract_env_value_root DISCORD_CHANNEL_ID)"

    if is_placeholder_token "$token_value"; then
        warn "DISCORD_BOT_TOKEN is still a placeholder in $ENV_FILE"
        return 1
    fi

    if ! is_valid_channel_id "$channel_value"; then
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
    require_file "$ENV_TEMPLATE_FILE"

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