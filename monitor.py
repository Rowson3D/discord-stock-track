"""
StockMonitor: manages the watchlist, polling loop, and Discord alert dispatch.
"""
import json
import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from pathlib import Path

import discord
import yaml
from config import CONFIG
from scrapers import scrape

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

STATUS_EMOJI = {
    "in_stock":     "🟢",
    "out_of_stock": "🔴",
    "low_stock":    "🟡",
    "unknown":      "⚪",
}

STATUS_COLOR = {
    "in_stock":     0x57F287,   # green
    "out_of_stock": 0xED4245,   # red
    "low_stock":    0xFEE75C,   # yellow
    "unknown":      0x99AAB5,   # grey
}

STATUS_LABEL = {
    "in_stock":     "In Stock",
    "out_of_stock": "Out of Stock",
    "low_stock":    "Low Stock",
    "unknown":      "Unknown",
}

SITE_NAMES = {
    "ui.com":            "Ubiquiti Store",
    "amazon.com":        "Amazon",
    "bhphotovideo.com":  "B&H Photo",
    "newegg.com":        "Newegg",
}


def detect_site(url: str) -> str | None:
    """Return the canonical site key from a URL, or None if unsupported."""
    host = urlparse(url.strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    for key in ["ui.com", "amazon.com", "bhphotovideo.com", "newegg.com"]:
        if key in host:
            return key
    return None


# How many times to retry a scrape that returns "unknown" before accepting it
SCRAPE_RETRIES = 2
RETRY_DELAY = 3  # seconds between retries


class StockMonitor:
    def __init__(self, bot):
        self.bot = bot
        self.watchlist: list[dict] = []
        self.watchlist_file = Path(CONFIG["watchlist_file"]).expanduser().resolve()
        self.product_packs_dir = Path(CONFIG["product_packs_dir"]).expanduser().resolve()
        # Track last check time per URL for per-site interval logic
        self._last_checked: dict[str, float] = {}
        # Track consecutive "unknown" results per URL for diagnostics
        self._consecutive_unknowns: dict[str, int] = {}

    # ── Watchlist persistence ──────────────────────────────────────

    def load_watchlist(self):
        """Load watchlist from JSON, merging with config defaults."""
        if self.watchlist_file.exists():
            try:
                with self.watchlist_file.open(encoding="utf-8") as f:
                    self.watchlist = json.load(f)
                logger.info(f"Loaded {len(self.watchlist)} products from watchlist.")
            except Exception as e:
                logger.error(f"Failed to load watchlist: {e}")
                self.watchlist = []
        else:
            self.watchlist = []

        # Merge default products from config (skip duplicates)
        existing_urls = {p["url"] for p in self.watchlist}
        for product in CONFIG.get("default_products", []):
            if product["url"] not in existing_urls:
                product = dict(product)
                product.setdefault("last_status", "unknown")
                product.setdefault("last_seen_in_stock", None)
                self.watchlist.append(product)
                logger.info(f"Added default product: {product['name']}")

        self._save_watchlist()

    def _save_watchlist(self):
        try:
            self.watchlist_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.watchlist_file.with_suffix(f"{self.watchlist_file.suffix}.tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(self.watchlist, f, indent=2)
            temp_file.replace(self.watchlist_file)
        except Exception as e:
            logger.error(f"Failed to save watchlist: {e}")

    # ── Add / Remove ───────────────────────────────────────────────

    def add_product(self, url: str) -> str:
        url = url.strip()
        site = detect_site(url)
        if not site:
            return (
                f"❌ Unsupported site. Supported: "
                f"ui.com, amazon.com, bhphotovideo.com, newegg.com"
            )

        existing = [p for p in self.watchlist if p["url"] == url]
        if existing:
            return f"⚠️ Already watching: {url}"

        product = {
            "url": url,
            "name": url,          # Will be updated on first check
            "site": site,
            "last_status": "unknown",
            "last_seen_in_stock": None,
        }
        self.watchlist.append(product)
        self._save_watchlist()
        return f"✅ Now watching **{SITE_NAMES[site]}**: {url}"

    def list_product_packs(self) -> list[dict]:
        return list(self._load_product_packs().values())

    def add_product_pack(self, pack_id: str) -> str:
        pack_id = pack_id.strip().lower()
        packs = self._load_product_packs()
        pack = packs.get(pack_id)
        if not pack:
            available = ", ".join(sorted(packs)) or "none"
            return f"⚠️ Product pack not found: `{pack_id}`. Available packs: {available}"

        added = 0
        skipped = 0
        existing_urls = {p["url"] for p in self.watchlist}

        for product in pack.get("products") or []:
            for vendor in product.get("vendors") or []:
                url = vendor.get("url", "").strip()
                site = vendor.get("site") or detect_site(url)
                if not url or not site or url in existing_urls:
                    skipped += 1
                    continue

                self.watchlist.append(
                    {
                        "url": url,
                        "name": product.get("name") or product.get("sku") or url,
                        "site": site,
                        "sku": product.get("sku"),
                        "category": product.get("category"),
                        "priority": product.get("priority", "normal"),
                        "pack": pack["id"],
                        "vendor": vendor.get("name") or SITE_NAMES.get(site, site),
                        "last_status": "unknown",
                        "last_seen_in_stock": None,
                    }
                )
                existing_urls.add(url)
                added += 1

        if added:
            self._save_watchlist()

        return f"✅ Added {added} watch entries from **{pack['name']}**. Skipped {skipped}."

    def remove_product(self, url: str) -> str:
        url = url.strip()
        before = len(self.watchlist)
        self.watchlist = [p for p in self.watchlist if p["url"] != url]
        if len(self.watchlist) < before:
            self._save_watchlist()
            return f"🗑️ Removed from watchlist: {url}"
        return f"⚠️ URL not found in watchlist: {url}"

    def get_watchlist(self) -> list[dict]:
        return self.watchlist

    def _load_product_packs(self) -> dict[str, dict]:
        packs: dict[str, dict] = {}
        if not self.product_packs_dir.exists():
            return packs

        for path in sorted(self.product_packs_dir.glob("*.yaml")):
            try:
                with path.open(encoding="utf-8") as f:
                    pack = yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"Failed to load product pack {path}: {e}")
                continue

            pack_id = str(pack.get("id") or path.stem).strip().lower()
            products = pack.get("products") or []
            packs[pack_id] = {
                "id": pack_id,
                "name": pack.get("name") or pack_id,
                "description": pack.get("description") or "",
                "products": products,
                "product_count": len(products),
                "watch_entry_count": sum(len(product.get("vendors") or []) for product in products),
            }

        return packs

    def build_report_embed(self) -> discord.Embed:
        counts = {"in_stock": 0, "low_stock": 0, "out_of_stock": 0, "unknown": 0}
        for product in self.watchlist:
            status = product.get("last_status", "unknown")
            counts[status if status in counts else "unknown"] += 1

        embed = discord.Embed(
            title="Stock Watch Report",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name="Tracker Network Stock Bot")
        embed.description = (
            f"Tracking **{len(self.watchlist)}** vendor entries. "
            f"Available now: **{counts['in_stock'] + counts['low_stock']}**."
        )
        embed.add_field(name="In Stock", value=str(counts["in_stock"]), inline=True)
        embed.add_field(name="Low Stock", value=str(counts["low_stock"]), inline=True)
        embed.add_field(name="Out of Stock", value=str(counts["out_of_stock"]), inline=True)
        embed.add_field(name="Unknown", value=str(counts["unknown"]), inline=True)

        available = [
            product for product in self.watchlist
            if product.get("last_status") in ("in_stock", "low_stock")
        ]
        if available:
            embed.add_field(
                name="Available Entries",
                value=self._format_report_lines(available[:8]),
                inline=False,
            )

        priority = [
            product for product in self.watchlist
            if product.get("priority") == "high" and product.get("last_status") in ("out_of_stock", "unknown")
        ]
        if priority:
            embed.add_field(
                name="High Priority Waiting",
                value=self._format_report_lines(priority[:8]),
                inline=False,
            )

        embed.set_footer(text="Use !check for live scrape, !watch_pack for SKU packs")
        return embed

    def _format_report_lines(self, products: list[dict]) -> str:
        lines = []
        for product in products:
            status = product.get("last_status", "unknown")
            emoji = STATUS_EMOJI.get(status, "⚪")
            sku = product.get("sku") or product.get("name") or "Unknown SKU"
            vendor = product.get("vendor") or SITE_NAMES.get(product.get("site"), product.get("site", "unknown"))
            price = f" · {product['last_price']}" if product.get("last_price") else ""
            lines.append(f"{emoji} [{sku}]({product['url']}) · {vendor}{price}")
        return "\n".join(lines)[:1024]

    # ── Polling loop ───────────────────────────────────────────────

    async def run_loop(self):
        logger.info("Polling loop started.")
        await self.bot.wait_until_ready()

        try:
            while not self.bot.is_closed():
                await self.check_all()
                await asyncio.sleep(10)  # Inner sleep; per-product interval is enforced below
        except asyncio.CancelledError:
            logger.info("Polling loop cancelled.")
            raise

    async def check_all(self, force: bool = False):
        if not self.watchlist:
            return

        channel = self.bot.get_channel(CONFIG["discord"]["channel_id"])
        if not channel:
            try:
                channel = await self.bot.fetch_channel(CONFIG["discord"]["channel_id"])
            except discord.DiscordException:
                logger.warning("Discord channel not found. Check DISCORD_CHANNEL_ID or config.py.")
                return

        for product in self.watchlist:
            url = product["url"]
            site = product["site"]
            interval = CONFIG["check_intervals"].get(site, 60)
            now = time.time()

            # Skip if checked recently (unless forced)
            if not force and (now - self._last_checked.get(url, 0)) < interval:
                continue

            self._last_checked[url] = now

            # Scrape with retries — if we get "unknown", try again before giving up
            result = None
            for attempt in range(1, SCRAPE_RETRIES + 1):
                try:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, scrape, url, site)
                except Exception as e:
                    logger.error(f"Scrape error for {url} (attempt {attempt}): {e}")
                    result = None
                    continue

                result = self._normalize_result(result)
                if result.get("status", "unknown") != "unknown":
                    break  # Got a definitive result, stop retrying

                if attempt < SCRAPE_RETRIES:
                    logger.info(f"[{site}] {product['name']}: got 'unknown', retrying ({attempt}/{SCRAPE_RETRIES})...")
                    await asyncio.sleep(RETRY_DELAY)

            if result is None:
                logger.error(f"All scrape attempts failed for {url}")
                continue

            new_status = result.get("status", "unknown")
            old_status = product.get("last_status", "unknown")

            # Update name if we got one
            if result.get("name") and result["name"] != url:
                product["name"] = result["name"]

            if result.get("price"):
                product["last_price"] = result["price"]

            if result.get("quantity") is not None:
                product["last_quantity"] = result["quantity"]

            # ── CRITICAL: Don't overwrite a known status with "unknown" ──
            # "unknown" means the scraper couldn't determine status (page load
            # failure, anti-bot block, parsing failure). Overwriting a known
            # state like "out_of_stock" with "unknown" destroys the state
            # machine and causes missed "back in stock" alerts.
            if new_status == "unknown":
                self._consecutive_unknowns[url] = self._consecutive_unknowns.get(url, 0) + 1
                count = self._consecutive_unknowns[url]
                if count <= 3 or count % 10 == 0:
                    logger.warning(
                        f"[{site}] {product['name']}: scraper returned 'unknown' "
                        f"({count} consecutive). Keeping previous status '{old_status}'."
                    )
                continue  # Skip status update and alert logic entirely

            # Reset consecutive unknown counter on successful scrape
            self._consecutive_unknowns[url] = 0

            logger.info(f"[{site}] {product['name']}: {old_status} → {new_status}")

            should_alert = self._should_alert(old_status, new_status)

            if should_alert:
                embed = self._build_embed(product, result, old_status, new_status)
                try:
                    await channel.send(embed=embed)
                    logger.info(f"🔔 Alert sent for {product['name']}: {old_status} → {new_status}")
                except discord.DiscordException as e:
                    logger.error(f"Failed to send alert for {product['name']}: {e}")

            # Update stored state (only reached for definitive statuses)
            product["last_status"] = new_status
            if new_status == "in_stock":
                product["last_seen_in_stock"] = time.strftime("%Y-%m-%d %H:%M:%S")

            self._save_watchlist()

    def _normalize_result(self, result: dict) -> dict:
        normalized = dict(result)
        quantity = normalized.get("quantity")
        threshold = CONFIG.get("low_stock_threshold", 5)

        if normalized.get("status") == "in_stock" and isinstance(quantity, int) and quantity <= threshold:
            normalized["status"] = "low_stock"

        return normalized

    def _should_alert(self, old: str, new: str) -> bool:
        alerts = CONFIG.get("alerts", {})

        if new == "unknown":
            return False

        # Back in stock (was OOS, now available)
        if alerts.get("back_in_stock") and old == "out_of_stock" and new in ("in_stock", "low_stock"):
            return True

        # Newly in stock (from unknown too)
        if alerts.get("in_stock") and new == "in_stock" and old != "in_stock":
            return True

        # Low stock warning
        if alerts.get("low_stock") and new == "low_stock" and old != "low_stock":
            return True

        return False

    def _build_embed(self, product: dict, result: dict, old_status: str, new_status: str) -> discord.Embed:
        site = product["site"]
        name = result.get("name") or product["name"]
        url = product["url"]
        price = result.get("price")
        quantity = result.get("quantity")

        emoji = STATUS_EMOJI.get(new_status, "⚪")
        color = STATUS_COLOR.get(new_status, 0x99AAB5)
        site_name = SITE_NAMES.get(site, site)

        if old_status == "out_of_stock" and new_status in ("in_stock", "low_stock"):
            title = f"{emoji} Back In Stock"
        elif new_status == "in_stock":
            title = f"{emoji} In Stock"
        elif new_status == "low_stock":
            title = f"{emoji} Low Stock"
        else:
            title = f"{emoji} Stock Update"

        embed = discord.Embed(
            title=title,
            description=f"**{name}**",
            url=url,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name="Tracker Network Stock Bot")
        embed.add_field(name="Retailer", value=site_name, inline=True)
        embed.add_field(name="Status", value=f"{emoji} {STATUS_LABEL.get(new_status, new_status)}", inline=True)

        if price:
            embed.add_field(name="Price", value=price, inline=True)

        if quantity is not None:
            embed.add_field(name="Qty Remaining", value=str(quantity), inline=True)

        if old_status != "unknown":
            embed.add_field(
                name="Previous Status",
                value=f"{STATUS_EMOJI.get(old_status, '⚪')} {STATUS_LABEL.get(old_status, old_status)}",
                inline=True,
            )

        embed.add_field(name="Action", value=f"[Open product page]({url})", inline=False)
        embed.set_footer(text=f"{site_name} stock monitor")

        return embed

    def build_test_embed(self, status: str = "in_stock") -> discord.Embed:
        product = self.watchlist[0] if self.watchlist else CONFIG["default_products"][0]
        result = {
            "name": product.get("name") or "Test Product",
            "price": "$79.00",
            "quantity": 3 if status == "low_stock" else None,
        }
        old_status = "out_of_stock" if status in ("in_stock", "low_stock") else "in_stock"
        embed = self._build_embed(product, result, old_status, status)
        embed.add_field(name="Test Mode", value="Simulated alert. Watchlist state not changed.", inline=False)
        return embed
