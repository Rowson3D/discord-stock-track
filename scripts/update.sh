#!/usr/bin/env bash
# Update FlipScout on its server: fast-forward to a commit from GitHub, rebuild, restart, and
# roll back if the new version doesn't come up.
#
#   scripts/update.sh                 update to the newest origin/main
#   scripts/update.sh <commit>        update to that commit (it must already be on origin/main)
#   scripts/update.sh --if-requested  do what the bot's "Update now" button asked, if anything
#   scripts/update.sh --build-only    rebuild and restart what's checked out (after a manual merge)
#   scripts/update.sh --backup        back up the database (cron runs this nightly)
#   scripts/update.sh --install       add both to your crontab
#
# Run it as the user that owns this folder (in the docker group), next to docker-compose.yml.
# The bot can only ask: it writes a commit id to data/update-request. This script never runs
# anything from that file; it checks the id is a commit already on origin/main and fast-forwards
# to it (never backwards, never over local commits).
#
# Each update keeps the running image as <image>:previous, stops the bot, backs up the database
# to data/backups/, starts the new version and waits for Docker to call it healthy (the bot's
# heartbeat). If it never does, the old image, the old database and the old commit go back.
# The result goes to data/update-result, which the bot reports to the owner within a minute,
# and the log to data/update.log.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
DATA=$ROOT/data
DB=$DATA/tracker.db
BACKUPS=$DATA/backups
BRANCH=${FLIPSCOUT_UPDATE_BRANCH:-main}
SERVICE=${FLIPSCOUT_UPDATE_SERVICE:-stock-bot}
WAIT=${FLIPSCOUT_UPDATE_WAIT:-180} # seconds the new version gets to become healthy
REQUEST=$DATA/update-request
RESULT=$DATA/update-result
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

stamp() { date -u +%FT%TZ; }

BACKUP=""
backup() { # backup <kind> <keep>: an online-safe copy of the database, keeping the newest few
  local kind=$1 keep=$2
  BACKUP=""
  if [ ! -f "$DB" ]; then
    echo "No $DB to back up"
    return 0
  fi
  mkdir -p "$BACKUPS"
  local dest
  dest=$BACKUPS/$kind-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD).db
  if command -v sqlite3 >/dev/null; then
    sqlite3 "$DB" ".backup '$dest'" || return 1
  else
    python3 -c 'import sqlite3, sys; s, d = map(sqlite3.connect, sys.argv[1:]); s.backup(d)' \
      "$DB" "$dest" || return 1
  fi
  # shellcheck disable=SC2012 # our own file names: no spaces or newlines
  ls -1t "$BACKUPS/$kind"-*.db | tail -n +$((keep + 1)) | xargs -r rm -f --
  BACKUP=$dest
  echo "Backed up the database to ${dest#"$ROOT"/}"
}

mode=${1:-}
case "$mode" in
  --install)
    lines="* * * * * $ROOT/scripts/update.sh --if-requested >> $DATA/update.log 2>&1
17 4 * * * $ROOT/scripts/update.sh --backup >> $DATA/update.log 2>&1"
    { crontab -l 2>/dev/null | grep -vF "$ROOT/scripts/update.sh" || true; echo "$lines"; } \
      | crontab -
    echo "Installed in your crontab:"
    echo "$lines"
    exit 0
    ;;
  --if-requested)
    [ -s "$REQUEST" ] || exit 0
    ;;
esac

mkdir -p "$DATA"
exec 9>"$DATA/update.lock"
if ! flock -n 9; then
  echo "$(stamp) another update is running"
  exit 0
fi

if [ "$mode" = "--backup" ]; then
  backup nightly 7
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
asked=$target # what to offer as "Try again" if this fails

finish() { # finish <ok|failed> <detail>
  {
    echo "status=$1"
    echo "sha=$(git rev-parse HEAD)"
    echo "target=$asked"
    echo "finished=$(stamp)"
    echo "detail=$2"
  } >"$RESULT.tmp"
  mv "$RESULT.tmp" "$RESULT"
  echo "$(stamp) $1: $2"
}

if [ -n "$target" ] && ! [[ $target =~ ^[0-9a-f]{40}$ ]]; then
  asked=""
  finish failed "Not a full commit id: ${target:0:60}"
  exit 1
fi

image=$(docker compose config --images "$SERVICE" 2>&1 | head -n 1) || true
if [ -z "$image" ] || [[ $image == *" "* ]]; then
  finish failed "Couldn't read the image name from docker-compose.yml: ${image:0:200}"
  exit 1
fi
previous=${image%:*}:previous
version_of() { # the commit an image was built from
  docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | sed -n 's/^FLIPSCOUT_VERSION=//p' | cut -c1-7
}

stopped=""     # set once the running bot is stopped: from then on a failure means rolling back
had_previous="" # set when there was a running image to go back to

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
  docker compose build -q "$SERVICE" || return 1

  # Keep what's running now (by image id: the build just moved the tag) to roll back to.
  local running
  running=$(docker compose ps -q "$SERVICE" 2>/dev/null | head -n 1)
  if [ -n "$running" ]; then
    docker tag "$(docker inspect -f '{{.Image}}' "$running")" "$previous" || return 1
    had_previous=1
  fi
  # Stop first, so nothing writes to the database while it's backed up and the backup is exact.
  docker compose stop "$SERVICE" || return 1
  stopped=1
  backup pre-update 5 || return 1
  docker compose up -d --no-build --wait --wait-timeout "$WAIT" "$SERVICE" || return 1
}

rollback() {
  echo "$(stamp) rolling back"
  echo "--- last log lines of the version that failed:"
  docker compose logs --no-color --tail 30 "$SERVICE" 2>&1 || true
  echo "---"
  docker compose stop "$SERVICE" || true
  if [ -n "$BACKUP" ]; then
    # The failed version may have migrated the database; the old one can't read that.
    rm -f "$DB-wal" "$DB-shm"
    cp "$BACKUP" "$DB.restore" && mv "$DB.restore" "$DB" || return 1
    echo "Restored the database from ${BACKUP#"$ROOT"/}"
  fi
  if [ "$(git rev-parse HEAD)" != "$before" ]; then
    git reset -q --keep "$before" || return 1
  fi
  [ -n "$had_previous" ] || {
    echo "No previous image to go back to"
    return 1
  }
  docker tag "$previous" "$image" || return 1
  docker compose up -d --no-build --wait --wait-timeout "$WAIT" "$SERVICE"
}

log=$(mktemp)
trap 'rm -f "$log"' EXIT

before=$(git rev-parse HEAD)
echo "$(stamp) updating from ${before:0:7}${target:+ to ${target:0:7}}"
if update >"$log" 2>&1; then
  cat "$log"
  after=$(git rev-parse HEAD)
  if [ "$before" = "$after" ]; then
    finish ok "Rebuilt ${after:0:7}."
  else
    finish ok "$(git log --format='%h %s' "$before..$after" | head -n 10 | paste -sd '|' - | cut -c1-800)"
  fi
  exit 0
fi

cat "$log"
why=$(tail -n 3 "$log" | paste -sd ' ' - | cut -c1-500)
if [ -n "$stopped" ]; then
  failed=$(git rev-parse --short HEAD)
  if rollback; then
    back=$(version_of "$image")
    why="$failed didn't come up healthy within ${WAIT}s, so FlipScout went back to ${back:-the previous version} and the database from before the update. $why"
  else
    why="$failed didn't come up and the rollback failed too: the bot may be down. $why"
  fi
elif [ "$(git rev-parse HEAD)" != "$before" ]; then
  git reset -q --keep "$before" || true # the old version is still running; match it
fi
finish failed "$why"
exit 1
