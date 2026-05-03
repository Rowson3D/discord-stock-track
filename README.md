# 📦 Stock Alert Discord Bot

Monitors Ubiquiti, Amazon, B&H Photo, and Newegg for stock changes and sends
alerts to a private Discord channel.

---

## 🚀 Setup

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

On Linux, prefer:
```bash
playwright install --with-deps chromium
```

> Playwright + Chromium is required for Ubiquiti (ui.com), which is a
> JavaScript-rendered SPA. All other sites use lightweight HTML scraping.

---

### 2. Create your Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Add Bot**
4. Under **Token**, click **Reset Token** and copy it
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Messages/View Channels`, `Embed Links`
7. Copy the generated URL and open it in your browser to invite the bot to your server

---

### 3. Get your Discord Channel ID

1. In Discord, go to **User Settings → Advanced** and enable **Developer Mode**
2. Right-click the channel you want alerts in → **Copy Channel ID**

---

### 4. Configure the bot

For local testing, copy the example env file to `.env` in the repo root and fill in your real values:
```bash
cp tracker-network-stock.env.example .env
```

On Windows PowerShell:
```powershell
Copy-Item tracker-network-stock.env.example .env
```

The app now loads `.env` automatically. You can still use exported environment variables instead:
```bash
export DISCORD_BOT_TOKEN="your-token"
export DISCORD_CHANNEL_ID="123456789012345678"
```

Optional deployment variables:
```bash
export STOCK_BOT_DATA_DIR="/var/lib/tracker-network-stock"
export STOCK_BOT_WATCHLIST_FILE="/var/lib/tracker-network-stock/watchlist.json"
```

`config.py` still contains your default intervals, alert toggles, and seeded products.

---

### 5. Run the bot
```bash
python bot.py
```

The bot now refuses to start if the Discord token is still the placeholder value.

---

## Linux Service

Use the included [stock-bot.service.example](stock-bot.service.example) as a starting point.

Fastest path on Linux:
```bash
chmod +x ./setup_linux_service.sh
./setup_linux_service.sh
```

If your token and channel are already in the shell environment, the script will write `/etc/tracker-network-stock/bot.env` for you. If they are not set yet, the script now prompts for them and validates that they are not placeholders before starting the service:
```bash
export DISCORD_BOT_TOKEN="your-token"
export DISCORD_CHANNEL_ID="123456789012345678"
./setup_linux_service.sh
```

You can override defaults when needed:
```bash
RUN_USER=jarrod RUN_GROUP=jarrod REPO_DIR="$PWD" ./setup_linux_service.sh
```

Typical setup on a Linux machine:
```bash
git clone <your-repo-url>
cd tracker-network-stock
chmod +x ./setup_linux_service.sh
./setup_linux_service.sh
```

The script still cannot automate Discord itself. Before you run it, make sure you have:
- created the bot application
- enabled `Message Content Intent`
- invited it with `View Channels`, `Send Messages`, and `Embed Links`
- copied the bot token and target channel ID

After editing `/etc/tracker-network-stock/bot.env`, restart with:
```bash
sudo systemctl restart tracker-network-stock.service
sudo systemctl status tracker-network-stock.service
```

Troubleshooting:
```bash
sudo systemctl status tracker-network-stock.service
journalctl -u tracker-network-stock.service -n 100 --no-pager
sudo sed -n '1,40p' /etc/systemd/system/tracker-network-stock.service
sudo sed -n '1,10p' /etc/tracker-network-stock/bot.env
./.venv/bin/python bot.py
```

Stock triage for the default travel router:
```bash
python scripts/triage_stock.py
python scripts/triage_stock.py --old-status out_of_stock --simulate-new-status in_stock
```

The triage output shows whether Playwright Chromium is installed, the current scraper result, and whether the monitor would send a Discord alert for the tested transition.

Common failure patterns:
- `bad-setting`: the unit file is malformed or still contains env-file contents instead of `[Unit]` / `[Service]` sections.
- Immediate exit on startup: `DISCORD_BOT_TOKEN` or `DISCORD_CHANNEL_ID` is missing or still a placeholder.
- `ExecStart` errors: the repo path or `.venv` path in the systemd unit does not match the actual Linux location.
- Playwright launch failures: rerun `./.venv/bin/playwright install --with-deps chromium`.

The service is configured to restart automatically after failures and on machine reboot.

---

## Docker on Linux

Fresh clone quick start:
```bash
git clone <your-repo-url>
cd tracker-network-stock
./scripts/docker_setup.sh
```

If running non-interactively:
```bash
export DISCORD_BOT_TOKEN="your-token"
export DISCORD_CHANNEL_ID="123456789012345678"
./scripts/docker_setup.sh
```

Build image:
```bash
docker build -t tracker-network-stock:latest .
```

Create env file:
```bash
cp tracker-network-stock.env.example .env
nano .env
```

Run with Docker Compose:
```bash
docker compose up -d --build
docker compose logs -f stock-bot
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

Triage inside container:
```bash
docker compose run --rm stock-bot python scripts/triage_stock.py
docker compose run --rm stock-bot python scripts/triage_stock.py --old-status out_of_stock --simulate-new-status in_stock
```

Stop/remove:
```bash
docker compose down
```

The image installs Playwright Chromium during build, so Ubiquiti scraping works without a separate browser install on the host. Runtime watchlist data lives in the Docker volume mounted at `/data`.

---

## Git Workflow

This repo is safe to push as long as you keep secrets in `/etc/tracker-network-stock/bot.env` or shell environment variables and do not commit local runtime data.

Files intentionally not tracked:
- `.venv/`
- `data/`
- `watchlist.json`
- local `.env` files

Typical first commit:
```bash
git init
git add .
git commit -m "Initial stock bot setup"
```

If you are pushing to GitHub:
```bash
git remote add origin <your-repo-url>
git branch -M main
git push -u origin main
```

---

## VS Code Remote SSH

Recommended extensions for this repo are listed in `.vscode/extensions.json`.

Typical workflow:
1. Push this repo to your git remote from your local machine.
2. SSH into the Linux machine once and clone the repo there.
3. In VS Code, use `Remote-SSH: Connect to Host...`.
4. Open the cloned folder on the Linux machine.
5. Use the integrated terminal in that remote window for setup and service management.

Typical Linux-side clone and setup:
```bash
git clone <your-repo-url>
cd tracker-network-stock
chmod +x ./setup_linux_service.sh
./setup_linux_service.sh
```

Once connected over Remote SSH, all terminals, Python execution, and file edits happen directly on the Linux host.

---

## 💬 Discord Commands

| Command | Description |
|---|---|
| `!watch <url>` | Add a product URL to monitor |
| `!unwatch <url>` | Remove a product from monitoring |
| `!list` | Show all monitored products and their current status |
| `!check` | Force an immediate check right now |
| `!help_stock` | Show command help |

### Example
```
!watch https://www.amazon.com/dp/B0XXXXXXXX
!watch https://www.newegg.com/p/XXXXXXXX
!list
!check
```

---

## ⚙️ Check Intervals (config.py)

| Site | Default Interval | Notes |
|---|---|---|
| ui.com | 60s | Uses headless browser |
| amazon.com | 300s | **Recommended 5 min+** — Amazon blocks aggressive scrapers |
| bhphotovideo.com | 60s | Safe to poll frequently |
| newegg.com | 60s | Safe to poll frequently |

---

## 📁 Files

```
stock-bot/
├── bot.py            # Discord bot + commands
├── monitor.py        # Polling loop + alert logic
├── scrapers.py       # Per-site scraping logic
├── config.py         # ← Edit this with your token + channel ID
├── watchlist.json    # Auto-created; persists your product list
└── requirements.txt
```

---

## ⚠️ Notes

- **Amazon**: The bot uses realistic browser headers but Amazon's anti-bot
  detection is aggressive. If you get blocked, increase the interval in
  `config.py` or consider using a proxy.
- **Ubiquiti**: Uses Playwright (headless Chromium) since ui.com is a
  React app. First run may be slower while Chromium launches.
- The watchlist is now stored under `data/watchlist.json` by default, or the path set by `STOCK_BOT_DATA_DIR` / `STOCK_BOT_WATCHLIST_FILE`.
- The monitor loop starts once during bot setup, avoiding duplicate polling tasks after Discord reconnects.
