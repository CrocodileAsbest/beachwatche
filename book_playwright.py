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

from urllib.parse import parse_qsl


SENSITIVE_FIELD_HINTS = (
    "voucher",
    "csrf",
    "token",
    "secret",
    "password",
)


def _redact_form_value(name: str, value: str) -> str:
    lower = name.lower()
    if any(hint in lower for hint in SENSITIVE_FIELD_HINTS):
        if not value:
            return ""
        return f"<redacted:{len(value)} chars>"
    return value


def log_cart_add_form(page: Page) -> None:
    """
    Log the rendered cart-add form before clicking.

    This helps you inspect the exact POST action and form fields pretix expects.
    Sensitive-looking fields are redacted.
    """
    try:
        form_info = page.evaluate(
            """
            () => {
              const button = [...document.querySelectorAll("button")]
                .find(b => (b.textContent || "").includes("Zum Warenkorb hinzufügen"));

              if (!button) {
                return { found: false, reason: "cart-add button not found" };
              }

              const form = button.closest("form");
              if (!form) {
                return { found: false, reason: "button has no parent form" };
              }

              const fields = [];
              for (const el of form.querySelectorAll("input, select, textarea, button")) {
                const name = el.getAttribute("name") || "";
                if (!name) continue;

                let value = "";
                if (el.tagName === "SELECT") {
                  value = el.value || "";
                } else if (el.type === "checkbox" || el.type === "radio") {
                  if (!el.checked) continue;
                  value = el.value || "on";
                } else {
                  value = el.value || "";
                }

                fields.push({
                  tag: el.tagName.toLowerCase(),
                  type: el.getAttribute("type") || "",
                  name,
                  value,
                });
              }

              return {
                found: true,
                action: form.action,
                method: form.method,
                field_count: fields.length,
                fields,
              };
            }
            """
        )

        if not form_info.get("found"):
            log.warning("Cart-add form not found: %s", form_info)
            return

        log.info(
            "Cart-add form: method=%s action=%s fields=%d",
            form_info.get("method"),
            form_info.get("action"),
            form_info.get("field_count"),
        )

        for field in form_info.get("fields", []):
            name = field.get("name", "")
            value = _redact_form_value(name, field.get("value", ""))
            log.info(
                "Cart-add form field: <%s type=%s> %s=%r",
                field.get("tag"),
                field.get("type"),
                name,
                value,
            )

    except Exception as e:
        log.warning("Could not log cart-add form: %s", e)

def arm_cart_add_request_logger(page: Page) -> None:
    """
    Log the actual POST request sent by the cart-add click.

    This captures what Playwright really submitted, not just what was present
    in the DOM before clicking.
    """
    def _on_request(request):
        try:
            if request.method != "POST":
                return

            url = request.url
            post_data = request.post_data or ""

            # Keep this broad enough to catch pretix cart/redeem submissions.
            if "cart" not in url and "redeem" not in url and "buchung-beachplatz" not in url:
                return

            log.info("Outgoing POST: %s", url)

            content_type = request.headers.get("content-type", "")
            log.info("Outgoing POST content-type: %s", content_type)

            if post_data:
                for key, value in parse_qsl(post_data, keep_blank_values=True):
                    log.info(
                        "Outgoing POST field: %s=%r",
                        key,
                        _redact_form_value(key, value),
                    )
            else:
                log.info("Outgoing POST had no readable post_data")

        except Exception as e:
            log.warning("Could not inspect outgoing request: %s", e)

    page.on("request", _on_request)



CART_ADD_SELECTOR = 'button:has-text("Zum Warenkorb hinzufügen")'

def fire_cart_add(page, timeout_ms: int = 1500) -> bool:
    CART_CONFIRM_TIMEOUT_S = 1.5
    CART_POLL_INTERVAL_S = 0.1

    log_cart_add_form(page)
    arm_cart_add_request_logger(page)

    CONFIRM_MARKERS = (
        "deinem warenkorb hinzugefügt",
        "dein warenkorb",
        "minuten für dich reserviert",
    )

    try:
        page.locator(CART_ADD_SELECTOR).first.click(timeout=timeout_ms)
    except Exception as e:
        log.error("Cart-add click failed: %s", e)
        return False

    deadline = time.time() + CART_CONFIRM_TIMEOUT_S
    while time.time() < deadline:
        try:
            content = page.content().lower()
            if any(marker in content for marker in CONFIRM_MARKERS):
                log.info("Cart commit confirmed on page")
                return True
        except Exception as e:
            log.warning("Cart-confirm read error: %s", e)

        time.sleep(CART_POLL_INTERVAL_S)

    # One fallback reload only, not repeated reloads.
    try:
        page.reload(wait_until="domcontentloaded", timeout=3000)
        content = page.content().lower()
        if any(marker in content for marker in CONFIRM_MARKERS):
            log.info("Cart commit confirmed after fallback reload")
            return True
    except Exception as e:
        log.warning("Cart-confirm fallback reload failed: %s", e)

    log.warning(
        "Cart commit not confirmed within %.1fs; proceeding to checkout anyway",
        CART_CONFIRM_TIMEOUT_S,
    )
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
