"""
Scrapers for each supported retailer.
Each scraper returns a dict: { "status": str, "quantity": int|None, "price": str|None, "name": str|None }
Status values: "in_stock" | "out_of_stock" | "low_stock" | "unknown"
"""
import re
import logging
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# Rotate these to reduce blocking risk
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

_ua_index = 0

def _next_ua():
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua


def _get(url: str, timeout: int = 15) -> requests.Response | None:
    headers = {
        "User-Agent": _next_ua(),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.warning(f"GET failed for {url}: {e}")
        return None


# ─────────────────────────────────────────────
# Ubiquiti  (ui.com) — requires Playwright (JS SPA)
# ─────────────────────────────────────────────
def scrape_ubiquiti(url: str) -> dict:
    result = {"status": "unknown", "quantity": None, "price": None, "name": None}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_next_ua())
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Product name
            try:
                result["name"] = page.locator("h1").first.inner_text(timeout=5000).strip()
            except Exception:
                pass

            # Price
            try:
                price_el = page.locator("[data-testid='price'], .price, [class*='price']").first
                result["price"] = price_el.inner_text(timeout=3000).strip()
            except Exception:
                pass

            page_text = page.content().lower()

            # Out-of-stock signals
            oos_phrases = ["out of stock", "sold out", "notify me", "not available", "unavailable"]
            low_phrases = ["low stock", "limited stock", "only", "left in stock", "few left"]
            in_stock_phrases = ["add to cart", "add to bag", "buy now", "in stock"]

            if any(p in page_text for p in oos_phrases):
                result["status"] = "out_of_stock"
            elif any(p in page_text for p in low_phrases):
                result["status"] = "low_stock"
                qty_match = re.search(r'only\s+(\d+)\s+left', page_text)
                if qty_match:
                    result["quantity"] = int(qty_match.group(1))
            elif any(p in page_text for p in in_stock_phrases):
                result["status"] = "in_stock"

            browser.close()
    except Exception as e:
        logger.error(f"Ubiquiti scrape error: {e}")

    return result


# ─────────────────────────────────────────────
# Amazon (amazon.com)
# ─────────────────────────────────────────────
def scrape_amazon(url: str) -> dict:
    result = {"status": "unknown", "quantity": None, "price": None, "name": None}
    resp = _get(url)
    if not resp:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Product name
    title_el = soup.find(id="productTitle")
    if title_el:
        result["name"] = title_el.get_text(strip=True)

    # Price
    price_el = soup.select_one(".a-price .a-offscreen, #priceblock_ourprice, #priceblock_dealprice")
    if price_el:
        result["price"] = price_el.get_text(strip=True)

    # Availability
    avail_el = soup.find(id="availability")
    if avail_el:
        avail_text = avail_el.get_text(strip=True).lower()
        if "in stock" in avail_text:
            result["status"] = "in_stock"
            qty_match = re.search(r'only\s+(\d+)\s+left', avail_text)
            if qty_match:
                qty = int(qty_match.group(1))
                result["quantity"] = qty
                result["status"] = "low_stock"
        elif "out of stock" in avail_text or "unavailable" in avail_text:
            result["status"] = "out_of_stock"
        elif "limited" in avail_text or "few" in avail_text:
            result["status"] = "low_stock"
    else:
        # Fallback: check for Add to Cart button
        atc = soup.find(id="add-to-cart-button")
        if atc:
            result["status"] = "in_stock"

    return result


# ─────────────────────────────────────────────
# B&H Photo (bhphotovideo.com)
# ─────────────────────────────────────────────
def scrape_bh(url: str) -> dict:
    result = {"status": "unknown", "quantity": None, "price": None, "name": None}
    resp = _get(url)
    if not resp:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Product name
    title_el = soup.find("h1")
    if title_el:
        result["name"] = title_el.get_text(strip=True)

    # Price
    price_el = soup.select_one("[data-selenium='pricingPrice'], .price_1DPoW")
    if price_el:
        result["price"] = price_el.get_text(strip=True)

    page_text = soup.get_text().lower()

    if "add to cart" in page_text or "in stock" in page_text:
        result["status"] = "in_stock"
    elif "out of stock" in page_text or "back-order" in page_text or "backordered" in page_text:
        result["status"] = "out_of_stock"
    elif "low stock" in page_text or "limited availability" in page_text:
        result["status"] = "low_stock"

    # B&H sometimes shows quantity
    qty_match = re.search(r'(\d+)\s+in stock', page_text)
    if qty_match:
        result["quantity"] = int(qty_match.group(1))

    return result


# ─────────────────────────────────────────────
# Newegg (newegg.com)
# ─────────────────────────────────────────────
def scrape_newegg(url: str) -> dict:
    result = {"status": "unknown", "quantity": None, "price": None, "name": None}
    resp = _get(url)
    if not resp:
        return result

    soup = BeautifulSoup(resp.text, "html.parser")

    # Product name
    title_el = soup.find("h1", class_="product-title")
    if not title_el:
        title_el = soup.find("h1")
    if title_el:
        result["name"] = title_el.get_text(strip=True)

    # Price
    price_el = soup.select_one(".price-current strong, .price-current")
    if price_el:
        result["price"] = price_el.get_text(strip=True)

    page_text = soup.get_text().lower()

    # Newegg-specific stock indicators
    if "add to cart" in page_text:
        result["status"] = "in_stock"
        qty_match = re.search(r'(\d+)\s+(?:in stock|available)', page_text)
        if qty_match:
            qty = int(qty_match.group(1))
            result["quantity"] = qty
    elif "out of stock" in page_text or "sold out" in page_text:
        result["status"] = "out_of_stock"
    elif "limited supply" in page_text or "hurry" in page_text:
        result["status"] = "low_stock"

    return result


# ─────────────────────────────────────────────
# Router: pick scraper by site
# ─────────────────────────────────────────────
def scrape(url: str, site: str) -> dict:
    scrapers = {
        "ui.com":             scrape_ubiquiti,
        "amazon.com":         scrape_amazon,
        "bhphotovideo.com":   scrape_bh,
        "newegg.com":         scrape_newegg,
    }
    fn = scrapers.get(site)
    if fn:
        return fn(url)
    logger.warning(f"No scraper found for site: {site}")
    return {"status": "unknown", "quantity": None, "price": None, "name": None}
