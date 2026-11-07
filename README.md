# Tracker Network Stock

UniFi stock and launch radar with Discord alerts. It tracks the **entire Ubiquiti store** in
every region you choose, records full stock and price history, and pings the right people when
something worth buying appears.

> Building toward a full resale-intelligence platform (web dashboard and P&L ledger next).
> See [docs/ROADMAP.md](docs/ROADMAP.md). Phases 0–3 are done.

## What It Does

- **Full-catalog tracking, no browser.** It reads the UniFi store's embedded structured data over
  plain HTTP: about 840 listings per region in about 10 seconds, every 5 minutes. Each listing
  carries variant-level stock, price, pack size, sold-out time, restock ETA, and coming-soon status.
- **Fast restock detection.** The store's freshly-stocked and what's-new feeds are checked every
  minute and trigger immediate product checks. "Hot" listings are re-checked about every minute:
  watched, high priority, Early Access, coming soon, restock due within 3 days, or recently sold
  out.
- **Launch radar.** Back in stock, sold out, price changes, restock ETAs, Early Access, new
  listings, delisted and relisted. Every event is stored and visible with `/radar`.
- **Multi-region.** `us`, `ca`, `uk`, `eu`. The CA store sells US SKUs in CAD, so the same variant
  is linked across regions.
- **Smart alerts.** Important events go to an alert channel with pings: watchers (optionally only
  below a max price), global subscribers, and an optional role. Routine events go to a radar
  channel, batched into digests. Flapping is suppressed. Alerts survive restarts (durable outbox).
- **Other retailers.** Amazon, Best Buy, B&H, and Newegg URLs can be watched. They're parsed via
  schema.org data with anti-bot detection and automatic backoff. These sites often block bots;
  see [Known Limits](#known-limits).
- **Resale intelligence.** Comps come from eBay's official API (active listings, plus sold
  prices if your app gets Marketplace Insights access) and from manual entries. Each listing title
  is matched against the exact model: no "U7 Pro Max" counted as a U7 Pro, no lots, accessories,
  or for-parts listings. The tracker then estimates net profit after eBay fees, shipping, and tax,
  plus ROI, sell-through, and confidence, ranks everything with `/opportunities`, and fires a
  **deal** alert when an item you can buy right now clears your profit threshold. Back-in-stock
  alerts show the estimated profit.
- **Optional SMS (Twilio)** and automatic cleanup of the bot's old messages.

## Quick Start (Docker)

```bash
git clone <your-repo-url> && cd tracker-network-stock
./scripts/docker_setup.sh          # prompts for token and channel if .env is missing
docker compose logs -f stock-bot
```

Or manually:

```bash
cp tracker-network-stock.env.example .env   # set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID
docker compose up -d --build
```

Data (SQLite database) lives in the `stock-bot-data` volume at `/data`.

### Upgrading From the Old Bot

`git pull && docker compose up -d --build`. On first start, the tracker imports the old
`/data/watchlist.json` and `subscribers.json` automatically (once; a `.legacy-imported` marker is
written):

- UniFi URLs become real per-variant listings.
- Other retailer URLs carry over.
- Per-item watchers become watches; subscribers become global subscriptions.

The old `!` prefix commands are gone; everything is a slash command now. Set `DISCORD_GUILD_ID`
to sync commands instantly and remove the old bot's stale global commands.

Not carried over yet: guarded checkout (returns in Phase 6) and YAML product packs (superseded by
full-catalog tracking; use `/priority` to poll key SKUs faster).

## Discord Setup

1. Create an application and bot at https://discord.com/developers/applications and copy the token.
   No privileged intents are needed.
2. Invite it with the `bot` and `applications.commands` scopes, and these permissions:
   View Channels, Send Messages, Embed Links, Read Message History.
3. With Developer Mode on, copy the alert channel ID (and optionally a radar channel and your
   server ID).

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_CHANNEL_ID=...          # important alerts with pings
DISCORD_RADAR_CHANNEL_ID=...    # optional: routine radar (defaults to the alert channel)
DISCORD_GUILD_ID=...            # optional: instant slash-command sync
DISCORD_ALERT_MENTION=<@&role>  # optional: extra mention on important alerts
```

Without a token, the tracker runs headless and writes events to the log.

## Commands

| Command | What it does |
|---|---|
| `/watch product [region] [max_price]` | Get pinged for back in stock, Early Access, and price drops. `product` autocompletes SKUs and names, or accepts a store URL (UniFi, Amazon, Best Buy, B&H, Newegg). |
| `/unwatch product` | Stop watching. |
| `/watches` | Your watches with live status and price. |
| `/subscribe` / `/unsubscribe` | Get pinged for every important alert. |
| `/stock product` | Status, price, ETA, last in stock, and recent history across regions. |
| `/radar [kind] [limit]` | Recent radar events. |
| `/restocks [days]` | Sold-out items with an announced restock date. |
| `/status` | Health: catalog counts per region, last checks, scheduler jobs, events. |
| `/opportunities [min_profit] [available] [category]` | Resale opportunities ranked by estimated profit. |
| `/value product [refresh]` | Valuation: buy → resell → net, fee breakdown, sold/active stats, recent comps. |
| `/comp product price [shipping] [condition] [quantity] [url] [note]` | *Admin.* Record a sold comp (e.g. your own sale). |
| `/check product` | *Admin.* Check a product right now. |
| `/priority product level` | *Admin.* `high` / `normal` / `low` polling priority. |
| `/testalert [kind] [delay]` | *Admin.* Post a sample alert here (use a delay to test phone push). |
| `/cleanup` | *Admin.* Delete the bot's old messages now. |

Alert messages include **Open** and **Watch** buttons; the Watch button works for anyone and
survives restarts. Admin means Manage Server permission or a user ID in `DISCORD_ADMIN_USER_IDS`.

## Resale Data (eBay)

Valuations work with manual comps alone (`/comp`), but eBay makes them automatic.

1. Create a free account at https://developer.ebay.com and create an application keyset.
2. Use the **Production** keyset: Sandbox only has fake test listings.
3. eBay disables production keysets until you deal with **Marketplace Account Deletion
   notifications**. On the keyset's *Alerts & Notifications* page, turn on **Exempted from
   Marketplace Account Deletion** and choose **"I do not persist eBay data"**. That's accurate
   for this tracker: it uses application tokens only, matches eBay results in memory, and stores
   only aggregate numbers per product (counts, median and percentile prices). It never stores
   listing IDs, titles, URLs, individual prices, or any user data. A database migration also
   purges any eBay rows. Suggested *Additional information*: "Personal, non-commercial
   price-tracking tool. Uses only application (client credentials) tokens with the Browse and
   Marketplace Insights APIs. No eBay user or account data is collected. Listing results are
   processed in memory to compute aggregate price statistics and are not stored."
4. Put the production **App ID** and **Cert ID** in `.env` (never in the tracked
   `.env.example`). The Dev ID isn't needed.

```dotenv
EBAY_CLIENT_ID=YourApp-PRD-xxxxxxxx-xxxxxxxx      # "App ID (Client ID)"
EBAY_CLIENT_SECRET=PRD-xxxxxxxxxxxx-xxxx-xxxx     # "Cert ID (Client Secret)"
```

That gives **active listings** (Browse API). For **sold prices**, apply for eBay's
Marketplace Insights API (limited release). Once approved, the same credentials pick it up
automatically; until then the tracker logs that sold data is unavailable and values items from
the competitive end of active listings. Collection stays well inside eBay's default quota: one
product per minute, with hot items refreshed every 6h, others every 72h, and accessories under
$40 skipped unless watched.

Profit math: `net = resale − (resale × 13.6% + $0.40) − $12 shipping − retail × (1 + tax)`. All
of it is configurable with `TRACKER_MARKET_*` variables.

## Configuration

Everything is set with environment variables. See
[tracker-network-stock.env.example](tracker-network-stock.env.example) for the full annotated list.
Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `UNIFI_STORE_REGIONS` | `us` | Regions to track, e.g. `us,ca` |
| `TRACKER_MONITOR_CATALOG_INTERVAL_SECONDS` | `300` | Full catalog crawl |
| `TRACKER_MONITOR_HOT_POLL_INTERVAL_SECONDS` | `60` | Re-check interval for hot listings |
| `DISCORD_MENTION_KINDS` | `back_in_stock,early_access,new_listing,relisted` | Which events ping |
| `DISCORD_ALERT_COOLDOWN_MINUTES` | `10` | Suppress repeats of the same alert |
| `SMS_ENABLED` + `TWILIO_*` + `SMS_TO_NUMBERS` | off | SMS for back in stock and Early Access |
| `MESSAGE_CLEANUP_ENABLED` | `false` | Delete the bot's own messages after `MESSAGE_CLEANUP_TTL_MINUTES` |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | unset | eBay production keyset for automatic comps |
| `TRACKER_MARKET_DEAL_MIN_PROFIT_CENTS` / `_DEAL_MIN_ROI` | `3000` / `0.15` | Deal alert thresholds |
| `TRACKER_MARKET_PURCHASE_TAX_RATE` | `0` | Sales tax you pay at retail |

Old variable names (`STOCK_BOT_DATA_DIR`, `STOCK_BOT_LOG_*`, `CHECKOUT_ALLOWED_APPROVERS` for
admins, Twilio and cleanup settings) still work.

## CLI

```bash
uv run tracker run                     # the full tracker (bot + scheduler); Ctrl+C to stop
uv run tracker unifi product u7-pro    # parse one product page
uv run tracker unifi crawl [--save]    # crawl a whole region once
uv run tracker radar --kind back_in_stock
uv run tracker opportunities --available  # ranked resale opportunities
uv run tracker market value u7-pro         # valuation + comps (--all shows excluded ones)
uv run tracker market collect u7-pro       # fetch eBay comps now
uv run tracker market comp u7-pro 265 --shipping 10   # record a sold comp
uv run tracker import-legacy           # manual import of old watchlist/subscribers
uv run tracker db upgrade              # apply migrations (also automatic on start)
```

In Docker: `docker compose run --rm stock-bot tracker <command>`.

## Other Deployments

**Local:** install [uv](https://docs.astral.sh/uv/), then `uv sync`, `cp
tracker-network-stock.env.example .env`, and `uv run tracker run`.

**systemd:** run `./setup_linux_service.sh`. It installs with uv (or pip on Python 3.12+), writes
`/etc/tracker-network-stock/bot.env`, and enables `tracker-network-stock.service`. Logs:
`journalctl -u tracker-network-stock.service -f`.

## Development

```bash
uv sync
uv run pytest                          # parsers run against recorded real store pages
uv run ruff check . && uv run ruff format --check .
uv run alembic revision --autogenerate -m "describe change"   # after editing tracker/db/models.py
```

```text
tracker/
├── sources/unifi/    store parser (__NEXT_DATA__), catalog crawler, regions
├── sources/web.py    Amazon / Best Buy / B&H / Newegg (JSON-LD + fallbacks + block detection)
├── services/         UniFi monitor (crawl, feeds, hot polls, delisting), retailer poller
├── ingest.py         snapshots → state, change-only history, radar events
├── outbox.py         durable event dispatch to sinks
├── market/           eBay client, title matching, valuation math, comp collection, deals
├── alerts.py         routing, pings, cooldown
├── bot/              Discord client, slash commands, embeds, buttons, sink, cleanup
├── sms.py            Twilio sink
├── scheduler.py      single-flight jobs with jitter and backoff
├── app.py            wires it all together (`tracker run`)
└── db/               SQLAlchemy models + Alembic migrations
```

## Known Limits

- **Other retailers block plain HTTP clients.** From a residential connection, Amazon serves a
  captcha, Best Buy resets the connection, and Newegg and B&H return 403 (tested 2026-11-07). A
  server IP may fare differently. Blocked listings are flagged and back off automatically; UniFi
  is unaffected.
- **Early Access** shows up only on product pages, so EA detection relies on product polls of new
  and hot items. It hasn't been observed live yet.
- **Australia** and other unsupported regions are served the US store; the tracker rejects them.
- **eBay sold prices** need Marketplace Insights approval. Without it, eBay refuses the token
  scope (HTTP 400) and the tracker logs it once and values items from active asking prices (25th
  percentile), which are rougher, plus manual comps. Verified live 2026-11-07: Browse search for
  U7-Pro returned 100 results; 24 matched, and 65 were correctly rejected as other models (XG, XGS,
  Wall, Max, Outdoor, black variants).
- **eBay listings are never stored**, so individual comps can't be reviewed later. Use
  `tracker market collect <sku>` or `/value <sku> refresh:true` to see the live listings and why
  each was matched or rejected.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "running headless" in logs | `DISCORD_BOT_TOKEN` missing or still `replace-me`. |
| Alerts not posting | Check `DISCORD_CHANNEL_ID` and that the bot can view and send in that channel. |
| Slash commands missing | Global sync can take up to an hour. Set `DISCORD_GUILD_ID` for instant sync. |
| Duplicate old commands | Set `DISCORD_GUILD_ID`; startup clears stale global commands. |
| `asked for the 'xx' store but got 'us'` | That region isn't a separate UniFi store. |
| Retailer listing stuck on unknown | The site is blocking requests; see Known Limits. |
