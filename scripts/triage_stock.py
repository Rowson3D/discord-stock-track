"""
Triage stock scraping and alert logic for one product URL.

Usage:
    python scripts/triage_stock.py
    python scripts/triage_stock.py https://store.ui.com/us/en/products/utr --old-status out_of_stock
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import CONFIG
from monitor import StockMonitor, detect_site
from scrapers import scrape


def check_playwright() -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"installed": False, "launches": False, "error": str(exc)}

    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            exists = executable.exists()
            browser = playwright.chromium.launch(headless=True)
            browser.close()
            return {"installed": True, "browser_binary_exists": exists, "launches": True, "path": str(executable)}
    except Exception as exc:
        return {"installed": True, "launches": False, "error": str(exc)}


def main() -> int:
    default_product = CONFIG.get("default_products", [{}])[0]
    parser = argparse.ArgumentParser(description="Run stock scraper and alert triage for a URL.")
    parser.add_argument("url", nargs="?", default=default_product.get("url"))
    parser.add_argument("--site", default=None, help="Override detected site key, e.g. ui.com")
    parser.add_argument("--old-status", default="out_of_stock", choices=["unknown", "out_of_stock", "in_stock", "low_stock"])
    parser.add_argument("--simulate-new-status", choices=["unknown", "out_of_stock", "in_stock", "low_stock"])
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("No URL supplied and no default product configured.")

    site = args.site or detect_site(args.url)
    if not site:
        raise SystemExit(f"Unsupported site for URL: {args.url}")

    result = scrape(args.url, site)
    scraped_status = result.get("status", "unknown")
    new_status = args.simulate_new_status or scraped_status
    monitor = StockMonitor(bot=None)
    would_alert = monitor._should_alert(args.old_status, new_status)

    report = {
        "url": args.url,
        "site": site,
        "playwright": check_playwright() if site == "ui.com" else "not_required",
        "scrape_result": result,
        "alert_simulation": {
            "old_status": args.old_status,
            "scraped_status": scraped_status,
            "new_status": new_status,
            "would_send_discord_alert": would_alert,
        },
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())