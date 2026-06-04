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


# ===========================================================================
# REPLACE the existing fire_cart_add function in book_playwright.py with this.
# ===========================================================================

# ===========================================================================
# REPLACE fire_cart_add in book_playwright.py with this corrected version.
# (complete_checkout from the previous patch stays as-is -- it's fine.)
# ===========================================================================

def fire_cart_add(page, timeout_ms: int = 10000) -> bool:
    """
    Click the cart-add button, then confirm the cart is committed by checking
    the CURRENT page for pretix's success message. Caller should verify the
    button is enabled via cart_add_button_enabled() first.

    Cart-add on this pretix instance is an inline update: the URL stays on
    /redeem?... and the page re-renders to show "Die gewählten Produkte
    wurden deinem Warenkorb hinzugefügt." plus the cart widget. There is NO
    separate /cart/ page (it 404s). So we confirm by polling the current
    page content, not by navigating anywhere.

    Why confirm at all: at release-time load, the cart-add may take a moment
    to commit. Confirming before we navigate to checkout avoids the
    ?require_cookie=true bounce that happens when checkout/start fires against
    an uncommitted cart.

    IMPORTANT: this runs AFTER the click that claims the 5-minute hold, so it
    does NOT slow the competitive part of the strike. By the time we're here,
    the slot is already ours for 5 minutes.

    Returns True once the cart is confirmed (or after the click if confirmation
    times out -- complete_checkout has its own retry backstop).
    """
    CART_CONFIRM_TIMEOUT_S = 5.0
    CART_POLL_INTERVAL_S = 0.3

    # Strings that indicate a committed cart on the current page.
    CONFIRM_MARKERS = (
        "deinem warenkorb hinzugefügt",   # "...added to your cart"
        "dein warenkorb",                  # cart widget heading
        "minuten für dich reserviert",     # the hold-timer text
    )

    try:
        page.get_by_role(
            "button", name="Zum Warenkorb hinzufügen"
        ).first.click(timeout=timeout_ms)
    except Exception as e:
        log.error("Cart-add click failed: %s", e)
        return False

    # Confirm the cart committed by polling the CURRENT page content.
    # No navigation -- cart-add is an inline re-render.
    deadline = time.time() + CART_CONFIRM_TIMEOUT_S
    confirmed = False
    while time.time() < deadline:
        try:
            content = page.content().lower()
            if any(marker in content for marker in CONFIRM_MARKERS):
                confirmed = True
                break
        except Exception as e:
            log.warning("Cart-confirm read error: %s", e)
        time.sleep(CART_POLL_INTERVAL_S)
        # The page may still be settling after the click; a light reload
        # ensures we see the committed state. Only reload if not yet confirmed.
        try:
            page.reload(wait_until="domcontentloaded", timeout=8000)
        except Exception:
            pass

    if confirmed:
        log.info("Cart commit confirmed on page")
    else:
        log.warning("Cart commit not confirmed within %.1fs; proceeding to "
                    "checkout anyway (checkout has its own retry)",
                    CART_CONFIRM_TIMEOUT_S)

    return True
# ===========================================================================
# REPLACE the existing complete_checkout function in book_playwright.py
# with this.
# ===========================================================================

def complete_checkout(page, email: str, timeout_ms: int = 30000) -> str | None:
    """
    Steps 5-8: Navigate to checkout, submit email, confirm terms, submit
    booking, wait for order page. Returns order URL on success.

    Robustness: pretix intermittently bounces a freshly-carted session to
    /?require_cookie=true at the checkout/start step under release-time load.
    Visiting the require_cookie URL re-confirms the session cookie, so we
    retry checkout/start up to CHECKOUT_START_RETRIES times. This is a
    backstop in addition to the cart-commit confirmation in fire_cart_add.

    None of this slows the competitive race -- it runs after the slot is
    already held in our cart (5-minute window).
    """
    CHECKOUT_START_RETRIES = 5
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
                log.warning("checkout/start bounced to require_cookie "
                            "(attempt %d/%d); re-confirming cookie, retrying "
                            "after %.1fs", attempt, CHECKOUT_START_RETRIES,
                            RETRY_WAIT_S)
                # Re-fetch the require_cookie URL so pretix re-runs its
                # cookie confirmation, then loop to retry.
                try:
                    page.goto(page.url, wait_until="domcontentloaded",
                              timeout=timeout_ms)
                except Exception:
                    pass
                time.sleep(RETRY_WAIT_S)
                continue

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
