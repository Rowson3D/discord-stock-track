# Discovery: finding hot items beyond what you already track

FlipScout watches the UniFi store closely. Discovery widens the net: it notices products that
are hot elsewhere, reads their resale market on eBay, and puts the promising ones in an inbox
where you decide what to track. It stays inside the project's rules: **free and official
sources only, and it respects every site's robots.txt and terms.**

## How it works

```
signals ──► candidates ──► eBay check ──► honest estimate ──► inbox (Track / Ignore)
                                │                                   │
                                └──── daily snapshots during trial ◄┘
```

1. **Signals** say "this product is hot": trending or most viewed, sold out, a big discount,
   or you asking about it. Each signal adds *heat* to a **candidate**. Heat halves every 3
   days without new signals.
2. **Identity.** A candidate is keyed by its model number when one can be found
   (`WH-1000XM5`, `RTX 5090`), otherwise its UPC, otherwise its cleaned title. Signals
   from different places about the same product land on the same candidate.
3. **eBay check.** FlipScout searches eBay's official Browse API, keeps only listings that are
   really that product (the model number must appear; parts, empty boxes, lots, and
   accessories "for" the product are rejected), counts one price per seller, and keeps only
   **aggregates** (sellers, price floor, median). No eBay listing is ever stored. Checks
   share a daily budget (`DISCOVERY_DAILY_EBAY_CALLS`, default 400 of eBay's 5,000).
4. **Honest estimate.** It's the same rule that stopped the UniFi fake deals:
   - **Buyable at regular price somewhere?** Resale is capped at that price plus tax and
     convenience. Nobody pays a reseller more than the store charges. Profit then only comes
     from a **sale price below regular**.
   - **Sold out at the source?** eBay asking prices are the real market (a restock target).
   - Asking prices are never "sold" data, so confidence stays **low**, or **medium** when
     the source is sold out and there are 3 or more sellers.
5. **Promising** (estimated net ≥ `DISCOVERY_MIN_PROFIT_CENTS`, ROI ≥ `DISCOVERY_MIN_ROI`,
   3 or more sellers) goes to the inbox and is rechecked daily for `DISCOVERY_TRIAL_DAYS`
   (14). If it fades, it's dropped. A fresh signal can bring it back.
6. **You decide.** **Track** keeps daily eBay snapshots and, when the candidate has a link to
   a supported store, adds it to your watchlist so restocks and price drops ping you.
   **Ignore** hides it for good, even if it trends again.

## Sources

| Source | Status | What it gives |
|---|---|---|
| **Check an item** (panel) | ✅ No key needed | Type any product name or paste a supported store link; FlipScout reads its eBay market right away. |
| **Best Buy Products API** | ✅ Needs a free key (`BESTBUY_API_KEY`) | Trending and most-viewed products, sold-out products in categories you choose, sale versus regular price, UPC, model number, links. |
| **Reddit** (r/buildapcsales, r/hardwareswap, r/homelabsales) | ⏳ Later | Needs Reddit API approval for a personal app. |
| **Slickdeals** | ❌ Not used | Its robots.txt disallows automated access to the RSS feeds (`Disallow: /newsearch.php?*rss=*`). |
| Amazon, Walmart, Target, StockX, Keepa | ❌ Not used | Pages are anti-bot protected or the data is paid. Walmart's affiliate API may be added if approved. |

### Best Buy setup
1. Register at <https://developer.bestbuy.com> and create a key.
2. Add it to `.env` only: `BESTBUY_API_KEY=...` (never share it in chat).
3. Optional: limit sold-out scans to categories, e.g.
   `BESTBUY_CATEGORIES=abcat0700000,pcmcat295700050012`. Find IDs with the Categories API,
   e.g. `https://api.bestbuy.com/v1/categories(name=Video Games*)?apiKey=…`.
4. Restart. The Discovery page shows the source status.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `DISCOVERY_ENABLED` | `true` | Turn discovery off entirely. |
| `DISCOVERY_DAILY_EBAY_CALLS` | `400` | eBay searches discovery may spend per day. |
| `DISCOVERY_CHECKS_PER_RUN` | `4` | Candidates checked every `DISCOVERY_CHECK_INTERVAL_SECONDS` (600). |
| `DISCOVERY_MIN_PROFIT_CENTS` | `2500` | Estimated net needed to count as promising. |
| `DISCOVERY_MIN_ROI` | `0.15` | Estimated ROI needed to count as promising. |
| `DISCOVERY_TRIAL_DAYS` | `14` | Daily rechecks for promising candidates. |
| `BESTBUY_API_KEY` | (none) | Enables the Best Buy source. |
| `BESTBUY_CATEGORIES` | (none) | Category IDs for sold-out scans. |
| `BESTBUY_INTERVAL_MINUTES` | `60` | How often to read Best Buy. |

## Not yet
- Discord alerts for newly promising candidates (the panel shows them; alerts come next).
- Reddit, then Walmart's affiliate API if approved.
- Learning which sources and categories actually produce flips, and shifting the eBay budget
  toward them (needs the ledger).
