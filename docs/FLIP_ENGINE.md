# Flip Engine: Plan v2

**Goal:** a private tool that makes *you* money by finding, buying, and reselling hot retail
products, and that gets better at predicting profit the more it sees.

**Decisions (2026-11-07):**
- **Deployment:** one always-on Linux server runs everything, and one bot serves multiple
  Discord servers.
- **Control:** a Windows control panel (web panel plus Tauri app), reached over Tailscale.
- **Roles:** owner (you) plus per-server admins.
- **Data budget: $0** (decided later the same day). Paid sources mentioned below, such as
  Keepa, are out. Free and official sources only; see [GRAILS.md](GRAILS.md).
- **Private tool:** it's not sold or shared.
- **Any hot retail product:** UniFi first, then GPUs, consoles, collectibles, homelab gear, and
  more.
- **Learning target:** profit and price prediction.

This replaces Phases 4–6 of [ROADMAP.md](ROADMAP.md). Phases 0–3 (UniFi tracking, alerts,
resale valuations) are done and become the foundation.

---

## 1. How money is actually made (the plays)

Each play is a detector that turns data the engine already has, or will have, into a
buy/skip/sell decision with expected profit.

| Play | What the engine detects | Data it needs | Have it? |
|---|---|---|---|
| **Restock flip** | A sold-out SKU with resale well above retail comes back in stock | Stock history, resale stats | ✅ |
| **Launch / Early Access premium** | New or EA SKU; buy day one, sell during the launch premium before supply catches up | New-listing and EA events, resale over time | ✅ events; ⏳ history |
| **Pack breakout** | A multi-pack whose per-unit cost is below single-unit resale (e.g. U7-Pro-5 at $899 is $179.80 per unit vs singles at ~$227) | Pack sizes, single resale | ✅ (UniFi packs are already modelled) |
| **Clearance / price drop** | A retail price drop below resale median minus fees | Price change events | ✅ |
| **Cross-retailer arbitrage** | The same product (same UPC) cheaper at retailer A than its resale on eBay or Amazon | UPC identity across retailers | ⏳ Phase 7 |
| **Cross-region** | A CA or EU store sells below US resale after shipping and duty | Multi-region prices | ✅ prices; ⏳ landed-cost model |
| **Return-window hedge** | Buy, list immediately, return unopened if it hasn't sold at target before the return window closes | Ledger plus each retailer's return window | ⏳ Phase 4 |
| **Amazon FBA** | Amazon buy-box price and sales rank imply a profitable FBA sale | Keepa data | ⏳ Phase 7 |

> ⚠️ The return-window hedge is within retailer policy for unopened items, but heavy returners
> get flagged (Best Buy, Amazon, and others track return rates). The engine should cap how often
> you rely on it per retailer.

---

## 2. Learning: profit and price prediction

### What we predict
1. **Resale price at time T:** where the market will be in 7, 14, and 30 days, not just today.
   Resale premiums decay after launch and after restocks, and that decay curve is the core
   signal.
2. **Time to sell:** how long a listing at price P takes to sell.
3. **Expected profit with a range:** for example, "80% chance net is at least $40", instead of
   one optimistic number.

### The decision formula (replaces today's static `net`)
```
expected_profit = P(sell within horizon) × (predicted_price − fees − shipping)
                  − cost − holding_cost(days, capital) − risk_buffer
```
Rank opportunities by expected profit per dollar of capital per day, which is what actually
compounds.

### Data the model learns from
| Source | Signal | Status |
|---|---|---|
| Retail stock and price history | Scarcity, restock cadence, time to sellout, MSRP | ✅ collected since Phase 1 |
| eBay aggregates **over time** | Launch-premium decay, sell-through trend | ⏳ today only the latest snapshot is kept; add a daily `ebay_stats_history` (aggregates only, so still compliant) |
| **Your ledger** (buys, sells, days to sell, fees) | Ground truth for every prediction | ⏳ Phase 4; the most valuable data you'll own |
| Category, age since launch, season, day of week | Seasonality and lifecycle | derived |
| Keepa (Amazon) | Amazon price and sales-rank history | ⏳ Phase 7 |

### Model ladder (each step must beat the last in a backtest)
1. **Baseline:** today's median comps minus fees. Already built.
2. **Decay curves per category:** fit premium-vs-days-since-launch with
   [statsforecast](https://github.com/Nixtla/statsforecast) or simple exponential fits, with
   category-level pooling so new SKUs borrow strength from similar ones.
3. **Gradient-boosted model:** [LightGBM](https://github.com/microsoft/LightGBM) on all
   features to predict resale in N days and the probability of profit.
4. **Time-to-sell:** survival analysis with [lifelines](https://github.com/CamDavidsonPilon/lifelines),
   which handles listings that haven't sold yet correctly.
5. **Honest uncertainty:** conformal prediction intervals with
   [MAPIE](https://github.com/scikit-learn-contrib/MAPIE).

### The learning loop
- Every prediction is **logged with the inputs it saw**.
- When the ledger records the real outcome, the error is computed.
- Retrain nightly, and promote a new model only if its backtest beats the current one.
- A calibration page shows things like "when we said 80%, it happened 76% of the time".
- **eBay license constraint:** eBay's June 2025 agreement update restricts feeding *restricted
  API* data into third-party generative AI. Classical models trained on our own aggregates and
  our own ledger don't involve generative AI. Keep it that way, and don't pipe eBay data into
  LLM tools.

---

## 3. Any-retailer engine: legitimate access, engineered well

Plain HTTP was blocked by Amazon (captcha), Best Buy (connection reset), Newegg (403), and B&H
(403). **The engine does not fight those defences.** That means no fingerprint spoofing,
anti-detect browsers, CAPTCHA solving, proxy rotation around IP bans, or purchase-limit
workarounds. A site that blocks you has said no. Working around it risks account bans and
cancelled orders, and edges towards unauthorized access. (An earlier draft of this plan listed
curl_cffi, patchright, and camoufox; they were removed for this reason.)

Instead, data comes in through the doors that are open, done better than anyone else:

1. **Official and partner data first:**
   - Best Buy Products API (free key; online availability, price, UPC).
   - Walmart affiliate API.
   - [Keepa](https://keepa.com) for Amazon price and sales-rank history.
   - eBay official APIs (already integrated).
2. **Polite public polling:** per-host budgets, jitter, conditional requests (ETag and
   If-Modified-Since), shared caching, and change-driven polling (feeds and hot lists before
   full crawls). Blocks are detected and **respected** with backoff (already built).
3. **Signals that come to you:** retailer restock emails and "notify me" subscriptions, parsed
   from a dedicated inbox, and the store feeds we already use (UniFi freshly-stocked).
4. **Long tail:** [changedetection.io](https://github.com/dgtlmoon/changedetection.io) for sites
   that allow monitoring; ingest its webhooks as snapshots.
5. **Your own session for your own actions:** checkout uses your normal logged-in browser, at
   human pace, and only when you confirm.
6. **Product identity across retailers:** UPC/GTIN, then MPN, then fuzzy title, so one product
   links its Best Buy, Walmart, Amazon, and eBay listings. The Phase 3 title matcher extends to
   this.

The UniFi store, the core of this tool, already works without any of this: its structured data
is served openly.

---

## 4. Execution: turning signals into money faster

- **Guarded buy (old Phase 6, expanded):** one tap on a Discord alert opens a pre-filled cart
  in your logged-in browser profile, with price and quantity caps. You confirm and pay. No
  unattended auto-checkout: it's against most retailers' terms, and it's an arms race.
- **Auto-list on eBay:** eBay's Sell APIs (Inventory and Offer, using your own seller OAuth)
  create the listing the moment you log a purchase, priced from the model's predicted price
  and repriced as the decay curve moves. This is your own data, so it's compatible with the
  no-persist exemption.
- **Capital and risk:** a budget, maximum units per SKU, maximum capital in one category, and a
  stop-loss rule ("cut the price to break even after N days").

---

## 5. Control plane, security, and compliance

### Topology
```
 Windows PC (Tauri app)   Phone (browser)
          \                 /
       Tailscale private network (WireGuard; no open router ports)
                    |
   Linux server (always on), Docker Compose:
     tracker   - scheduler, Discord bot (all your servers), REST API on the tailnet only
     updater   - the only container with Docker access; installs signed images only
     backup    - nightly SQLite snapshot, encrypted off-site (restic)
```
- **One bot, many Discord servers.** One process serves every server it's in; no
  per-server deployments.
- **The panel is never on the public internet.** The API binds to localhost and is published
  with `tailscale serve` (HTTPS, reachable only by your tailnet). Tailscale ACLs allow only your
  devices. The host firewall denies inbound traffic except SSH over the tailnet.

### Control panel
- A **web app** (FastAPI JSON API plus a React single-page app) served by the tracker.
- **Windows:** the same app packaged with [Tauri 2](https://github.com/tauri-apps/tauri):
  native window, tray icon, toast notifications for deals and alerts (pushed over
  WebSocket), and auto-start. The phone uses the same panel in its browser.
- **Pages:**
  - Opportunities, product detail with charts, radar feed, watches.
  - Ledger and P&L.
  - Servers and permissions.
  - Sources and keys (status only; secrets are never displayed).
  - Budgets.
  - Ops: health, jobs, logs, version, update and rollback.
  - Audit log.

### Who can do what (role-based access)
| Role | Scope | Can |
|---|---|---|
| **Owner** (you) | Global | Everything: sources and keys, budgets, ledger, buying, updates, approving servers, all settings. Panel access is owner-only. |
| **Server admin** | One Discord server | That server's channels, roles, mention rules, which alerts it gets, member permissions |
| **Member** | One Discord server | Watch and unwatch, `/stock`, `/radar`, plus whatever the server's tier allows |

- **Server allowlist:** the bot automatically leaves any server you haven't approved. Turn off
  "Public Bot" in the Discord developer portal, so a leaked invite link can't spread it.
- **Private by default:** the ledger, budgets, and buying are never visible in any server.
  Each server has a visibility flag for eBay-derived valuations. Showing them to other people
  is "displaying eBay content" under eBay's license terms, so leave it off except where it's
  only you.
- **Configuration moves from `.env` to the database per server:** channels, roles, mentions,
  cooldowns. `.env` keeps only secrets and bootstrap settings.

### Security controls
- **Secrets:** on the server only (`.env` with `600` permissions, or Docker secrets). Never
  sent to the panel, never logged (log redaction filter), never in git (pre-commit secret scan
  such as [gitleaks](https://github.com/gitleaks/gitleaks), plus a CI scan). A rotation
  runbook covers each key.
- **Panel authentication:** Tailscale identity (the device must be on your tailnet), plus an
  owner login with a passkey or TOTP, plus short-lived sessions. Changes to buying, budgets,
  or keys require re-authentication.
- **Audit log:** append-only record of every admin action from the panel or Discord (who,
  where, what, before and after). Viewable in the panel, exportable.
- **Containers:** non-root, read-only root filesystem, `no-new-privileges`, all capabilities
  dropped, minimal base image. The updater is the only privileged container.
- **Supply chain:**
  - `uv.lock` pinned with hashes; Renovate or Dependabot for updates.
  - [pip-audit](https://github.com/pypa/pip-audit) and [Trivy](https://github.com/aquasecurity/trivy)
    in CI.
  - Images signed with [cosign](https://github.com/sigstore/cosign); the updater refuses
    unsigned images.
- **Updates:** a merge to main triggers CI to build, scan, sign, and push to GHCR. In the panel,
  "Update" makes the updater verify the signature, back up the DB, pull, run migrations, and
  health-check, rolling back automatically on failure.
- **Backups:** a nightly SQLite online backup, encrypted off-site with
  [restic](https://github.com/restic/restic), with a monthly automated restore test.
- **Monitoring:** a heartbeat to your Discord DMs or the Windows app when any job fails
  repeatedly. Optional [Uptime Kuma](https://github.com/louislam/uptime-kuma) on the tailnet.
- **Abuse resistance:** per-user command rate limits, input validation on every command,
  bounded queries, and no user-supplied URLs fetched except from the allowlisted retailer
  domains (prevents server-side request forgery).

### Compliance checklist
| Area | Obligation | How the system enforces it |
|---|---|---|
| eBay API | No persisted eBay listing data (exemption basis), no generative-AI ingestion of restricted data, display rules | Aggregates only (done); valuations flag per server; no LLM paths for eBay data |
| Discord | Developer Terms and Policy; members' data | Minimal intents and permissions; `/forgetme` deletes a member's watches and data; short privacy note in `/about` |
| Retailers | Terms of service, rate limits, purchase limits | Official APIs first, polite polling, respect blocks, no unattended checkout, purchase caps |
| Twilio and SMS | Consent | Only numbers you configure |
| Taxes | Resale income records | Ledger CSV export; purchase tax setting |

---

## 6. Revised phases

| Phase | Deliverable | Why now |
|---|---|---|
| **4. Multi-server core and security** | Owner/server-admin/member roles; per-server config in the DB; server allowlist with auto-leave; audit log; secret redaction; `/forgetme`; hardened containers; gitleaks | Required before the bot joins multiple servers |
| **5. Ledger and outcomes** | `/bought`, `/sold`, `/inventory`; FIFO P&L, days to sell, return windows; CSV export | Ground truth for learning; start the day you flip |
| **6. Control panel and ops** | FastAPI API plus React panel on Tailscale; Tauri Windows app; history snapshots and charts; signed updates with rollback; backups; heartbeat alerts | Your control centre |
| **7. Prediction v1** | Decay curves, LightGBM resale and time-to-sell models, expected profit with ranges, backtests, calibration, nightly retrain | The learning |
| **8. Any-retailer via official data** | Best Buy and Walmart APIs, Keepa, inbox restock parsing, changedetection.io bridge, UPC identity, first non-UniFi categories | Widens the opportunity set |
| **9. Execution** | Guarded buy with your own session, eBay auto-listing and repricing, capital limits and stop-losses | Speed and less manual work |

---

## 7. Open-source references

| Repo / tool | Use |
|---|---|
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | Long-tail restock and price monitor; ingest via webhook |
| [Discount Bandit](https://github.com/Cybrarist/Discount-Bandit) | Self-hosted multi-store price tracker; reference for store adapters |
| [evotecit/unifistocktracker](https://github.com/evotecit/unifistocktracker) | Prior UniFi stock tracker (PowerShell, stale); the niche is open |
| [Tauri 2](https://github.com/tauri-apps/tauri) | Windows desktop app around the web panel |
| [Tailscale](https://tailscale.com) | Private network to reach the panel; no open ports |
| [cosign](https://github.com/sigstore/cosign), [Trivy](https://github.com/aquasecurity/trivy), [gitleaks](https://github.com/gitleaks/gitleaks), [pip-audit](https://github.com/pypa/pip-audit) | Signed images, scanning, secret detection |
| [restic](https://github.com/restic/restic), [Uptime Kuma](https://github.com/louislam/uptime-kuma) | Encrypted backups, uptime monitoring |
| [keepa](https://github.com/akaszynski/keepa) | Amazon price and sales-rank history |
| [statsforecast](https://github.com/Nixtla/statsforecast), [LightGBM](https://github.com/microsoft/LightGBM), [lifelines](https://github.com/CamDavidsonPilon/lifelines), [MAPIE](https://github.com/scikit-learn-contrib/MAPIE) | Forecasting, boosting, time-to-sell, prediction intervals |
| [DuckDB](https://github.com/duckdb/duckdb) | Fast analytics and backtests over the SQLite history |

---

## 8. Risks to keep in view

- **eBay license:** private use is the cleanest position. Never redistribute eBay data, and
  never feed restricted-API data to generative AI.
- **Retailer terms and anti-bot:** official data first, polite polling, respect blocks, no
  evasion tooling, and no unattended checkout.
- **Returns:** heavy returners get flagged, so cap reliance on the return-window hedge.
- **Taxes:** resale income is taxable; your 0% purchase-tax setting assumes a resale
  certificate. The ledger's CSV export is for this.
- **Capital risk:** predictions are probabilistic. Enforce budgets and stop-losses in code,
  not in your head.

---

## Progress log

**2026-11-07: Phase 4 (multi-server core and security) done**
- `guilds` table: allowlist (pending, approved, or blocked) and per-server settings (alert and
  radar channels, mention role, admin role, ping kinds, cooldown, digest size, valuation
  visibility).
- Startup pre-approves `DISCORD_GUILD_ID` and `DISCORD_ALLOWED_GUILD_IDS` and seeds the home
  server from `.env`. Single-server installs are adopted automatically from
  `DISCORD_CHANNEL_ID`.
- An unknown server gets an Approve or Block DM to the owner(s), and the bot auto-leaves after
  the grace period. Commands sync per approved server; stale global commands are cleared.
- Roles: owner (config IDs or the app owner/team), server admin, and member. One
  `interaction_check` gates every command; a command not in the table is owner-only by default.
- Watches and subscriptions record their server; pings stay in that server. Legacy watches
  belong to the home server.
- Valuations are hidden per server (stripped from alerts, deal alerts suppressed,
  `/opportunities` and `/value` refused) unless the owner enables them.
- The alert sink refuses to post to a channel belonging to a different server.
- `audit_log` is append-only, enforced by SQLite triggers in both the models and migration
  0007. It records approvals, configuration changes, comps, checks, priorities, test alerts,
  cleanups, and forgets.
- `/forgetme` (deletes watches and subscriptions, unlinks comps) and `/about` (privacy note).
- Log redaction of configured secrets and token-shaped strings, including tracebacks.
- Hardened container (read-only, no capabilities, `no-new-privileges`, limits, healthcheck),
  pre-commit (gitleaks, ruff), and CI security job (gitleaks, pip-audit; currently no known
  vulnerabilities).
- 226 tests. Verified live: migration, bootstrap, and the append-only audit log rejecting a
  `DELETE`.

**Not verified:** a live Discord session (approval DMs, per-server command sync). It needs a
bot token on the test machine.

**2026-11-07: seller de-duplication and `/scan`**
- eBay sellers are parsed in memory. Each active seller counts **once, at their cheapest
  listing**, so one reseller's many listings can't inflate comp counts or confidence. Only the
  aggregate count of distinct sellers is stored (`ebay_stats.active_listings` vs `active_count`,
  migration 0008); seller identities never are.
- `/scan` (owner) and `tracker market scan`: a read-only live profit scan. It prices pack
  singles first, then buyable and high-value items, against eBay. It reports top flips, **pack
  breakouts** (per-unit cost vs single resale, profit per pack, break-even resale per unit), and
  items that lose money at retail. Nothing is stored, no alerts fire, and only one scan runs
  at a time.
- Migration 0008 initially lacked a server default and **failed on databases with existing eBay
  stats**. Caught before release; the new `test_upgrade_preserves_existing_data` now upgrades a
  populated database.
- Live scan (25 calls): UVC-G5-Bullet 3-pack is about +$186 per pack (10 sellers / 15 listings;
  break-even $152.85 per unit). UAP-AC-PRO loses $81.50 at retail and its 5-pack loses $361.
  234 tests.
