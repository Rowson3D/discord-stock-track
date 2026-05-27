"""
StockMonitor: manages the watchlist, polling loop, and Discord alert dispatch.
"""
import json
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
from pathlib import Path

import discord
import requests
import yaml
from config import CONFIG
from checkout import checkout_enabled_for, checkout_summary, run_checkout, test_checkout
from scrapers import scrape

logger = logging.getLogger(__name__)

# ── Logging setup (runs once at import time) ──────────────────────────────────
def _configure_logging() -> None:
    """Configure root logger with console + optional rotating file handler."""
    _log_level_name = os.getenv("STOCK_BOT_LOG_LEVEL", "INFO").upper()
    _log_level = getattr(logging, _log_level_name, logging.INFO)
    _log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    _handlers: list[logging.Handler] = [logging.StreamHandler()]

    _log_file = os.getenv("STOCK_BOT_LOG_FILE", "").strip()
    if _log_file:
        Path(_log_file).parent.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            _log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        _handlers.append(_file_handler)

    logging.basicConfig(level=_log_level, format=_log_fmt, handlers=_handlers)

    # Suppress noisy discord.py gateway noise at INFO; keep WARNING+
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


_configure_logging()

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
    "bestbuy.com":       "Best Buy",
    "bhphotovideo.com":  "B&H Photo",
    "newegg.com":        "Newegg",
}


def detect_site(url: str) -> str | None:
    """Return the canonical site key from a URL, or None if unsupported."""
    host = urlparse(url.strip()).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    for key in ["ui.com", "amazon.com", "bestbuy.com", "bhphotovideo.com", "newegg.com"]:
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
        self.subscribers_file = Path(CONFIG["subscribers_file"]).expanduser().resolve()
        self.product_packs_dir = Path(CONFIG["product_packs_dir"]).expanduser().resolve()
        self.subscribers: set[str] = set()
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

    def load_subscribers(self):
        if not self.subscribers_file.exists():
            self.subscribers = set()
            return

        try:
            with self.subscribers_file.open(encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                user_ids = payload.get("user_ids", [])
            elif isinstance(payload, list):
                user_ids = payload
            else:
                user_ids = []
            self.subscribers = {str(user_id).strip() for user_id in user_ids if str(user_id).strip()}
        except Exception as e:
            logger.error(f"Failed to load subscribers: {e}")
            self.subscribers = set()

    def _save_subscribers(self):
        try:
            self.subscribers_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.subscribers_file.with_suffix(f"{self.subscribers_file.suffix}.tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump({"user_ids": sorted(self.subscribers)}, f, indent=2)
            temp_file.replace(self.subscribers_file)
        except Exception as e:
            logger.error(f"Failed to save subscribers: {e}")

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
                f"ui.com, amazon.com, bestbuy.com, bhphotovideo.com, newegg.com"
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
                        "checkout": vendor.get("checkout") or product.get("checkout") or {},
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

    def remove_product_by_index(self, index: int) -> str:
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"

        product = self.watchlist.pop(index - 1)
        self._save_watchlist()
        label = product.get("sku") or product.get("name") or product["url"]
        return f"🗑️ Removed watch `{index}`: **{label}**"

    def remove_products_by_pack(self, pack_id: str) -> str:
        pack_id = pack_id.strip().lower()
        before = len(self.watchlist)
        self.watchlist = [p for p in self.watchlist if p.get("pack", "").lower() != pack_id]
        removed = before - len(self.watchlist)
        if removed:
            self._save_watchlist()
            return f"🗑️ Removed {removed} watch entries from pack `{pack_id}`."
        return f"⚠️ No watch entries found for pack `{pack_id}`."

    def remove_products_by_sku(self, sku: str) -> str:
        sku = sku.strip().lower()
        before = len(self.watchlist)
        self.watchlist = [p for p in self.watchlist if str(p.get("sku", "")).lower() != sku]
        removed = before - len(self.watchlist)
        if removed:
            self._save_watchlist()
            return f"🗑️ Removed {removed} watch entries for SKU `{sku.upper()}`."
        return f"⚠️ No watch entries found for SKU `{sku.upper()}`."

    def clear_watchlist(self) -> str:
        removed = len(self.watchlist)
        self.watchlist = []
        self._save_watchlist()
        return f"🗑️ Cleared watchlist. Removed {removed} entries."

    def get_watchlist(self) -> list[dict]:
        return self.watchlist

    def subscribe_user(self, user_id: int | str) -> str:
        user_id_str = str(user_id).strip()
        if not user_id_str:
            return "⚠️ Invalid user ID."
        if user_id_str in self.subscribers:
            return "⚠️ You are already subscribed to stock alerts."

        self.subscribers.add(user_id_str)
        self._save_subscribers()
        return "✅ You are now subscribed to stock alerts."

    def unsubscribe_user(self, user_id: int | str) -> str:
        user_id_str = str(user_id).strip()
        if user_id_str not in self.subscribers:
            return "⚠️ You are not subscribed to stock alerts."

        self.subscribers.remove(user_id_str)
        self._save_subscribers()
        return "✅ You are no longer subscribed to stock alerts."

    def notify_user(self, index: int, user_id: int | str) -> str:
        """Subscribe a user to alerts for a specific watchlist entry."""
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"
        user_id_str = str(user_id).strip()
        product = self.watchlist[index - 1]
        watchers: list = product.setdefault("watchers", [])
        if user_id_str in watchers:
            name = product.get("sku") or product.get("name") or product["url"]
            return f"⚠️ You are already watching alerts for **{name}**."
        watchers.append(user_id_str)
        self._save_watchlist()
        name = product.get("sku") or product.get("name") or product["url"]
        return f"✅ You'll be mentioned when **{name}** changes stock status. Use `!unnotify {index}` to stop."

    def unnotify_user(self, index: int, user_id: int | str) -> str:
        """Remove a user from per-product alerts for a specific watchlist entry."""
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"
        user_id_str = str(user_id).strip()
        product = self.watchlist[index - 1]
        watchers: list = product.get("watchers") or []
        if user_id_str not in watchers:
            name = product.get("sku") or product.get("name") or product["url"]
            return f"⚠️ You are not watching alerts for **{name}**."
        product["watchers"] = [w for w in watchers if w != user_id_str]
        self._save_watchlist()
        name = product.get("sku") or product.get("name") or product["url"]
        return f"✅ You will no longer be mentioned for **{name}**."

    def build_subscribers_message(self) -> str:
        if not self.subscribers:
            return "📭 No subscribed users."

        mentions = " ".join(f"<@{user_id}>" for user_id in sorted(self.subscribers))
        return f"🔔 Subscribers ({len(self.subscribers)}): {mentions}"

    def configure_checkout(self, index: int, enabled: bool, quantity: int | None = None, max_quantity: int | None = None, max_unit_price: float | None = None, max_order_total: float | None = None) -> str:
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"

        product = self.watchlist[index - 1]
        checkout = dict(product.get("checkout") or {})
        checkout["enabled"] = enabled
        if quantity is not None:
            checkout["quantity"] = max(1, quantity)
        if max_quantity is not None:
            checkout["max_quantity"] = max(1, max_quantity)
        if max_unit_price is not None:
            checkout["max_unit_price"] = max_unit_price
        if max_order_total is not None:
            checkout["max_order_total"] = max_order_total

        product["checkout"] = checkout
        self._save_watchlist()
        return f"✅ Updated checkout for watch `{index}`: {checkout_summary(product)}"

    async def checkout_product_by_index(self, index: int, force: bool = False) -> str:
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"

        product = self.watchlist[index - 1]
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, run_checkout, product, {}, force)
        self._save_watchlist()
        return f"{result['status']}: {result['message']}"

    async def test_checkout_by_index(self, index: int, depth: str = "page") -> str:
        if index < 1 or index > len(self.watchlist):
            return f"⚠️ Watch index out of range: {index}"

        product = self.watchlist[index - 1]
        loop = asyncio.get_running_loop()
        timeout = max(15, int(CONFIG.get("checkout", {}).get("test_timeout_seconds", 90)))
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, test_checkout, product, depth),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"timeout: Checkout test exceeded `{timeout}` seconds. Browser may still finish in background. Try `!checkout_test {index} page` or raise `CHECKOUT_TEST_TIMEOUT_SECONDS`."
        return f"{result['status']}: {result['message']}"

    def build_watchlist_embeds(self) -> list[discord.Embed]:
        if not self.watchlist:
            embed = discord.Embed(title="Watchlist", description="📭 No products currently being monitored.", color=0x99AAB5)
            return [embed]

        embeds: list[discord.Embed] = []
        chunk_size = 8
        total = len(self.watchlist)
        counts = {"in_stock": 0, "low_stock": 0, "out_of_stock": 0, "unknown": 0}
        for product in self.watchlist:
            counts[product.get("last_status", "unknown") if product.get("last_status", "unknown") in counts else "unknown"] += 1

        for start in range(0, total, chunk_size):
            page = start // chunk_size + 1
            embed = discord.Embed(
                title="Watchlist",
                description=(
                    f"Entries: **{total}** · In Stock: **{counts['in_stock']}** · "
                    f"Low Stock: **{counts['low_stock']}** · Out: **{counts['out_of_stock']}** · Unknown: **{counts['unknown']}**"
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_author(name="Tracker Network Stock Bot")

            for idx, product in enumerate(self.watchlist[start:start + chunk_size], start + 1):
                status = product.get("last_status", "unknown")
                emoji = STATUS_EMOJI.get(status, "⚪")
                sku = product.get("sku") or product.get("name") or product["url"]
                vendor = product.get("vendor") or SITE_NAMES.get(product.get("site"), product.get("site", "unknown"))
                pack = f" · pack `{product['pack']}`" if product.get("pack") else ""
                price = f" · {product['last_price']}" if product.get("last_price") else ""
                embed.add_field(
                    name=f"{idx}. {emoji} {sku}",
                    value=(
                        f"Vendor: `{vendor}` · Site: `{product['site']}`{pack}{price}\n"
                        f"[Open product page]({product['url']})\n"
                        f"{checkout_summary(product)}"
                    ),
                    inline=False,
                )

            embed.set_footer(text=f"Page {page}/{(total + chunk_size - 1) // chunk_size} · Remove: !unwatch <index>|<url>")
            embeds.append(embed)

        return embeds

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

        _check_count = 0
        _last_heartbeat = time.monotonic()
        _HEARTBEAT_INTERVAL = 3600  # log alive message every hour

        try:
            while not self.bot.is_closed():
                try:
                    await self.check_all()
                    _check_count += 1
                except Exception as e:
                    logger.exception(
                        f"Unexpected error in polling loop (will retry in 30s): {e}"
                    )
                    await asyncio.sleep(30)
                    continue

                now = time.monotonic()
                if now - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                    logger.info(
                        f"Polling loop alive — {_check_count} check cycles completed."
                    )
                    _last_heartbeat = now

                await asyncio.sleep(self._get_next_poll_delay())
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

        due_products = sorted(self.watchlist, key=self._product_priority_rank)

        for product in due_products:
            url = product["url"]
            site = product["site"]
            interval = self._get_effective_interval(product)
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
                message = self._build_alert_message(product, result, new_status)
                try:
                    await channel.send(content=message, embed=embed)
                    logger.info(f"🔔 Alert sent for {product['name']}: {old_status} → {new_status}")
                except discord.DiscordException as e:
                    logger.error(f"Failed to send alert for {product['name']}: {e}")

                try:
                    await asyncio.to_thread(self._send_sms_alert, product, result, old_status, new_status)
                except Exception as e:
                    logger.error(f"Failed to send SMS alert for {product['name']}: {e}")

                if checkout_enabled_for(product):
                    checkout_result = await loop.run_in_executor(None, run_checkout, product, result, False)
                    try:
                        await channel.send(f"Checkout `{checkout_result['status']}` for **{product['name']}**: {checkout_result['message']}")
                    except discord.DiscordException as e:
                        logger.error(f"Failed to send checkout result for {product['name']}: {e}")

            # Update stored state (only reached for definitive statuses)
            product["last_status"] = new_status
            if new_status == "in_stock":
                product["last_seen_in_stock"] = time.strftime("%Y-%m-%d %H:%M:%S")

            self._save_watchlist()

    def _get_effective_interval(self, product: dict) -> float:
        site = product["site"]
        base_interval = CONFIG["check_intervals"].get(site, 60)
        priority = str(product.get("priority", "normal")).lower()
        multiplier = CONFIG.get("priority_interval_multipliers", {}).get(priority, 1.0)
        return max(1.0, float(base_interval) * float(multiplier))

    def _product_priority_rank(self, product: dict) -> tuple[int, float]:
        priority = str(product.get("priority", "normal")).lower()
        ranks = {"high": 0, "normal": 1, "low": 2}
        last_checked = self._last_checked.get(product["url"], 0.0)
        return (ranks.get(priority, 1), last_checked)

    def _get_next_poll_delay(self) -> float:
        poll_config = CONFIG.get("poll_loop", {})
        min_sleep = max(0.25, float(poll_config.get("min_sleep_seconds", 1.0)))
        max_sleep = max(min_sleep, float(poll_config.get("max_sleep_seconds", 10.0)))

        if not self.watchlist:
            return max_sleep

        now = time.time()
        next_due_in = max_sleep
        for product in self.watchlist:
            interval = self._get_effective_interval(product)
            elapsed = now - self._last_checked.get(product["url"], 0.0)
            due_in = max(0.0, interval - elapsed)
            next_due_in = min(next_due_in, due_in)

        return min(max_sleep, max(min_sleep, next_due_in))

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

    def _send_sms_alert(self, product: dict, result: dict, old_status: str, new_status: str):
        sms_config = CONFIG.get("sms", {})
        if not sms_config.get("enabled"):
            return

        provider = sms_config.get("provider", "twilio")
        if provider != "twilio":
            logger.warning(f"SMS disabled: unsupported provider '{provider}'.")
            return

        account_sid = sms_config.get("account_sid")
        auth_token = sms_config.get("auth_token")
        from_number = sms_config.get("from_number")
        to_numbers = sms_config.get("to_numbers") or []
        timeout_seconds = sms_config.get("timeout_seconds", 10)

        if not all([account_sid, auth_token, from_number, to_numbers]):
            logger.warning("SMS enabled but Twilio config is incomplete. Skipping SMS alert.")
            return

        body = self._build_sms_message(product, result, old_status, new_status)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

        for to_number in to_numbers:
            response = requests.post(
                url,
                auth=(account_sid, auth_token),
                data={
                    "From": from_number,
                    "To": to_number,
                    "Body": body,
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()

        logger.info(f"SMS alert sent for {product['name']} to {len(to_numbers)} recipient(s).")

    def _build_sms_message(self, product: dict, result: dict, old_status: str, new_status: str) -> str:
        name = result.get("name") or product["name"]
        site_name = SITE_NAMES.get(product["site"], product["site"])
        price = result.get("price")
        quantity = result.get("quantity")
        status_label = STATUS_LABEL.get(new_status, new_status)
        previous_label = STATUS_LABEL.get(old_status, old_status)

        parts = [f"{status_label}: {name}", site_name]
        if old_status != "unknown":
            parts.append(f"was {previous_label}")
        if price:
            parts.append(f"price {price}")
        if quantity is not None:
            parts.append(f"qty {quantity}")
        parts.append(product["url"])
        return " | ".join(parts)

    def _build_alert_message(self, product: dict, result: dict, new_status: str) -> str | None:
        discord_config = CONFIG.get("discord", {})
        mentions = self._get_alert_mentions(product)
        if not discord_config.get("mobile_push", True) and not mentions:
            return None

        name = result.get("name") or product["name"]
        site_name = SITE_NAMES.get(product["site"], product["site"])
        status_label = STATUS_LABEL.get(new_status, new_status)
        parts = []
        if mentions:
            parts.append(" ".join(mentions))
        if discord_config.get("mobile_push", True):
            parts.append(f"{status_label}: {name} at {site_name}")
        return " | ".join(parts) if parts else None

    def _get_alert_mentions(self, product: dict | None = None) -> list[str]:
        discord_config = CONFIG.get("discord", {})
        mentions: list[str] = []
        global_mention = discord_config.get("alert_mention", "")
        if global_mention:
            mentions.append(global_mention)
        mentions.extend(f"<@{user_id}>" for user_id in sorted(self.subscribers))
        # Per-product watchers
        if product:
            mentions.extend(f"<@{user_id}>" for user_id in sorted(product.get("watchers") or []))

        unique_mentions: list[str] = []
        seen: set[str] = set()
        for mention in mentions:
            if mention not in seen:
                unique_mentions.append(mention)
                seen.add(mention)
        return unique_mentions

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
        embed.add_field(name="Checkout", value=checkout_summary(product), inline=False)
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
