#!/usr/bin/env python3
"""
book_playwright.py

Browser-driven booking flow. Used by strike_playwright.py to complete
a real booking after a slot opens.

The flow:
  1. Visit listing page (establishes session)
  2. Visit slot detail page
  3. Fill voucher field, click "Gutschein einlösen"
  4. Click "Zum Warenkorb hinzufügen" (cart-add)
  5. Navigate to /checkout/start (auto-redirects to questions)
  6. Fill email, click "Fortfahren"
  7. On confirm page: tick terms checkbox, click "Anmeldung abschicken"
  8. Wait for redirect to /order/.../

Designed to be called from a strike script which has already pre-warmed
a browser session up to step 4 (cart-add button visible but slot may not
yet be open).
"""

from __future__ import annotations

import logging
import time
from playwright.sync_api import Page, TimeoutError as PWTimeout

log = logging.getLogger("book_pw")

BASE_URL = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/"


def prewarm_to_redeem(page: Page, slot_id: str, voucher: str,
                     timeout_ms: int = 15000) -> bool:
    """
    Steps 1-3: Land on the redeem page with cart button visible.
    The cart button may be either enabled (slot already open) or disabled
    (slot not yet released). Either is fine.

    Used by the strike script during pre-warm so each browser is ready
    to fire the cart-add the instant the slot opens.

    Returns True on success, False on any failure.
    """
    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.goto(f"{BASE_URL}{slot_id}/", wait_until="domcontentloaded",
                  timeout=timeout_ms)
        page.locator(
            'input[name="_voucher_code"], input[name="voucher"]'
        ).first.fill(voucher)
        page.get_by_role("button", name="Gutschein einlösen").click()
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return True
    except PWTimeout as e:
        log.error("Pre-warm failed for %s: %s", slot_id, e)
        return False
    except Exception as e:
        log.error("Pre-warm error for %s: %s", slot_id, e)
        return False


def cart_add_button_enabled(page) -> bool:
    """
    Reload the page and check if the cart-add button is currently enabled.

    Important: pretix pages are server-rendered. The button's disabled
    attribute reflects server state at the moment of page load and does
    not update via JavaScript. To detect the slot transitioning from
    "not yet open" to "open", we must reload the page on each poll.

    A human would refresh F5 until the button enables — this mimics that.
    """
    try:
        page.reload(wait_until="domcontentloaded", timeout=8000)
        btn = page.get_by_role(
            "button", name="Zum Warenkorb hinzufügen"
        ).first
        return btn.get_attribute("disabled") is None
    except Exception as e:
        log.warning("Reload+check failed: %s", e)
        return False


def fire_cart_add(page: Page, timeout_ms: int = 10000) -> bool:
    """
    Click the cart-add button. Caller should verify the button is enabled
    via cart_add_button_enabled() first to avoid waiting on a disabled
    element.
    """
    try:
        page.get_by_role(
            "button", name="Zum Warenkorb hinzufügen"
        ).first.click(timeout=timeout_ms)
        # Cart-add is async; give it a moment to register server-side
        time.sleep(1.5)
        return True
    except Exception as e:
        log.error("Cart-add click failed: %s", e)
        return False


def complete_checkout(page: Page, email: str,
                      timeout_ms: int = 30000) -> str | None:
    """
    Steps 5-8: Navigate to checkout, submit email, confirm terms,
    submit booking, wait for order page. Returns order URL on success.
    """
    try:
        # Step 5: navigate to checkout (redirects to questions)
        page.goto(f"{BASE_URL}checkout/start",
                  wait_until="domcontentloaded", timeout=timeout_ms)
        if "/checkout/questions" not in page.url:
            log.error("Expected questions page, got: %s", page.url)
            return None

        # Step 6: submit email
        page.locator('input[name="email"]').fill(email)
        page.get_by_role("button", name="Fortfahren").click()
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        if "/checkout/confirm" not in page.url:
            log.error("Expected confirm page, got: %s", page.url)
            return None

        # Step 7: tick terms checkbox and submit
        page.locator(
            'input[type="checkbox"][name^="confirm_"]'
        ).first.check()
        page.get_by_role(
            "button", name="Anmeldung abschicken"
        ).first.click()

        # Step 8: wait for redirect to order page (up to 60s; pretix
        # processes async and may take a bit)
        page.wait_for_url("**/order/**", timeout=60000)
        order_url = page.url
        log.info("Booking confirmed: %s", order_url)
        return order_url

    except PWTimeout as e:
        log.error("Checkout timeout: %s", e)
        return None
    except Exception as e:
        log.error("Checkout error: %s", e)
        return None
