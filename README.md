# Tracker Network Stock Bot

Discord stock-alert bot for monitoring retailer product pages and posting stock changes to a configured Discord channel.

The bot currently supports Ubiquiti, Amazon, B&H Photo, and Newegg. Ubiquiti pages use Playwright Chromium because the store is JavaScript-rendered; other retailers use HTML scraping.

## Features

- Polls configured products on per-retailer intervals.
- Sends Discord embeds when products become available or low stock.
- Preserves last known status when a scraper returns `unknown`, preventing missed back-in-stock transitions caused by transient page or browser failures.
- Persists watchlist data across restarts.
- Includes Docker Compose, systemd, and local development workflows.
- Includes a triage script for scraper/browser/alert-state verification.

## Supported Retailers

| Retailer | Site Key | Default Interval | Notes |
|---|---:|---:|---|
| Ubiquiti Store | `ui.com` | 60s | Uses Playwright Chromium with HTTP fallback. |
| Amazon | `amazon.com` | 300s | Higher interval recommended because Amazon blocks aggressive scraping. |
| B&H Photo | `bhphotovideo.com` | 60s | HTML scraper. |
| Newegg | `newegg.com` | 60s | HTML scraper. |

## Requirements

For Docker deployment:

- Linux host
- Docker Engine
- Docker Compose plugin (`docker compose version`)

For local Python or systemd deployment:

- Python 3.12 recommended
- `pip`
- Playwright Chromium (`playwright install chromium`; use `--with-deps` on Linux)

## Discord Setup

1. Open https://discord.com/developers/applications.
2. Create an application and add a bot.
3. Copy the bot token.
4. Enable **Message Content Intent** under the bot settings.
5. Invite the bot with these permissions: `View Channels`, `Send Messages`, `Embed Links`.
6. Enable Discord Developer Mode and copy the target channel ID.

## Configuration

Create a `.env` file from the example:

```bash
cp tracker-network-stock.env.example .env
nano .env
```

Required variables:

```dotenv
DISCORD_BOT_TOKEN=your-token
DISCORD_CHANNEL_ID=123456789012345678
```

Optional variables:

```dotenv
STOCK_BOT_DATA_DIR=/data
STOCK_BOT_WATCHLIST_FILE=/data/watchlist.json
```

Runtime settings such as retailer intervals, alert toggles, low-stock threshold, and default products live in [config.py](config.py).

Product packs live under [config/products](config/products). The included `unifi_msp` pack adds core UniFi gear commonly sourced by MSPs and installers.

## Quick Start: Docker on Linux

Fresh clone:

```bash
git clone <your-repo-url>
cd tracker-network-stock
./scripts/docker_setup.sh
```

The setup script checks Docker, prompts for Discord settings if `.env` is missing, builds the image, starts the bot, and runs triage.

Non-interactive setup:

```bash
export DISCORD_BOT_TOKEN="your-token"
export DISCORD_CHANNEL_ID="123456789012345678"
./scripts/docker_setup.sh
```

Manual Compose setup:

```bash
cp tracker-network-stock.env.example .env
nano .env
docker compose up -d --build
docker compose logs -f stock-bot
```

The Docker image installs Playwright Chromium at build time. Runtime data is stored in the `stock-bot-data` Docker volume mounted at `/data`.

## Updating a Docker Deployment

On the deployed Linux host:

```bash
cd tracker-network-stock
git pull
docker compose up -d --build
docker compose logs -f stock-bot
```

Run triage after update:

```bash
docker compose run --rm stock-bot python scripts/triage_stock.py
```

One-command update using the setup script:

```bash
cd tracker-network-stock
git pull
./scripts/docker_setup.sh
```

## Docker Commands

Build image only:

```bash
docker build -t tracker-network-stock:latest .
```

Start service:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f stock-bot
```

Stop service:

```bash
docker compose down
```

Run without Compose:

```bash
docker volume create tracker-network-stock-data
docker run -d \
  --name tracker-network-stock \
  --restart unless-stopped \
  --env-file .env \
  -e STOCK_BOT_DATA_DIR=/data \
  -v tracker-network-stock-data:/data \
  tracker-network-stock:latest
```

## Local Development

Create environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

On Linux, install browser dependencies too:

```bash
playwright install --with-deps chromium
```

Run locally:

```bash
cp tracker-network-stock.env.example .env
nano .env
python bot.py
```

Windows PowerShell activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
Copy-Item tracker-network-stock.env.example .env
python bot.py
```

## Linux systemd Deployment

Docker is the recommended deployment path. Use systemd when you need a host-managed Python service instead.

Fast setup:

```bash
git clone <your-repo-url>
cd tracker-network-stock
./setup_linux_service.sh
```

Non-interactive setup:

```bash
export DISCORD_BOT_TOKEN="your-token"
export DISCORD_CHANNEL_ID="123456789012345678"
./setup_linux_service.sh
```

Restart after config changes:

```bash
sudo systemctl restart tracker-network-stock.service
sudo systemctl status tracker-network-stock.service
```

Logs:

```bash
journalctl -u tracker-network-stock.service -n 100 --no-pager
```

The example unit is [stock-bot.service.example](stock-bot.service.example).

## Discord Commands

| Command | Description |
|---|---|
| `!watch <url>` | Add a supported product URL to the watchlist. |
| `!unwatch <index\|url>` | Remove a watch by list number or exact URL. |
| `!unwatch_pack <pack_id>` | Remove all entries added from a product pack. |
| `!remove_sku <sku>` | Remove all watch entries for a SKU. |
| `!clear_watches confirm` | Clear the entire watchlist. |
| `!list` | Show monitored products in paged embeds with index numbers. |
| `!packs` | Show available product packs. |
| `!watch_pack <pack_id>` | Add a product pack to the watchlist. |
| `!report` | Show current stock summary by status. |
| `!check` | Force an immediate stock check. |
| `!test_alert [status]` | Send a simulated alert embed after 15 seconds without changing watchlist state. |
| `!help_stock` | Show bot command help. |

Examples:

```text
!watch https://store.ui.com/us/en/products/utr
!watch https://www.amazon.com/dp/B0XXXXXXXX
!packs
!watch_pack unifi_msp
!report
!list
!unwatch 3
!remove_sku UDM-SE
!check
!test_alert in_stock
```

## MSP Product Packs

Product packs turn the bot from a URL watcher into a procurement monitor. A pack defines SKUs, categories, priority, and vendor URLs. Adding a pack creates one watchlist entry per vendor URL.

Included pack:

| Pack ID | Description |
|---|---|
| `unifi_msp` | Core UniFi gateways, switches, access points, cameras, and NVRs for MSP sourcing. |

Add the pack:

```text
!watch_pack unifi_msp
```

Remove entries later:

```text
!list
!unwatch 2
!remove_sku UDM-SE
!unwatch_pack unifi_msp
!clear_watches confirm
```

Run a live scrape and then show a summary:

```text
!check
!report
```

Each pack product supports this shape:

```yaml
- sku: UDM-SE
  name: UniFi Dream Machine Special Edition
  category: Gateways
  priority: high
  vendors:
    - name: Ubiquiti Store
      site: ui.com
      url: https://store.ui.com/us/en/products/udm-se
```

## Triage and Verification

Run on host Python environment:

```bash
python scripts/triage_stock.py
python scripts/triage_stock.py --old-status out_of_stock --simulate-new-status in_stock
```

Run inside Docker:

```bash
docker compose run --rm stock-bot python scripts/triage_stock.py
docker compose run --rm stock-bot python scripts/triage_stock.py --old-status out_of_stock --simulate-new-status in_stock
```

The triage report includes:

- detected site key
- Playwright install and launch status for Ubiquiti
- current scrape result
- simulated alert decision for status transitions

Expected alert simulation for an available product:

```json
{
  "old_status": "out_of_stock",
  "new_status": "in_stock",
  "would_send_discord_alert": true
}
```

Test the Discord UI from the target channel:

```text
!test_alert in_stock
!test_alert low_stock
!test_alert out_of_stock
```

The test command waits 15 seconds before sending the embed, so you can leave the channel and verify notification behavior. It sends the same embed format as a real alert and does not update `last_status`.

## Data and Persistence

Default data locations:

| Deployment | Watchlist Location |
|---|---|
| Docker | `/data/watchlist.json` in `stock-bot-data` volume |
| Local development | `data/watchlist.json` |
| systemd | `/var/lib/tracker-network-stock/watchlist.json` unless overridden |

The watchlist file is written atomically through a temporary file and then replaced.

## Project Structure

```text
tracker-network-stock/
|-- bot.py                         # Discord bot entry point and commands
|-- config.py                      # Environment loading and runtime configuration
|-- monitor.py                     # Polling loop, state machine, and Discord alerts
|-- scrapers.py                    # Retailer-specific stock scrapers
|-- scripts/
|   |-- docker_setup.sh            # Docker clone-to-run setup helper
|   `-- triage_stock.py            # Scraper and alert-state triage tool
|-- config/products/               # Product pack YAML files
|-- Dockerfile                     # Production container image
|-- docker-compose.yml             # Docker Compose service definition
|-- setup_linux_service.sh         # systemd setup helper
|-- stock-bot.service.example      # Example systemd unit
|-- tracker-network-stock.env.example
`-- requirements.txt
```

## Security Notes

- Never commit `.env` or real Discord bot tokens.
- Rotate any token that was pasted into chat, committed, or shared outside the deployment host.
- Keep production secrets in `.env`, shell environment variables, or `/etc/tracker-network-stock/bot.env` for systemd.
- The Docker container runs as non-root user `stockbot`.
- `.gitignore` and `.dockerignore` exclude local env files, virtual environments, logs, and runtime data.

## Troubleshooting

Docker service status:

```bash
docker compose ps
docker compose logs -f stock-bot
```

Container triage:

```bash
docker compose run --rm stock-bot python scripts/triage_stock.py
```

systemd status:

```bash
sudo systemctl status tracker-network-stock.service
journalctl -u tracker-network-stock.service -n 100 --no-pager
```

Common failures:

| Symptom | Likely Cause | Fix |
|---|---|---|
| Bot exits immediately | Missing or placeholder Discord token/channel | Check `.env` or systemd env file. |
| No Ubiquiti status | Playwright Chromium missing or cannot launch | Docker: rebuild image. Local/systemd: run `playwright install --with-deps chromium`. |
| No Discord alerts | Bot lacks channel permission or wrong channel ID | Reinvite bot or update `DISCORD_CHANNEL_ID`. |
| Repeated `unknown` status | Retailer page changed, network block, or anti-bot response | Run triage and review scraper output/logs. |
| `bad-setting` in systemd | Malformed unit file | Compare against [stock-bot.service.example](stock-bot.service.example). |

## Maintenance

- Prefer Docker for production deployments to avoid host Python and browser drift.
- After code updates, run `docker compose up -d --build` and then triage.
- Increase retailer intervals if rate limits or anti-bot pages appear.
- Keep dependencies current in [requirements.txt](requirements.txt) and rebuild Docker after changes.
