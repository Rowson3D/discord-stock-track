"""
Configuration for Stock Alert Bot.

Prefer environment variables for secrets and deployment-specific settings.
"""
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("STOCK_BOT_DATA_DIR", BASE_DIR / "data")).expanduser()
WATCHLIST_FILE = Path(
    os.getenv("STOCK_BOT_WATCHLIST_FILE", DATA_DIR / "watchlist.json")
).expanduser()


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


CONFIG = {
    "discord": {
        # Prefer DISCORD_BOT_TOKEN in production/systemd deployments.
        "token": os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE"),

        # Right-click a channel in Discord (with Developer Mode on) -> Copy ID
        "channel_id": _get_int_env("DISCORD_CHANNEL_ID", 123456789012345678),
    },

    "check_intervals": {
        # Seconds between checks per site. Amazon is higher to avoid bans.
        "ui.com":             60,
        "amazon.com":        300,   # 5 min recommended — Amazon blocks aggressive scrapers
        "bhphotovideo.com":   60,
        "newegg.com":         60,
    },

    "alerts": {
        "in_stock":      True,   # Alert when item comes IN stock
        "back_in_stock": True,   # Alert specifically when item was OOS and is now in stock
        "low_stock":     True,   # Alert when item shows "low stock" / limited quantity
    },

    "low_stock_threshold": 5,   # Quantity at or below this = "low stock" warning

    # Watchlist file — products are saved/loaded from here
    "watchlist_file": str(WATCHLIST_FILE),

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
