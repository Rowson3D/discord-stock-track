#!/usr/bin/env bash
# Update FlipScout on its server: fast-forward to a commit from GitHub, rebuild, restart.
#
#   scripts/update.sh                 update to the newest origin/main
#   scripts/update.sh <commit>        update to that commit (it must already be on origin/main)
#   scripts/update.sh --if-requested  do what the bot's "Update now" button asked, if anything
#   scripts/update.sh --install       run --if-requested every minute from your crontab
#   scripts/update.sh --build-only    rebuild and restart what's checked out (after a manual merge)
#
# Run it as the user that owns this folder (in the docker group), next to docker-compose.yml.
# The bot can only ask: it writes a commit id to data/update-request. This script never runs
# anything from that file; it checks the id is a commit already on origin/main and fast-forwards
# to it (never backwards, never over local commits). The result goes to data/update-result,
# which the restarted bot reports to the owner, and the log to data/update.log.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
DATA=$ROOT/data
BRANCH=${FLIPSCOUT_UPDATE_BRANCH:-main}
REQUEST=$DATA/update-request
RESULT=$DATA/update-result
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

mode=${1:-}
case "$mode" in
  --install)
    line="* * * * * $ROOT/scripts/update.sh --if-requested >> $DATA/update.log 2>&1"
    { crontab -l 2>/dev/null | grep -vF "$ROOT/scripts/update.sh" || true; echo "$line"; } | crontab -
    echo "Installed in your crontab: $line"
    exit 0
    ;;
  --if-requested)
    [ -s "$REQUEST" ] || exit 0
    ;;
esac

mkdir -p "$DATA"
exec 9>"$DATA/update.lock"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) another update is running"
  exit 0
fi

target=""
case "$mode" in
  --if-requested)
    [ -s "$REQUEST" ] || exit 0 # another run took it while we waited
    target=$(head -c 100 "$REQUEST" | tr -d '[:space:]')
    rm -f "$REQUEST"
    ;;
  --build-only | "") ;;
  *) target=$mode ;;
esac

finish() { # finish <ok|failed> <detail>
  {
    echo "status=$1"
    echo "sha=$(git rev-parse HEAD)"
    echo "finished=$(date -u +%FT%TZ)"
    echo "detail=$2"
  } >"$RESULT.tmp"
  mv "$RESULT.tmp" "$RESULT"
  echo "$(date -u +%FT%TZ) $1: $2"
}

if [ -n "$target" ] && ! [[ $target =~ ^[0-9a-f]{40}$ ]]; then
  finish failed "Not a full commit id: ${target:0:60}"
  exit 1
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT

update() {
  if [ "$mode" != "--build-only" ]; then
    git fetch -q origin "$BRANCH" || return 1
    local remote
    remote=$(git rev-parse "origin/$BRANCH") || return 1
    target=${target:-$remote}
    if ! git merge-base --is-ancestor "$target" "$remote"; then
      echo "${target:0:7} isn't on origin/$BRANCH"
      return 1
    fi
    if git merge-base --is-ancestor "$target" HEAD; then
      echo "Already at ${target:0:7} or newer"
    else
      git merge -q --ff-only "$target" || return 1
    fi
  fi
  GIT_SHA=$(git rev-parse HEAD) || return 1
  export GIT_SHA
  docker compose build -q || return 1
  docker compose up -d || return 1
}

before=$(git rev-parse HEAD)
echo "$(date -u +%FT%TZ) updating from ${before:0:7}${target:+ to ${target:0:7}}"
if update >"$log" 2>&1; then
  cat "$log"
  after=$(git rev-parse HEAD)
  if [ "$before" = "$after" ]; then
    finish ok "Rebuilt ${after:0:7}."
  else
    finish ok "$(git log --format='%h %s' "$before..$after" | head -n 10 | paste -sd '|' - | cut -c1-800)"
  fi
else
  cat "$log"
  finish failed "$(tail -n 4 "$log" | paste -sd ' ' - | cut -c1-800)"
  exit 1
fi
