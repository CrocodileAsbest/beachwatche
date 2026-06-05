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
a browser session up to step 4 (cart-add button may not yet be rendered
before release).
"""

from __future__ import annotations

import logging
import time
from playwright.sync_api import Page, TimeoutError as PWTimeout

log = logging.getLogger("book_pw")

BASE_URL = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/"

# HAR inspection showed the cart-add submit button has this stable id.
# This is faster and more direct than resolving by accessible role + text.
CART_ADD_SELECTOR = "#btn-add-to-cart"


# ---------------------------------------------------------------------------
# Pre-warm
# ---------------------------------------------------------------------------

def prewarm_to_redeem(
    page: Page,
    slot_id: str,
    voucher: str,
    timeout_ms: int = 15000,
) -> bool:
    """
    Steps 1-3: Land on the redeemed voucher page.

    Before release, pretix may not render the cart-add button at all. That is
    fine: the strike script should reload once at release time before calling
    fire_cart_add().

    Returns True on success, False on any failure.
    """
    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.goto(
            f"{BASE_URL}{slot_id}/",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

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


# ---------------------------------------------------------------------------
# Optional legacy helper
# ---------------------------------------------------------------------------

def cart_add_button_enabled(page: Page) -> bool:
    """
    Legacy polling helper.

    Reload the page and check if the cart-add button is present and enabled.
    The direct-strike script normally does not use this; it reloads once at
    release time and calls fire_cart_add() directly.
    """
    try:
        page.reload(wait_until="domcontentloaded", timeout=8000)
        btn = page.locator(CART_ADD_SELECTOR)
        if btn.count() == 0:
            return False
        return btn.first.get_attribute("disabled", timeout=1000) is None
    except Exception as e:
        log.warning("Reload+check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Cart add
# ---------------------------------------------------------------------------

def fire_cart_add(page: Page, timeout_ms: int = 1500) -> bool:
    """
    Click the cart-add button, then briefly confirm the cart was committed.

    Optimized for direct-strike use:
    - Uses the stable #btn-add-to-cart selector instead of get_by_role().
    - Uses a short click timeout because the strike script assumes availability
      at the release boundary.
    - Does not repeatedly reload while confirming. It checks the current page
      briefly, then performs at most one fallback reload.

    Returns True after confirmation, or True after confirmation timeout so that
    complete_checkout() can still try its own checkout/start retry backstop.
    Returns False only if the cart-add click itself failed.
    """
    CART_CONFIRM_TIMEOUT_S = 1.5
    CART_POLL_INTERVAL_S = 0.1

    confirm_markers = (
        "deinem warenkorb hinzugefügt",
        "dein warenkorb",
        "minuten für dich reserviert",
        "require_cookie=true",
    )

    try:
        page.locator(CART_ADD_SELECTOR).first.click(timeout=timeout_ms)
    except Exception as e:
        log.error("Cart-add click failed: %s", e)
        return False

    deadline = time.time() + CART_CONFIRM_TIMEOUT_S
    while time.time() < deadline:
        try:
            # The async cart-add flow may navigate to require_cookie=true. If so,
            # the cart was accepted and checkout can proceed.
            if "require_cookie=true" in page.url:
                log.info("Cart-add accepted; page reached require_cookie URL")
                return True

            content = page.content().lower()
            if any(marker in content for marker in confirm_markers):
                log.info("Cart commit confirmed on page")
                return True
        except Exception as e:
            log.warning("Cart-confirm read error: %s", e)

        time.sleep(CART_POLL_INTERVAL_S)

    # One fallback reload only. Avoid the previous repeated reload loop.
    try:
        page.reload(wait_until="domcontentloaded", timeout=5000)
        if "require_cookie=true" in page.url:
            log.info("Cart-add accepted after fallback reload")
            return True

        content = page.content().lower()
        if any(marker in content for marker in confirm_markers):
            log.info("Cart commit confirmed after fallback reload")
            return True
    except Exception as e:
        log.warning("Cart-confirm fallback reload failed: %s", e)

    log.warning(
        "Cart commit not confirmed within %.1fs; proceeding to checkout anyway "
        "(checkout has its own retry)",
        CART_CONFIRM_TIMEOUT_S,
    )
    return True
      # ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def complete_checkout(page: Page, email: str, timeout_ms: int = 30000) -> str | None:
    """
    Steps 5-8: Navigate to checkout, submit email, confirm terms, submit
    booking, wait for order page. Returns order URL on success.

    Robustness: pretix can bounce a freshly-carted session to
    /?require_cookie=true at checkout/start. Visiting/retrying checkout/start
    normally resolves that once the cart cookie/session is settled.
    """
    CHECKOUT_START_RETRIES = 5
    RETRY_WAIT_S = 0.8

    try:
        # Step 5: navigate into checkout. Retry on require_cookie or other
        # transient release-time redirects.
        landed_on_questions = False
        for attempt in range(1, CHECKOUT_START_RETRIES + 1):
            page.goto(
                f"{BASE_URL}checkout/start",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )

            if "/checkout/questions" in page.url:
                landed_on_questions = True
                break

            if "require_cookie" in page.url:
                log.warning(
                    "checkout/start bounced to require_cookie "
                    "(attempt %d/%d); retrying after %.1fs",
                    attempt,
                    CHECKOUT_START_RETRIES,
                    RETRY_WAIT_S,
                )
                try:
                    page.goto(
                        page.url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except Exception:
                    pass
                time.sleep(RETRY_WAIT_S)
                continue

            log.error("checkout/start landed on unexpected URL: %s", page.url)
            time.sleep(RETRY_WAIT_S)

        if not landed_on_questions:
            log.error(
                "Could not reach /checkout/questions/ after %d attempts; last URL: %s",
                CHECKOUT_START_RETRIES,
                page.url,
            )
            return None

        # Step 6: submit email.
        page.locator('input[name="email"]').fill(email)
        page.get_by_role("button", name="Fortfahren").click()
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

        if "/checkout/confirm" not in page.url:
            log.error("Expected confirm page, got: %s", page.url)
            return None

        # Step 7: tick terms checkbox and submit.
        page.locator('input[type="checkbox"][name^="confirm_"]').first.check()
        page.get_by_role("button", name="Anmeldung abschicken").first.click()

        # Step 8: wait for redirect to order page.
        page.wait_for_url("**/order/**", timeout=60000)
        order_url = page.url
        log.info("Booking confirmed: %s", order_url)
        return order_url

    except Exception as e:
        log.error("Checkout error: %s", e)
        return None
