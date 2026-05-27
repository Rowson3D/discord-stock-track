"""
Configuration for Stock Alert Bot.

Prefer environment variables for secrets and deployment-specific settings.
"""
import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("STOCK_BOT_DATA_DIR", BASE_DIR / "data")).expanduser()
WATCHLIST_FILE = Path(
    os.getenv("STOCK_BOT_WATCHLIST_FILE", DATA_DIR / "watchlist.json")
).expanduser()
SUBSCRIBERS_FILE = Path(
    os.getenv("STOCK_BOT_SUBSCRIBERS_FILE", DATA_DIR / "subscribers.json")
).expanduser()
PRODUCT_PACKS_DIR = Path(
    os.getenv("STOCK_BOT_PRODUCTS_DIR", BASE_DIR / "config" / "products")
).expanduser()


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def _get_float_env(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default

    try:
        return float(value)
    except ValueError:
        return default


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_list_env(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


CONFIG = {
    "discord": {
        # Prefer DISCORD_BOT_TOKEN in production/systemd deployments.
        "token": os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE"),

        # Right-click a channel in Discord (with Developer Mode on) -> Copy ID
        "channel_id": _get_int_env("DISCORD_CHANNEL_ID", 123456789012345678),
        "mobile_push": _get_bool_env("DISCORD_MOBILE_PUSH_ENABLED", True),
        "alert_mention": os.getenv("DISCORD_ALERT_MENTION", "").strip(),
    },

    "sms": {
        "enabled": _get_bool_env("SMS_ENABLED", False),
        "provider": os.getenv("SMS_PROVIDER", "twilio").strip().lower(),
        "account_sid": os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
        "auth_token": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        "from_number": os.getenv("TWILIO_FROM_NUMBER", "").strip(),
        "to_numbers": _get_list_env("SMS_TO_NUMBERS"),
        "timeout_seconds": _get_int_env("SMS_TIMEOUT_SECONDS", 10),
    },

    "check_intervals": {
        # Seconds between checks per site. Amazon is higher to avoid bans.
        "ui.com":             60,
        "amazon.com":        300,   # 5 min recommended — Amazon blocks aggressive scrapers
        "bestbuy.com":       120,
        "bhphotovideo.com":   60,
        "newegg.com":         60,
    },

    "priority_interval_multipliers": {
        "high": 0.5,
        "normal": 1.0,
        "low": 1.5,
    },

    "poll_loop": {
        "min_sleep_seconds": _get_float_env("POLL_LOOP_MIN_SLEEP_SECONDS", 1.0) or 1.0,
        "max_sleep_seconds": _get_float_env("POLL_LOOP_MAX_SLEEP_SECONDS", 10.0) or 10.0,
    },

    "alerts": {
        "in_stock":      True,   # Alert when item comes IN stock
        "back_in_stock": True,   # Alert specifically when item was OOS and is now in stock
        "low_stock":     True,   # Alert when item shows "low stock" / limited quantity
    },

    "low_stock_threshold": 5,   # Quantity at or below this = "low stock" warning

    "checkout": {
        # Disabled by default. First supported flow is ui.com add-to-cart/review.
        "enabled": _get_bool_env("CHECKOUT_ENABLED", False),
        "mode": os.getenv("CHECKOUT_MODE", "review_only"),
        "headless": _get_bool_env("CHECKOUT_HEADLESS", True),
        "browser_profile_dir": os.getenv("CHECKOUT_BROWSER_PROFILE_DIR", str(DATA_DIR / "playwright-profile")),
        "allowed_approvers": _get_list_env("CHECKOUT_ALLOWED_APPROVERS"),
        "require_allowed_approvers": _get_bool_env("CHECKOUT_REQUIRE_ALLOWED_APPROVERS", True),
        "max_order_total": _get_float_env("CHECKOUT_MAX_ORDER_TOTAL"),
        "require_price_match": _get_bool_env("CHECKOUT_REQUIRE_PRICE_MATCH", True),
        "default_quantity": _get_int_env("CHECKOUT_DEFAULT_QUANTITY", 1),
        "default_max_quantity": _get_int_env("CHECKOUT_DEFAULT_MAX_QUANTITY", 1),
        "default_cooldown_hours": _get_int_env("CHECKOUT_DEFAULT_COOLDOWN_HOURS", 24),
        "test_timeout_seconds": _get_int_env("CHECKOUT_TEST_TIMEOUT_SECONDS", 90),
    },

    "message_cleanup": {
        "enabled": _get_bool_env("MESSAGE_CLEANUP_ENABLED", False),
        "ttl_minutes": _get_int_env("MESSAGE_CLEANUP_TTL_MINUTES", 60),
        "interval_minutes": _get_int_env("MESSAGE_CLEANUP_INTERVAL_MINUTES", 10),
        "scan_limit": _get_int_env("MESSAGE_CLEANUP_SCAN_LIMIT", 200),
        "max_deletes_per_run": _get_int_env("MESSAGE_CLEANUP_MAX_DELETES_PER_RUN", 25),
        "delete_delay_seconds": _get_float_env("MESSAGE_CLEANUP_DELETE_DELAY_SECONDS", 1.25),
        "delete_user_commands": _get_bool_env("MESSAGE_CLEANUP_DELETE_USER_COMMANDS", False),
    },

    # Watchlist file — products are saved/loaded from here
    "watchlist_file": str(WATCHLIST_FILE),

    # Subscriber file — Discord user IDs for global stock alert mentions
    "subscribers_file": str(SUBSCRIBERS_FILE),

    # Product pack directory — YAML packs are loaded by !packs and !watch_pack
    "product_packs_dir": str(PRODUCT_PACKS_DIR),

    # Pre-seeded products to monitor (you can also add via !watch command)
    "default_products": [
        {
            "url": "https://store.ui.com/us/en/products/utr",
            "name": "Ubiquiti UTR Travel Router",
            "site": "ui.com",
        },
        # Add more default products here:
        # {
        #     "url": "https://www.amazon.com/dp/ASIN_HERE",
        #     "name": "Product Name",
        #     "site": "amazon.com",
        # },
    ],
}
