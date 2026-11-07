# Grail Radar: finding "party hat" items

**Goal:** spot items that are about to become rare (discontinued, short-run, Early Access,
quietly pulled) *before* the market prices it in. Buy them at retail, then either flip the spike
or vault them and sell near the peak.

**Decisions (2026-11-07):**
- **Markets:** UniFi and networking end-of-life (EOL) and Early Access first, then the other
  ideas in §5.
- **Holding:** fast flips and long holds, with a capital split.
- **Data budget: $0.** Free and official sources only.

---

## 1. What research says actually predicts rarity value

| Finding | Source | What we do with it |
|---|---|---|
| **Retirement/EOL is the strongest signal.** When retail sells out for good, supply passes to resellers and prices jump. LEGO is the documented case: about +20–30% at retirement, then about 10–20% a year (the tracker's own estimates). | [BrickEconomy pricing FAQ](https://www.brickeconomy.com/faqs-pricing.aspx), [BrickPicker retirement model](https://app.brickpicker.com/tools/retirement?set=10270) | Detect EOL *ourselves*, and earlier than the market |
| **Ubiquiti publishes no EOL roadmap** (and buyers complain about it). Its legacy and vintage list is only updated after the fact. | [Community FAQ](https://ubntwiki.com/_export/xhtml/guides/faq), [UI legacy products](https://help.ui.com/hc/articles/1500001268521) | Nobody else predicts UniFi EOL, so this is our edge |
| **Many spikes aren't real.** A listed price that nobody pays is a mirage; check what things actually sell for. | [MTGGoldfish on buyouts](https://www.mtggoldfish.com/articles/understanding-buyouts-and-price-spikes) | Need several sellers plus sell-through, never one listing |
| **Supply floods in after record prices**, and recall-driven spikes usually fade within a year. | [cllct](https://cllct.com/sports-collectibles/auctions/from-sealed-iphones-to-jordan-star-cards-markets-flood-after-record-sales), [recall analysis](https://geekvibesnation.com/what-a-recalled-collectible-really-tells-you-about-which-toys-hold-their-value/) | Exit rules: sell into strength and track momentum, don't wait for a peak that never comes |
| **Edition sizes are rarely disclosed.** | [Axios](https://www.axios.com/2023/02/25/supply-and-demand-in-the-collectibles-market) | Infer scarcity from behaviour (sell-out speed, supply shrinking) instead |

---

## 2. Rarity signals FlipScout can compute (all free)

| # | Signal | How | Data |
|---|---|---|---|
| 1 | **Regional delisting cascade** | A product disappears from the CA, UK, or EU store before the US one. That's often the first public sign of EOL. | UniFi store, multi-region (have it; enable `us,ca,uk,eu`) |
| 2 | **Long out of stock with no restock date** | Sold out for N weeks with no restock ETA, while siblings restock normally | Stock history (have) |
| 3 | **Successor launched** | A new model in the same family appears (U7 → U6-era EOL, G6 → G5). Predecessors usually follow into EOL. | New-listing events plus SKU/category families (have the events) |
| 4 | **Official legacy confirmation** | The item appears in Ubiquiti's vintage/legacy help-center list | help.ui.com public JSON API (free, no key) |
| 5 | **Supply contraction** | eBay distinct sellers and listings trending down | eBay aggregate history (**needs daily snapshots; start now**) |
| 6 | **Price momentum** | eBay 25th percentile, and resale vs MSRP, rising week over week | Same history |
| 7 | **Sell-out speed** | Time from restock to sold out shrinking | Stock history (have) |
| 8 | **Early Access / short run** | EA flag, EA SKU suffixes (e.g. `-EA` seen in live eBay titles), colour variants, small batches | Product pages (have EA flag) |
| 9 | **Lifecycle age** | Days since launch: older models are closer to EOL | First-seen date plus a Wayback Machine backfill |
| 10 | **Legacy demand** | MSPs pay a premium for discontinued models to extend existing installs (matching switches and cameras) | eBay sell-through on EOL SKUs |

### Grail score
```
grail_score = EOL_probability     (signals 1–4, 9)
            × demand              (sell-through, distinct sellers, sell-out speed)
            × momentum            (signals 5–6)
            × premium_headroom    (resale ÷ retail)
            × confidence          (data depth; penalize thin, one-seller markets)
```
Each item gets a plain-English reason, e.g. "Delisted in CA and EU, successor launched 41 days
ago, eBay sellers down 30% in 3 weeks, buy at retail $199."

---

## 3. Strategies (capital split)

| Book | Share | Play | Exit rule |
|---|---|---|---|
| **Flip book** | ~70% | Restocks, launch and EA spikes, pack breakouts, "last call" restocks of EOL candidates | Sell within days to weeks; time stop after N days |
| **Vault book** | ~30%, capped | Buy at retail when EOL probability is high, before retirement | Target price, **or** momentum flattens for N weeks, **or** a supply surge appears. Then sell. |

- **"Last call" alerts:** when an EOL candidate restocks, it may be the final batch, so ping
  immediately.
- **Vault tracking:** each held item shows cost, current resale, unrealized gain, months held,
  and the exit trigger status. It's a portfolio, not a shelf.

---

## 4. Free data sources

| Source | Gives | Cost / access |
|---|---|---|
| UniFi store (`us`, `ca`, `uk`, `eu`) | Stock, prices, EA, delistings | Free; have it |
| eBay Browse API | Resale for **any** category: the universal market signal | Free; have it (store aggregates only) |
| help.ui.com (Zendesk Help Center API) | Official vintage/legacy lists | Free public JSON |
| Internet Archive Wayback CDX API | Historical store snapshots: launch dates, past prices | Free |
| Best Buy Products API | Mainstream retail incl. availability and active flags (for §5) | Free developer key (verify fields) |
| Reddit API (personal "script" app) | Demand chatter, "where can I buy X" | Free for personal use (register an app) |

**Start the clock now.** eBay history can't be backfilled because we don't store listings, so
daily aggregate snapshots are only as old as the day we start them. That makes this the first
thing to build.

---

## 5. Other ideas (beyond UniFi), ranked for free-data feasibility

1. **Networking and homelab EOL generally:** Synology, MikroTik, TP-Link Omada, Raspberry Pi
   and Compute Modules, mini PCs. Same buyers (MSPs, homelabbers), same eBay signal, the same
   "legacy demand" dynamic.
2. **Best Buy-exclusive and limited items:** Founders Edition GPUs (Best Buy is the US
   channel), special-edition consoles and controllers, Pokémon TCG and LEGO sold at Best Buy.
   It's one free API, with eBay as the resale oracle.
3. **LEGO retiring sets:** the most predictable pattern, but the best data (BrickEconomy,
   BrickLink history) isn't free through official APIs. Use retailer availability plus eBay
   sold-style signals instead, at lower confidence.
4. **Colour, EA, and region variants:** black/white editions, EA-labelled units, region-only
   SKUs. Short runs inside otherwise common product lines.
5. **Recalled or pulled products:** usually a fading spike (see §1), so flip-book only, never
   vault.

---

## 6. Build order

| Step | Deliverable |
|---|---|
| **G1. Start the clock** | Daily `ebay_stats_history` (aggregates only) for watched, hot, and EOL-candidate items; enable all four UniFi regions; stock-history derived metrics (time to sellout, out-of-stock duration) |
| **G2. EOL detector** | Signals 1–4 and 9: regional cascade, long-out-of-stock, successor detection by family, help.ui.com legacy list, Wayback first-seen backfill. New radar events: `eol_suspected`, `eol_confirmed`, `last_call`. |
| **G3. Grail score and `/grails`** | Ranked candidates with plain-English reasons; grail alerts on high-score items in stock at retail |
| **G4. Vault** | Holds in the ledger with targets and exit-trigger alerts (needs the Phase 5 ledger) |
| **G5. Beyond UniFi** | Best Buy API source, networking brands, families mapped across retailers |

G1 should start immediately, even before the rest, because history compounds.

---

## Progress log

**2026-11-07: G1 (start the clock) done**
- `ebay_stats_history`: one row per variant per UTC day (latest collection wins), holding
  aggregates only (sellers, listings, price floor, median, retail). Migration 0009.
- Collection tiers: **hot** every 6h; **end-of-life candidates daily** (sold out, coming
  soon, withdrawn from a region, or **delisted**; delisted items used to be skipped entirely);
  everything else every 72h. Accessories and cheap multi-packs (judged per unit) no longer
  spend eBay quota unless watched.
- All four UniFi stores by default (`us,ca,uk,eu`). New regions baseline silently.
- New status `RegionNotAvailable` (UK) mapped to out of stock, with the raw value kept: a
  regional withdrawal is signal #1.
- `tracker/history.py`: restock count, median sell-out time, out-of-stock duration (marked
  "+" when it started before tracking), and N-day eBay trend (seller and price-floor change).
- Surfaced in `/stock` (sell-out speed, out of stock for, withdrawn, delisted), `/value`
  (eBay trend), and `tracker history <sku>`.
- 244 tests.

**Next: G2 (EOL detector):** regional cascade, long out of stock, successor detection,
help.ui.com legacy list, Wayback first-seen backfill, and `eol_suspected`, `eol_confirmed`, and
`last_call` events.
