"""
Guarded checkout helpers.

Current support: ui.com add-to-cart and checkout-review navigation. This module
does not enter card details, CVV, or click final place-order controls.
"""
import logging
import re
import time
from contextlib import suppress
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from config import CONFIG

logger = logging.getLogger(__name__)

SUPPORTED_SITES = {"ui.com"}
SUPPORTED_MODES = {"cart_only", "review_only"}


def checkout_enabled_for(product: dict) -> bool:
    checkout = _product_checkout(product)
    if CONFIG["checkout"].get("require_allowed_approvers", True) and not CONFIG["checkout"].get("allowed_approvers"):
        return False
    return bool(CONFIG["checkout"].get("enabled") and checkout.get("enabled", True))


def checkout_summary(product: dict) -> str:
    checkout = _product_checkout(product)
    product_enabled = bool(checkout.get("enabled", True))
    global_enabled = bool(CONFIG["checkout"].get("enabled"))
    approvers_ready = bool(CONFIG["checkout"].get("allowed_approvers")) or not CONFIG["checkout"].get("require_allowed_approvers", True)
    enabled = "on" if product_enabled else "off"
    global_state = "on" if global_enabled else "off"
    approver_state = "set" if approvers_ready else "missing"
    quantity = _bounded_quantity(product, {})
    cooldown = int(checkout.get("cooldown_hours", CONFIG["checkout"].get("default_cooldown_hours", 24)))
    max_unit = checkout.get("max_unit_price", "unset")
    max_order = checkout.get("max_order_total", CONFIG["checkout"].get("max_order_total") or "unset")
    return f"checkout `{enabled}` · global `{global_state}` · approvers `{approver_state}` · qty `{quantity}` · max unit `{max_unit}` · max order `{max_order}` · cooldown `{cooldown}h`"


def run_checkout(product: dict, scrape_result: dict | None = None, force: bool = False) -> dict:
    scrape_result = scrape_result or {}
    site = product.get("site")
    checkout = _product_checkout(product)
    mode = str(checkout.get("mode") or CONFIG["checkout"].get("mode") or "review_only").strip().lower()

    if site not in SUPPORTED_SITES:
        return _result("skipped", f"Checkout not supported for `{site}` yet.")

    if not checkout_enabled_for(product):
        return _result("disabled", "Checkout disabled by config or product settings.")

    if mode not in SUPPORTED_MODES:
        return _result("blocked", f"Checkout mode `{mode}` not supported. Use `cart_only` or `review_only`.")

    cooldown_message = _cooldown_block(product, checkout, force)
    if cooldown_message:
        return _result("cooldown", cooldown_message)

    price_message = _price_block(product, checkout, scrape_result)
    if price_message:
        return _result("blocked", price_message)

    quantity = _bounded_quantity(product, scrape_result)
    if quantity < 1:
        return _result("blocked", "Quantity resolved below 1.")

    if site == "ui.com":
        result = _run_ui_checkout(product, quantity, mode)
    else:
        result = _result("skipped", f"Checkout not implemented for `{site}`.")

    product["last_checkout_attempt_at"] = int(time.time())
    product["last_checkout_status"] = result["status"]
    product["last_checkout_message"] = result["message"]
    return result


def test_checkout(product: dict) -> dict:
    site = product.get("site")
    if site not in SUPPORTED_SITES:
        return _result("skipped", f"Checkout test not supported for `{site}` yet.")

    if site == "ui.com":
        return _test_ui_checkout(product)

    return _result("skipped", f"Checkout test not implemented for `{site}`.")


def _run_ui_checkout(product: dict, quantity: int, mode: str) -> dict:
    profile_dir = Path(CONFIG["checkout"]["browser_profile_dir"]).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = bool(CONFIG["checkout"].get("headless", True))
    browser = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                viewport={"width": 1366, "height": 900},
            )
            page = browser.new_page()
            page.goto(product["url"], wait_until="networkidle", timeout=45000)
            _dismiss_ui_overlays(page)
            _set_quantity_if_present(page, quantity)
            _click_add_to_cart(page)

            if mode == "cart_only":
                page.wait_for_timeout(1500)
                return _result("carted", f"Added `{quantity}` to cart. Review cart in saved browser profile.")

            _open_cart_or_checkout(page)
            return _result("review", f"Added `{quantity}` and opened checkout/cart review. Final order not placed.")
    except Exception as exc:
        logger.exception("Checkout failed for %s", product.get("url"))
        return _result("error", f"Checkout failed: {exc}")
    finally:
        if browser:
            with suppress(Exception):
                browser.close()


def _test_ui_checkout(product: dict) -> dict:
    profile_dir = Path(CONFIG["checkout"]["browser_profile_dir"]).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = bool(CONFIG["checkout"].get("headless", True))
    browser = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                viewport={"width": 1366, "height": 900},
            )
            page = browser.new_page()
            page.goto(product["url"], wait_until="networkidle", timeout=45000)
            _dismiss_ui_overlays(page)
            unavailable = _detect_unavailable_reason(page)
            if unavailable:
                return _result("unavailable", f"No-charge test reached product page. `{unavailable}` shown, so no add-to-cart button expected. Nothing clicked.")

            add_to_cart = _add_to_cart_locator(page)
            add_to_cart.wait_for(state="visible", timeout=15000)
            title = page.locator("h1").first.inner_text(timeout=5000).strip()
            return _result("ok", f"No-charge test passed. Add-to-cart visible for `{title or product.get('name')}`. Nothing clicked.")
    except Exception as exc:
        logger.exception("Checkout test failed for %s", product.get("url"))
        return _result("error", f"No-charge test failed: {exc}")
    finally:
        if browser:
            with suppress(Exception):
                browser.close()


def _dismiss_ui_overlays(page) -> None:
    for pattern in (r"accept", r"agree", r"close", r"no thanks", r"not now"):
        with suppress(Exception):
            page.get_by_role("button", name=re.compile(pattern, re.I)).first.click(timeout=1500)


def _set_quantity_if_present(page, quantity: int) -> None:
    if quantity <= 1:
        return

    selectors = [
        "input[type='number']",
        "input[name*='quantity' i]",
        "input[aria-label*='quantity' i]",
    ]
    for selector in selectors:
        with suppress(Exception):
            control = page.locator(selector).first
            control.fill(str(quantity), timeout=2000)
            return


def _click_add_to_cart(page) -> None:
    unavailable = _detect_unavailable_reason(page)
    if unavailable:
        raise RuntimeError(f"Product unavailable: {unavailable}")

    button = _add_to_cart_locator(page)
    button.click(timeout=15000)


def _add_to_cart_locator(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"add\s+to\s+(cart|bag)", re.I)).first,
        page.locator("button").filter(has_text=re.compile(r"add\s+to\s+(cart|bag)", re.I)).first,
        page.locator("[data-testid*='add' i], [aria-label*='add to cart' i], [aria-label*='add to bag' i]").first,
    ]
    return candidates[0].or_(candidates[1]).or_(candidates[2]).first


def _detect_unavailable_reason(page) -> str | None:
    text = page.locator("body").inner_text(timeout=5000).lower()
    for phrase in (
        "sold out",
        "out of stock",
        "notify me",
        "subscribe to back in stock",
        "back in stock emails",
        "coming soon",
        "unavailable",
        "not available",
    ):
        if phrase in text:
            return phrase
    return None


def _open_cart_or_checkout(page) -> None:
    patterns = (r"checkout", r"view cart", r"cart")
    for pattern in patterns:
        for role in ("button", "link"):
            with suppress(PlaywrightTimeoutError, Exception):
                page.get_by_role(role, name=re.compile(pattern, re.I)).first.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=15000)
                return

    with suppress(Exception):
        page.goto("https://store.ui.com/us/en/cart", wait_until="networkidle", timeout=15000)
        return


def _product_checkout(product: dict) -> dict:
    checkout = dict(product.get("checkout") or {})
    checkout.setdefault("quantity", CONFIG["checkout"].get("default_quantity", 1))
    checkout.setdefault("max_quantity", CONFIG["checkout"].get("default_max_quantity", 1))
    checkout.setdefault("cooldown_hours", CONFIG["checkout"].get("default_cooldown_hours", 24))
    return checkout


def _bounded_quantity(product: dict, scrape_result: dict) -> int:
    checkout = _product_checkout(product)
    quantity = int(checkout.get("quantity") or CONFIG["checkout"].get("default_quantity", 1))
    max_quantity = int(checkout.get("max_quantity") or CONFIG["checkout"].get("default_max_quantity", 1))
    quantity = min(quantity, max_quantity)
    available = scrape_result.get("quantity")
    if isinstance(available, int):
        quantity = min(quantity, available)
    return max(quantity, 0)


def _cooldown_block(product: dict, checkout: dict, force: bool) -> str | None:
    if force:
        return None

    last_attempt = product.get("last_checkout_attempt_at")
    if not last_attempt:
        return None

    cooldown_hours = int(checkout.get("cooldown_hours", CONFIG["checkout"].get("default_cooldown_hours", 24)))
    remaining = (int(last_attempt) + cooldown_hours * 3600) - int(time.time())
    if remaining <= 0:
        return None
    minutes = max(1, remaining // 60)
    return f"Checkout cooldown active for {minutes} more minutes."


def _price_block(product: dict, checkout: dict, scrape_result: dict) -> str | None:
    require_price = bool(CONFIG["checkout"].get("require_price_match", True))
    unit_price = _parse_price(scrape_result.get("price") or product.get("last_price"))
    max_unit = _to_float(checkout.get("max_unit_price"))
    max_order = _to_float(checkout.get("max_order_total") or CONFIG["checkout"].get("max_order_total"))
    quantity = _bounded_quantity(product, scrape_result)

    if require_price and unit_price is None and (max_unit is not None or max_order is not None):
        return "Price required but scraper did not return parseable price."

    if unit_price is not None and max_unit is not None and unit_price > max_unit:
        return f"Unit price ${unit_price:.2f} exceeds max ${max_unit:.2f}."

    if unit_price is not None and max_order is not None and unit_price * quantity > max_order:
        return f"Estimated subtotal ${unit_price * quantity:.2f} exceeds max order ${max_order:.2f}."

    return None


def _parse_price(value) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:,\d{3})*(?:\.\d{2})?", str(value))
    if not match:
        return None
    return float(match.group(0).replace(",", ""))


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    with suppress(TypeError, ValueError):
        return float(value)
    return None


def _result(status: str, message: str) -> dict:
    return {"status": status, "message": message}