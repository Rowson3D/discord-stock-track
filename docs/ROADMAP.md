# Roadmap: From Stock Bot to Resale Intelligence Platform

**Goal:** turn the UniFi stock-alert bot into a resale intelligence tool. It should know *when* something is buyable, *whether* it's worth flipping, and *how* you actually did.

**Decisions locked in (2026-11-07):**
- Core purpose: resale intel + alerts. Track retail stock *and* resale market prices, compute net profit, rank by opportunity.
- Interface: Discord for real-time alerts and quick actions, plus a local web dashboard for analysis and management.
- Scope: deep on UniFi, with a plugin system so other retailers and categories can drop in.
- Extras in scope: Early Access/launch radar, price and deal alerts, guarded add-to-cart, inventory and P&L ledger.

---

## 1. Where the current code stands

About 3.7k lines across `bot.py`, `monitor.py`, `scrapers.py`, `checkout.py`, and `config.py`. It works, but it has hit its ceiling.

### Structural problems
| Problem | Impact |
|---|---|
| UniFi checks launch a **full Chromium per product, sequentially** | 15 high-priority UniFi watches at 30s intervals can't keep up. Slow and memory-heavy. |
| Stock detection is **text matching on the whole page** ("add to cart" anywhere means in stock) | False positives from related products, footers, and upsells. The price regex grabs the first `$` on the page. |
| Persistence is a **single JSON file rewritten after every check** | No history at all: no price or stock time series, so nothing to analyze. |
| `monitor.py` mixes logging setup, persistence, embeds, polling, SMS, and checkout | Hard to change one thing without breaking another. No tests. |
| Duplicate `!prefix` and `/slash` command sets | Help text says `/unwatch`, but the command is `/delete`. |
| Zero resale-market awareness | The tool can't answer its original question: what's worth reselling? |

### Specific bugs found
- `load_watchlist()` re-merges `default_products` on every startup, so you can never permanently remove the UTR.
- `_short_url()` uses `lstrip("www.")`, which strips *characters*, not the prefix.
- `detect_site()` uses substring matching (`"ui.com" in host`), so lookalike hosts match.
- Alert cooldown and backoff state live in memory, so they reset on restart.
- Logging is configured as an import side effect of `monitor.py`.
- in_stock ↔ low_stock flapping can re-alert after each cooldown.

### Key discovery: UniFi exposes structured data
`store.ui.com` product pages embed `__NEXT_DATA__` JSON, fetchable with a plain HTTP GET (verified: 200 OK, no browser). Each **variant** carries:
`sku`, `status` (`Available` / sold out / …), `isEarlyAccess`, `soldOutAt`, `restockEtaAt`, `isVisibleInStore`, and an exact price in cents.

So for UniFi:
- **No Playwright needed for monitoring.** Requests are ~50x lighter, and we can poll faster, politely.
- Variant-level accuracy (single vs 5-pack).
- Early Access, restock ETAs, and sold-out timestamps come for free, and they're the core of the launch radar.
- Collection and category pages expose product lists, which enables **full-catalog discovery** without hand-maintained YAML.

---

## 2. Target architecture

```
                ┌──────────────────────── one process, one asyncio loop ───────────────────────┐
                │                                                                              │
 Retail sources │  Scheduler (heap by next-due, per-host concurrency + rate limits, backoff)   │
  ui.com (JSON) ├─►  Observations ──► SQLite (history) ──► Engines ──► Alert router ──┬─► Discord bot
  amazon, bby…  │                                           │  profit / opportunity   ├─► SMS (Twilio)
                │                                           │  launch radar / deals   └─► Dashboard feed
 Market sources │  Comp collector ──► market_comps ────────►┘                                   │
  eBay, r/HLS,  │                                                                              │
  manual        │  FastAPI web dashboard (same DB, same loop)  ◄── you                         │
                └──────────────────────────────────────────────────────────────────────────────┘
```

### Stack
| Concern | Choice | Why |
|---|---|---|
| Runtime | Python 3.12, fully `async` | One event loop shared by the bot, scheduler, and dashboard |
| HTTP | `httpx.AsyncClient` (HTTP/2) | Connection reuse, async, timeouts |
| Browser | Playwright, **only** for checkout and stubborn retailers; one persistent context | Not launched per check |
| Storage | SQLite + SQLAlchemy 2.0 (async) + Alembic | Time-series history, zero-ops, easy backup |
| Config | `pydantic-settings` | Typed, validated env config, one place |
| Discord | discord.py 2.x, **slash commands only**, cogs, buttons, autocomplete | Modern UX, no duplicate command sets |
| Dashboard | FastAPI + Jinja + HTMX + Chart.js | Single container, no JS build step, fast to iterate |
| Quality | pytest with recorded fixtures, ruff, GitHub Actions | Parsers tested against real saved pages |
| Deploy | Docker Compose; slim image by default, Chromium only with the `checkout` extra | Smaller image, faster rebuilds |

### Package layout (proposed)
```
tracker/
  settings.py            # pydantic-settings
  db/                    # models, session, alembic migrations
  sources/
    base.py              # RetailSource / MarketSource protocols
    unifi/               # __NEXT_DATA__ parser, catalog crawler, regions
    amazon.py bestbuy.py newegg.py bhphoto.py   # ported behind the interface
    market/ ebay.py homelabsales.py manual.py
  scheduler.py           # priority heap, rate limits, adaptive intervals
  engines/
    profit.py            # fees, tax, shipping → net profit / ROI
    opportunity.py       # scoring
    radar.py             # new SKU / EA / restock-ETA / sold-out events
    rules.py             # alert rule evaluation + dedupe
  alerts/ router.py discord.py sms.py
  bot/ cogs/ (watch, market, radar, ledger, admin, checkout)
  web/ app.py routes/ templates/ static/
  checkout/ unifi.py     # guarded, never places orders
  ledger/ service.py     # FIFO cost basis, P&L
  main.py                # starts bot + scheduler + web in one loop
tests/ fixtures/ (saved HTML/JSON per source)
```

### Data model (core tables)
- `products`: canonical item (brand, model, category, MSRP)
- `variants`: SKU-level (UniFi `U7-Pro-US` vs `U7-Pro-5-US`), pack size, EA flag
- `listings`: a variant at a retailer and region (URL, source key)
- `stock_observations`: time series: status, qty, price, EA, restock ETA. Written **on change**, plus a periodic heartbeat.
- `market_comps`: resale data points (source, sold/active, price, shipping, condition, date, match confidence)
- `opportunities`: computed snapshot per variant (net profit, ROI, sell-through, score)
- `events`: radar feed (new SKU, EA drop, restock ETA set, sold out, price change)
- `watches` and `alert_rules`: who wants what, under which conditions, routed where
- `alerts_sent`: persistent dedupe and cooldown (survives restarts)
- `lots` and `sales`: the ledger

---

## 3. Feature design

### 3.1 Resale intelligence (the core)
- **Comp collection per variant.** eBay sold and active listings, r/homelabsales `[FS]` posts, and manual comps you enter.
- **Matching and cleaning.** Normalize titles. Exclude "for parts", lots, bundles, and mismatched pack sizes. Each comp gets a confidence score, and low-confidence comps are shown but not counted.
- **Profit engine:**
  `net = resale_median × (1 − platform_fee%) − fixed_fee − outbound_ship − (retail + sales_tax + inbound_ship)`
  Fee, tax, and shipping profiles are configurable per platform.
- **Opportunity score:** net margin, ROI, sell-through (sold vs active over 30/90 days), velocity, and scarcity (sold-out frequency, EA, time-to-sellout). Ranked in Discord (`/opportunities`) and on the dashboard.

### 3.2 Launch radar (UniFi)
- Crawl the full catalog per region on a schedule, and diff it against the DB.
- Events: **new SKU**, **Early Access drop**, **restock ETA announced or changed**, **sold out** (with time-to-sellout), **back in stock**, **price change**.
- Hot items (EA, recently sold out, high score) poll faster automatically. Cold items back off.

### 3.3 Alerts
- Rule types: status transition, price ≤ X, spread ≥ Y% or ≥ $Z net, new SKU in category, EA drop, restock ETA.
- Routing: channel per category or event type, DM to watchers, role mentions, SMS for top-tier rules only.
- Discord buttons on alerts: **Open**, **Cart test**, **Snooze 24h**, **Log purchase** (opens a ledger modal).
- Persistent dedupe and cooldown, plus hysteresis to stop in_stock/low_stock flapping.

### 3.4 Web dashboard
- **Opportunities:** sortable table (score, net, ROI, sell-through, stock state) with filters.
- **Product detail:** retail price and stock timeline, resale comps scatter, profit breakdown.
- **Radar feed:** live event stream.
- **Watches and rules:** manage everything the Discord commands can.
- **Ledger:** inventory on hand, realized P&L, ROI, days-to-sell.
- **Health:** per-source success rate, latency, block/backoff state.
- Binds to localhost or LAN by default, with a simple access token. Discord OAuth is optional later.

### 3.5 Inventory & P&L ledger
- Lots: date, SKU, qty, unit cost, tax, shipping, where bought. Sales: platform, price, fees, shipping.
- FIFO cost basis. Realized and unrealized P&L. Per-SKU ROI and days-to-sell, which feed back into the opportunity score as your actual results.
- CSV export for taxes.

### 3.6 Guarded add-to-cart (port)
- Same safety model as today: approver allowlist, qty and price caps, cooldowns, **never enters payment and never places orders**.
- Reuses one persistent browser context. Triggered from an alert button or `/checkout`.

---

## 4. Phased delivery

Each phase ends in a working, deployable state. The old bot keeps running until Phase 2 reaches parity.

| Phase | Deliverable | Done when |
|---|---|---|
| **0. Foundation** | New `tracker/` package, settings, SQLite + migrations, logging, pytest/ruff, CI, Docker. Importer for `watchlist.json` and `subscribers.json`. | `pytest` green in CI; old data migrated into SQLite |
| **1. UniFi source v2 + scheduler** | `__NEXT_DATA__` parser (variant-level), catalog crawler, multi-region, async scheduler with rate limits and backoff, observation history | Full US catalog tracked via HTTP only; fixture tests for the parsers |
| **2. Alerts + Discord rebuild** | Rule engine, router, slash-command cogs, buttons, persistent dedupe. Other retailers ported behind the interface. Old bot retired. | Feature parity+ with the old bot; old files deleted |
| **3. Market intelligence** | eBay and homelabsales collectors, manual comps, matching, profit engine, opportunity score, deal and spread alerts, launch radar events | `/opportunities` returns a ranked list with net profit you trust |
| **4. Web dashboard** | Opportunities, product detail charts, radar feed, watches/rules, health | Day-to-day analysis happens in the browser |
| **5. Ledger** | Lots, sales, FIFO P&L, Discord "Log purchase" flow, CSV export | Real buys and sales tracked; ROI feeds scoring |
| **6. Guarded checkout** | Port and harden the ui.com add-to-cart/review flow | `/checkout test` passes against the persistent profile |

---

### Progress log

**2026-11-07: Phase 0 done, Phase 1 partly done** (branch `rebuild/foundation`)
- `tracker/` package: typed settings (legacy `STOCK_BOT_*` env names still work), logging, async
  HTTP client (per-host rate limits, retries with backoff, `Retry-After` support).
- SQLite schema with Alembic migrations: products, variants, listings, stock_observations, events,
  watches. A migration-drift test keeps models and migrations in sync.
- UniFi source v2: `__NEXT_DATA__` parser plus catalog crawler. Live run: **841 listings across 7
  categories in about 10s**, with no browser. Captures pack size from option labels (98 multi-packs,
  e.g. `UBB-XG-US` is a 2-pack), sold-out timestamps, restock ETAs, and coming-soon items.
- Ingest engine: change-only history, plus radar events (sold out, back in stock, price change,
  restock ETA, Early Access, new listing). Unknown statuses never overwrite known ones. The first
  crawl is a silent baseline.
- Legacy importer for `watchlist.json` and `subscribers.json` (idempotent).
- CLI: `tracker unifi product|crawl [--save]`, `tracker import-legacy`, `tracker db upgrade`.
- 52 tests, ruff, GitHub Actions CI.

**Findings that change the plan**
- Category pages don't include `isEarlyAccess`; only product pages do. EA detection needs targeted
  product-page polls for new or hot SKUs.
- The store home page exposes `freshlyStockedProducts` with `stockedAt` timestamps, which is a
  cheap, high-signal restock feed for the radar.
- `whats-new` redirects to the home page, so it's skipped as a category.

**2026-11-07: Phase 1 done** (same branch)
- `tracker run`: an asyncio scheduler (single-flight jobs, jitter, exponential backoff) runs a
  catalog crawl (5 min), the home feeds (1 min), and hot product-page polls (10s, 4 pages per
  tick) for each region.
- Freshly-stocked and what's-new feeds queue immediate product-page polls. New listings are
  polled too, because product pages are the only source of the Early Access flag.
- Hot listings: watched, high priority, Early Access, coming soon, restock ETA within 3 days, or a
  sold-out/restock/new/relisted event in the last 6h.
- Delisting is inferred only from complete crawls covering at least 50% of active listings, after a
  45-minute grace period. Seeing a delisted listing again emits `relisted`.
- Durable outbox: events are dispatched from the DB (`notified_at`), delivered at least once,
  and events older than 60 minutes (e.g. after downtime) are recorded but not announced. A
  `LoggingSink` is wired in now; the Discord sink plugs in during Phase 2.
- `tracker radar` shows the event feed.
- Multi-region verified live: `ca`/`uk`/`eu` live on `{region}.store.ui.com`, while `au` silently
  serves the US store and is now rejected. The **CA store sells US SKUs in CAD** (831 variants
  shared with the US store), which makes cross-border price spreads available, e.g. UDM-Pro-Max
  $599 USD vs $859 CAD. The CA store also uses an `Unavailable` status, mapped to out of stock.
- 80 tests. Live run: 2 regions, 1,682 listings, 35 feed-triggered product polls drained cleanly.

**Not yet verified:** an Early Access drop observed live (no EA items were public during testing),
and graceful SIGTERM shutdown on Linux/Docker (handler installed, untested on Windows).

**2026-11-07: Phase 2 done** (same branch). The legacy bot is retired.
- **Discord bot** (`tracker/bot/`): 13 slash commands. `/watch` (SKU/name autocomplete or any
  store URL, optional region and max price), `/unwatch`, `/watches`, `/subscribe`, `/stock`
  (per-region status and history), `/radar`, `/restocks`, `/status`, and admin `/check`,
  `/priority`, `/testalert`, `/cleanup`. Alerts carry persistent **Open** and **Watch**
  buttons. With `DISCORD_GUILD_ID` set, commands sync instantly and the old bot's stale global
  commands are cleared.
- **Alert routing** (`tracker/alerts.py`): important kinds go to the alert channel with pings
  (watchers honoring max price, global subscribers, an optional role). Routine kinds go to an
  optional radar channel, as digests when there are many. Price drops ping watchers.
  A 10-minute per-listing cooldown suppresses flapping.
- **Sinks:** Discord, Twilio SMS (back in stock and EA by default), and logging, all on the
  durable outbox. Message cleanup is ported (opt-in).
- **Other retailers:** JSON-LD first, ported per-site fallbacks scoped to the buy box, and
  anti-bot detection (captcha, Cloudflare challenge) with backoff. All four sites block plain
  HTTP from a residential IP; real block pages are kept as test fixtures.
- **Upgrade path:** on first start the old `watchlist.json`/`subscribers.json` are imported
  automatically (once). Old env names still work, and placeholder token/channel values are
  treated as unset.
- **Deploy:** uv-based Dockerfile (Python 3.13, locked deps, no Chromium), the systemd script
  and unit run `tracker run`, and the Docker setup script smoke-tests a product fetch.
- **Removed:** `bot.py`, `monitor.py`, `scrapers.py`, `checkout.py`, `config.py`,
  `requirements.txt`, YAML product packs, and the triage/rebuild scripts.
- **Bug caught live and fixed:** the legacy import created rows before the first crawl, so the
  first crawl emitted 840 `new_listing` events (840 pings). Baselines are now an explicit marker
  set only by a complete crawl (migration 0004 `meta`), and two flood guards were added: more than
  25 or 20% new listings in one crawl are recorded silently, and mass delisting (more than 10 or
  20%) is refused.
- 133 tests, including offline serialization of every slash command against Discord's limits.

**Not verified:** a live Discord session (no bot token on the dev machine), the Docker image
build (no Docker on the dev machine; the install steps were mirrored with `uv sync --locked
--no-dev`), and the systemd script beyond `bash -n`.

**Parity notes:** guarded checkout returns in Phase 6. YAML product packs are replaced by
full-catalog tracking plus `/priority`.

**2026-11-07: Phase 3 done** (same branch)
- **Data-source spike:** eBay search pages (403), the eBay API without a token (403), and Reddit
  (403 / login wall) all refuse anonymous access. Decision: eBay official API plus manual comps.
  The Browse API gives active listings. Marketplace Insights gives sold prices behind the same
  credentials if eBay approves it, and turns itself off on a 403.
- **Matching** (`tracker/market/matching.py`): SKU phrase or compact match, rejects longer
  catalog SKUs and model modifiers (U7-Pro ≠ U7 Pro Max / UDM-Pro ≠ UDM Pro SE), parts,
  accessories, and box-only listings. Detects lot sizes: pack variants need the exact quantity,
  and singles price lots per unit at a lower score. Condition comes from eBay condition IDs, else
  the title.
- **Valuation** (`tracker/market/valuation.py`): sold median (≥3 comps), else the active-listing
  25th percentile; sanity band (0.3–4× retail) plus IQR outlier removal. Net after a 13.6% +
  $0.40 fee, $12 shipping, and tax (default 0%). ROI, 30-day sell-through, confidence, and an
  opportunity score (profit × √ROI × confidence × liquidity).
- **Collection** (`tracker/market/service.py`): one variant per minute; hot items (watched, high
  priority, EA, coming soon, restock within 7 days) every 6h, others every 72h, accessories under
  $40 skipped. Comps are upserted by eBay item ID, and excluded comps are kept with a reason for
  auditing.
- **Deals:** a new `deal` event when a buyable item clears $30 net and 15% ROI at medium or high
  confidence. It fires once per crossing and re-arms after the item sells out. It's an important
  alert (pings by default, SMS by default). All alerts now show estimated profit when a valuation
  exists.
- **Surfaces:** `/opportunities`, `/value [refresh]`, admin `/comp`; CLI `tracker opportunities`
  and `tracker market value|collect|comp`.
- Migration 0005 (`market_comps`, `valuations`, `variants.comps_checked_at`). 178 tests.
  Verified live against the real catalog with manual test comps; the profit math was checked by
  hand.

**2026-11-07: eBay compliance change.** The eBay keyset is exempted from Marketplace Account
Deletion with "I do not persist eBay data", so the tracker no longer stores eBay listings. Results
are matched in memory, and only aggregate stats are kept (`ebay_stats`: counts, median and
percentile prices, matched/excluded counts). Migration 0006 purges any non-manual
`market_comps` rows. Valuation is now two-stage (`summarize` → `compute_valuation`), with basis
priority eBay sold > manual sold > eBay active. Active stats expire after 96h. Listing-level
detail is shown live only (`tracker market collect`, `/value refresh`).

**Verified live with production keys:** the Browse API works (100 results for U7-Pro: 24 matched,
65 correctly rejected as sibling models). Marketplace Insights is refused (token scope HTTP 400),
so collection runs active-only. Insight: at retail ($189), U7-Pro's eBay active market (25th
percentile $226.84) nets about −$5 after fees and shipping, so not a flip at MSRP.

**Next: Phase 4 (web dashboard).**

## 5. Risks and open questions

| Risk | Mitigation |
|---|---|
| **eBay sold data.** The official Marketplace Insights API is restricted access; the Browse API gives active listings only. Scraping sold search results is fragile and against eBay's ToS. | Pluggable `MarketSource`. Start with the Browse API (active) plus manual and homelabsales comps. Decide on sold-data access in Phase 3 after a spike. |
| UniFi changes its Next.js data shape | Parser fixtures plus a schema canary that fires a Discord admin alert when parsing confidence drops |
| Rate limiting or anti-bot measures on the UniFi store | Per-host token bucket, jitter, conditional requests, adaptive intervals. Browser fallback only if needed. |
| Comp matching accuracy (pack sizes, used vs new, bundles) | Confidence scoring, exclusion rules, a manual override per variant |
| Scope creep | Phases are independently shippable. Phase 3 is the value milestone. |

**Housekeeping before Phase 0:** git reports "dubious ownership" for this folder (it was created under a different Windows account SID). Run `git config --global --add safe.directory "E:/Code Projects/Applications/Discord/tracker-network-stock"` or retake ownership of the folder. All work happens on a feature branch, not `main`.
