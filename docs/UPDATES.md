# Updates

FlipScout can tell you when a newer version is on GitHub and update itself when you say so.

## What you see

Every 30 minutes the bot compares the commit it's running with the newest commit on GitHub. When
GitHub is ahead, the owner gets a DM:

- **FlipScout update: N new commits**, newest first, each linked to GitHub.
- **Update now** installs exactly those commits. **See the changes ↗** opens the comparison.

You get one DM per new version, not one every 30 minutes. A few minutes after **Update now**, the
bot DMs **✅ FlipScout updated to abc1234** with what changed. If the update fails, it DMs
**⚠️ FlipScout update failed** with why, says which version is still running, and offers
**Try again**.

Health (in Server settings → More settings and tools) shows the running version. For the owner, it
also shows **Update to abc1234** whenever GitHub is ahead, in case the DM is gone.

## How it works

The bot's container has no access to Docker or the server, by design, so it can only ask:

1. **Update now** writes the commit id to `data/update-request`.
2. On the server, cron runs `scripts/update.sh --if-requested` every minute. It checks that the
   commit is already on GitHub's `main`, fast-forwards to it (never backwards, never over local
   commits) and builds the new image. It never runs anything the request says.
3. It tags the running image `<image>:previous`, stops the bot, backs up the database to
   `data/backups/pre-update-*.db` (the newest 5 are kept), starts the new version and waits up
   to 3 minutes for Docker to call it healthy.
4. If it never becomes healthy, the script puts everything back: the previous image, the database
   from the backup (the new version may have migrated it, and the old one can't read that) and
   the previous commit. If the build itself fails, the old version never stopped.
5. The script writes `data/update-result` and appends to `data/update.log`. The bot checks for
   the result every minute and DMs you.

"Healthy" means the bot is connected to Discord and its database answers: every 20 seconds it
touches `/tmp/flipscout-heartbeat` inside the container, and Docker's health check fails once that
file is 90 seconds old. `docker ps` shows it.

If nothing picks the request up within 10 minutes, the bot tells you the updater isn't installed.

## Setting it up (once, on the server)

In the folder with `docker-compose.yml`, as the user that runs Docker:

```bash
echo "FLIPSCOUT_UPDATE_REPO=owner/name" >> .env    # the public GitHub repo
scripts/update.sh --install                       # adds the cron lines
scripts/update.sh --build-only                    # rebuild once so the bot knows its version
```

The repo must be public: the check uses GitHub's API without a token (60 requests an hour; the bot
uses 2).

## Updating by hand

- `scripts/update.sh`: update to the newest `main`.
- `scripts/update.sh <commit>`: update to a commit that's already on `main`.
- `scripts/update.sh --build-only`: rebuild what's checked out, for example after merging by hand.
  It waits for health and rolls back the same way.
- `scripts/update.sh --backup`: back up the database now. Cron runs it nightly at 04:17 (server time) and
  keeps 7 (`data/backups/nightly-*.db`).

To restore a backup by hand: `docker compose stop`, copy it over `data/tracker.db`, delete
`data/tracker.db-wal` and `data/tracker.db-shm`, then `docker compose up -d`.

Build with the script (or pass `GIT_SHA=$(git rev-parse HEAD)` to `docker compose build`) so the
image knows its commit. Without it, Health shows `unknown` and no update DMs are sent.
