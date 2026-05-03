#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="stock-bot"
ENV_FILE=".env"
ENV_TEMPLATE="tracker-network-stock.env.example"

log() {
    printf '[docker-setup] %s\n' "$1"
}

warn() {
    printf '[warn] %s\n' "$1" >&2
}

fail() {
    printf '[error] %s\n' "$1" >&2
    exit 1
}

is_placeholder_token() {
    local value="${1:-}"
    [[ -z "$value" || "$value" == "replace-me" || "$value" == "YOUR_BOT_TOKEN_HERE" ]]
}

is_valid_channel_id() {
    local value="${1:-}"
    [[ "$value" =~ ^[0-9]{17,20}$ ]] && [[ "$value" != "123456789012345678" ]]
}

get_env_value() {
    local key="$1"
    if [[ -f "$ENV_FILE" ]]; then
        grep -E "^${key}=" "$ENV_FILE" | head -n 1 | cut -d= -f2- || true
    fi
}

write_env_file() {
    local token_value="$1"
    local channel_value="$2"

    cat > "$ENV_FILE" <<EOF
DISCORD_BOT_TOKEN=${token_value}
DISCORD_CHANNEL_ID=${channel_value}
EOF
    chmod 600 "$ENV_FILE"
}

prompt_secret() {
    local prompt="$1"
    local value=""
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r -s value < /dev/tty || fail "Unable to read value"
    printf '\n' > /dev/tty
    printf '%s' "$value"
}

prompt_text() {
    local prompt="$1"
    local value=""
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r value < /dev/tty || fail "Unable to read value"
    printf '%s' "$value"
}

ensure_env_file() {
    local token_value="${DISCORD_BOT_TOKEN:-$(get_env_value DISCORD_BOT_TOKEN)}"
    local channel_value="${DISCORD_CHANNEL_ID:-$(get_env_value DISCORD_CHANNEL_ID)}"

    if ! is_placeholder_token "$token_value" && is_valid_channel_id "$channel_value"; then
        log "Using existing Discord config"
        write_env_file "$token_value" "$channel_value"
        return
    fi

    [[ -r /dev/tty && -w /dev/tty ]] || fail "Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID, then rerun. No TTY available for prompts."

    warn "Discord config missing or placeholder. Prompting now."
    while is_placeholder_token "$token_value"; do
        token_value="$(prompt_secret 'Discord bot token: ')"
    done

    while ! is_valid_channel_id "$channel_value"; do
        channel_value="$(prompt_text 'Discord channel ID: ')"
    done

    write_env_file "$token_value" "$channel_value"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing command: $1"
}

main() {
    [[ -f "docker-compose.yml" ]] || fail "Run this from repo root after clone."
    [[ -f "$ENV_TEMPLATE" ]] || fail "Missing $ENV_TEMPLATE"

    require_command docker
    docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin missing. Install docker compose plugin."

    ensure_env_file

    log "Building and starting container"
    docker compose up -d --build

    log "Running stock triage"
    docker compose run --rm "$SERVICE_NAME" python scripts/triage_stock.py

    log "Setup complete"
    log "Logs: docker compose logs -f $SERVICE_NAME"
}

main "$@"
