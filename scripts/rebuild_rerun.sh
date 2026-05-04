#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="stock-bot"
GPU_TRIAGE_URL="https://www.bestbuy.com/site/nvidia-geforce-rtx-4090-24gb-gddr6x-graphics-card-titanium-and-black/6521430.p?skuId=6521430"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() {
    printf '[rebuild-rerun] %s\n' "$1"
}

fail() {
    printf '[error] %s\n' "$1" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: ./scripts/rebuild_rerun.sh [command]

Commands:
  rebuild       Rebuild image, recreate container, run default triage (default)
  restart       Restart existing container without rebuild
  logs          Follow stock-bot logs
  triage        Run default stock triage in container
  gpu-triage    Run RTX 4090 Best Buy triage in container
  ps            Show compose service status
  stop          Stop and remove compose containers

Examples:
  ./scripts/rebuild_rerun.sh
  ./scripts/rebuild_rerun.sh rebuild
  ./scripts/rebuild_rerun.sh gpu-triage
  ./scripts/rebuild_rerun.sh logs
EOF
}

require_docker_compose() {
    command -v docker >/dev/null 2>&1 || fail "Missing command: docker"
    docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin missing. Install docker compose plugin."
}

require_env_file() {
    [[ -f ".env" ]] || fail "Missing .env. Run ./scripts/docker_setup.sh first or copy tracker-network-stock.env.example to .env."
}

run_rebuild() {
    require_env_file
    log "Rebuilding image and starting container"
    docker compose up -d --build

    log "Container status"
    docker compose ps

    log "Running default triage"
    docker compose run --rm "$SERVICE_NAME" python scripts/triage_stock.py

    log "Done. Follow logs with: ./scripts/rebuild_rerun.sh logs"
}

main() {
    cd "$REPO_ROOT"
    [[ -f "docker-compose.yml" ]] || fail "Run from repo with docker-compose.yml"
    require_docker_compose

    local command_name="${1:-rebuild}"
    case "$command_name" in
        rebuild)
            run_rebuild
            ;;
        restart)
            require_env_file
            log "Restarting container"
            docker compose restart "$SERVICE_NAME"
            docker compose ps
            ;;
        logs)
            docker compose logs -f "$SERVICE_NAME"
            ;;
        triage)
            require_env_file
            docker compose run --rm "$SERVICE_NAME" python scripts/triage_stock.py
            ;;
        gpu-triage)
            require_env_file
            docker compose run --rm "$SERVICE_NAME" python scripts/triage_stock.py "$GPU_TRIAGE_URL" --old-status out_of_stock
            ;;
        ps)
            docker compose ps
            ;;
        stop)
            docker compose down
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            usage
            fail "Unknown command: $command_name"
            ;;
    esac
}

main "$@"