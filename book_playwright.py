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
    Reload the page and check if the cart-add button is present and enabled.

    Important: pretix does NOT render a disabled button for not-yet-released
    slots — the button is absent entirely from the page. We must check
    existence first (with count(), which is instant) before checking the
    disabled attribute.

    Also: pages are server-rendered. The button state reflects server state
    at page-load time, not via JavaScript. To detect the slot opening, we
    must reload on each poll. A human would refresh F5 until the button
    appears — this mimics that.
    """
    try:
        page.reload(wait_until="domcontentloaded", timeout=8000)
        btn = page.get_by_role("button", name="Zum Warenkorb hinzufügen")
        if btn.count() == 0:
            return False  # Slot not yet released
        return btn.first.get_attribute("disabled", timeout=1000) is None
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


def complete_checkout(page, email: str, timeout_ms: int = 30000) -> str | None:
    """
    Steps 5-8: Navigate to checkout, submit email, confirm terms, submit
    booking, wait for order page. Returns order URL on success.

    Robustness: pretix intermittently bounces a freshly-carted session to
    /?require_cookie=true at the checkout/start step -- especially under
    load at the 20:00 release moment, when the cart-add may not have fully
    committed server-side before we navigate. Visiting the require_cookie
    URL itself re-confirms the session cookie, so we retry checkout/start
    up to CHECKOUT_START_RETRIES times, with a short wait between attempts.
    """
    CHECKOUT_START_RETRIES = 4
    RETRY_WAIT_S = 0.8

    try:
        # Step 5: navigate into checkout. Retry on require_cookie bounce.
        landed_on_questions = False
        for attempt in range(1, CHECKOUT_START_RETRIES + 1):
            page.goto(f"{BASE_URL}checkout/start",
                      wait_until="domcontentloaded", timeout=timeout_ms)

            if "/checkout/questions" in page.url:
                landed_on_questions = True
                break

            if "require_cookie" in page.url:
                # The bounce itself set/confirmed the cookie. Wait briefly
                # (lets any async cart-add finish committing), then retry.
                log.warning("checkout/start bounced to require_cookie "
                            "(attempt %d/%d); retrying after %.1fs",
                            attempt, CHECKOUT_START_RETRIES, RETRY_WAIT_S)
                # Re-fetch the require_cookie URL explicitly so pretix
                # re-runs its cookie confirmation, then loop to retry.
                try:
                    page.goto(page.url, wait_until="domcontentloaded",
                              timeout=timeout_ms)
                except Exception:
                    pass
                time.sleep(RETRY_WAIT_S)
                continue

            # Some other unexpected URL.
            log.error("checkout/start landed on unexpected URL: %s", page.url)
            time.sleep(RETRY_WAIT_S)

        if not landed_on_questions:
            log.error("Could not reach /checkout/questions/ after %d attempts; "
                      "last URL: %s", CHECKOUT_START_RETRIES, page.url)
            return None

        # Step 6: submit email
        page.locator('input[name="email"]').fill(email)
        page.get_by_role("button", name="Fortfahren").click()
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        if "/checkout/confirm" not in page.url:
            log.error("Expected confirm page, got: %s", page.url)
            return None

        # Step 7: tick terms checkbox and submit
        page.locator('input[type="checkbox"][name^="confirm_"]').first.check()
        page.get_by_role("button", name="Anmeldung abschicken").first.click()

        # Step 8: wait for redirect to order page
        page.wait_for_url("**/order/**", timeout=60000)
        order_url = page.url
        log.info("Booking confirmed: %s", order_url)
        return order_url

    except Exception as e:
        log.error("Checkout error: %s", e)
        return None
