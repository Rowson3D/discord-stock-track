# Updates

FlipScout can tell you when a newer version is on GitHub and update itself when you say so.

## What you see

Every 30 minutes the bot compares the commit it's running with the newest commit on GitHub. When
GitHub is ahead, the owner gets a DM:

- **FlipScout update: N new commits**, newest first, each linked to GitHub.
- **Update now** installs exactly those commits. **See the changes ↗** opens the comparison.

You get one DM per new version, not one every 30 minutes. A minute or two after **Update now**,
the restarted bot DMs **✅ FlipScout updated to abc1234** with what changed. If the update fails,
it DMs **⚠️ FlipScout update failed** and keeps running the old version. Health (in Server settings →
More settings and tools) shows the running version.

## How it works

The bot's container has no access to Docker or the server, by design, so it can only ask:

1. **Update now** writes the commit id to `data/update-request`.
2. On the server, cron runs `scripts/update.sh --if-requested` every minute. It checks that the
   commit is already on GitHub's `main`, fast-forwards to it (never backwards, never over local
   commits), rebuilds the image and restarts the bot. It never runs anything the request says.
3. The script writes `data/update-result` and appends to `data/update.log`. The restarted bot
   reads the result and DMs you.

If nothing picks the request up within 10 minutes, the bot tells you the updater isn't installed.

## Setting it up (once, on the server)

In the folder with `docker-compose.yml`, as the user that runs Docker:

```bash
echo "FLIPSCOUT_UPDATE_REPO=owner/name" >> .env    # the public GitHub repo
scripts/update.sh --install                       # adds the cron line
scripts/update.sh --build-only                    # rebuild once so the bot knows its version
```

The repo must be public: the check uses GitHub's API without a token (60 requests an hour; the bot
uses 2).

## Updating by hand

- `scripts/update.sh`: update to the newest `main`.
- `scripts/update.sh <commit>`: update to a commit that's already on `main`.
- `scripts/update.sh --build-only`: rebuild what's checked out, for example after merging by hand.

Build with the script (or pass `GIT_SHA=$(git rev-parse HEAD)` to `docker compose build`) so the
image knows its commit. Without it, Health shows `unknown` and no update DMs are sent.
