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

def fire_cart_add(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Submit the rendered pretix cart-add form via fetch(), then poll pretix's
    async check_url until the cart-add task is ready.

    Important release-time behavior:
    - If the cart-add button/form is not present in the freshly reloaded HTML,
      return False quickly. The strike script should then reload and retry for
      a short post-release burst window.
    - If the POST succeeds but async polling times out, return True and let
      complete_checkout() act as the backstop. At that point a cart hold may
      already exist, so trying to click again could double-hold.
    - For non-preflight direct-fetch failures, fall back to the proven click
      flow with a capped timeout.
    """
    try:
        result = page.evaluate(
            """
            async ({ timeoutMs }) => {
              const startedAt = Date.now();

              function remaining() {
                return Math.max(0, timeoutMs - (Date.now() - startedAt));
              }

              function sleep(ms) {
                return new Promise(resolve => setTimeout(resolve, ms));
              }

              function absoluteUrl(url) {
                return new URL(url, window.location.origin).toString();
              }

              const btn = document.querySelector("#btn-add-to-cart");
              if (!btn) {
                throw new Error("NO_CART_BUTTON: cart-add button not found");
              }

              const form = btn.closest("form");
              if (!form) {
                throw new Error("NO_CART_FORM: cart-add form not found");
              }

              const fd = new FormData(form);
              fd.set("ajax", "1");

              const postRes = await fetch(form.action, {
                method: "POST",
                body: fd,
                credentials: "same-origin",
                headers: {
                  "X-Requested-With": "XMLHttpRequest",
                  "Accept": "application/json, text/javascript, */*; q=0.01",
                },
              });

              const postText = await postRes.text();
              let postJson;
              try {
                postJson = JSON.parse(postText);
              } catch (e) {
                throw new Error("POST_BAD_JSON: " + postText.slice(0, 300));
              }

              if (!postRes.ok) {
                throw new Error("POST_HTTP_" + postRes.status + ": " + postText.slice(0, 300));
              }

              let lastJson = postJson;
              let checkUrl = postJson.check_url ? absoluteUrl(postJson.check_url) : null;

              if (postJson.ready === true) {
                return {
                  ok: true,
                  phase: "post-ready",
                  redirect: postJson.redirect || postJson.url || null,
                };
              }

              if (!checkUrl) {
                throw new Error("POST_NO_CHECK_URL: " + postText.slice(0, 500));
              }

              while (remaining() > 0) {
                const checkRes = await fetch(checkUrl, {
                  method: "GET",
                  credentials: "same-origin",
                  headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                  },
                });

                const checkText = await checkRes.text();
                let checkJson;
                try {
                  checkJson = JSON.parse(checkText);
                } catch (e) {
                  throw new Error("CHECK_BAD_JSON: " + checkText.slice(0, 300));
                }

                lastJson = checkJson;

                if (checkJson.ready === true) {
                  return {
                    ok: true,
                    phase: "check-ready",
                    redirect: checkJson.redirect || checkJson.url || null,
                  };
                }

                if (checkJson.redirect || checkJson.url) {
                  return {
                    ok: true,
                    phase: "check-redirect",
                    redirect: checkJson.redirect || checkJson.url,
                  };
                }

                if (checkJson.check_url) {
                  checkUrl = absoluteUrl(checkJson.check_url);
                }

                await sleep(100);
              }

              throw new Error("POLL_TIMEOUT: lastJson=" + JSON.stringify(lastJson));
            }
            """,
            {"timeoutMs": timeout_ms},
        )

        log.info("Direct fetch cart-add phase: %s", result.get("phase"))

        redirect = result.get("redirect")
        if redirect:
            if redirect.startswith("/"):
                redirect = "https://tix.htw.stura-dresden.de" + redirect

            try:
                page.goto(redirect, wait_until="domcontentloaded", timeout=5000)
            except Exception as e:
                log.warning("Direct fetch redirect navigation failed: %s", e)

        else:
            # Mimic pretix's final cookie-confirm landing after cart-add.
            try:
                current = page.url
                if "/redeem" in current:
                    page.evaluate(
                        """
                        () => {
                          const params = new URLSearchParams(window.location.search);
                          const next = params.get("next");
                          if (next) {
                            window.location.href = next + (next.includes("?") ? "&" : "?") + "require_cookie=true";
                          }
                        }
                        """
                    )
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception as e:
                log.warning("Direct fetch require_cookie navigation failed: %s", e)

        return True

    except Exception as e:
        msg = str(e)

        # This is the critical competitive-setting case: the release reload
        # returned HTML without the form. Do not spend seconds on fallback click;
        # return False so strike_playwright can reload and retry immediately.
        if "NO_CART_BUTTON" in msg or "NO_CART_FORM" in msg:
            log.warning("Direct fetch cart-add preflight failed: %s", e)
            return False

        # After the POST starts, the slot may already be held. Avoid a second
        # cart-add attempt; let checkout/start verify whether the cart exists.
        if "POLL_TIMEOUT" in msg:
            log.warning(
                "Direct fetch async polling timed out; proceeding to checkout "
                "because a cart hold may already exist: %s",
                e,
            )
            return True

        log.warning("Direct fetch cart-add failed, falling back to click flow: %s", e)
        return fire_cart_add_click_fallback(
            page,
            timeout_ms=min(timeout_ms, 2500),
        )


def fire_cart_add_click_fallback(page: Page, timeout_ms: int = 2500) -> bool:
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
