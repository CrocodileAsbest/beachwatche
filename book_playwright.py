#!/usr/bin/env python3
"""
book_playwright.py

Browser-driven booking flow. Used by strike_playwright.py to complete
a real booking after a slot opens.

The flow:
  1. Visit listing page (establishes session)
  2. Visit slot detail page
  3. Fill voucher field, click "Gutschein einlösen"
  4. Add slot to cart
  5. Navigate to /checkout/start (auto-redirects to questions)
  6. Fill email, click "Fortfahren"
  7. On confirm page: tick terms checkbox, click "Anmeldung abschicken"
  8. Wait for redirect to /order/.../

Designed to be called from a strike script which has already pre-warmed
a browser session to the redeemed voucher page. At release time, fire_cart_add()
fetches fresh server HTML, parses the cart-add form without rendering, POSTs it,
polls pretix's async cart-add task, and then navigates the browser to the
slot-specific require_cookie landing before checkout.
"""

from __future__ import annotations

import logging
import time
from playwright.sync_api import Page, TimeoutError as PWTimeout

log = logging.getLogger("book_pw")

BASE_URL = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/"

# HAR inspection showed the cart-add submit button has this stable id.
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
    fine: fire_cart_add() fetches fresh HTML internally at release time.

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
    The direct-strike script normally does not use this; it calls
    fire_cart_add() which handles fresh HTML fetch internally.
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
# Cart add helpers
# ---------------------------------------------------------------------------

def _navigate_to_require_cookie_landing(page: Page, timeout_ms: int = 5000) -> None:
    """
    After direct fetch cart-add, pretix still expects the browser to visit the
    slot-specific landing URL with require_cookie=true before checkout/start.

    Without this browser navigation, checkout/start can keep bouncing to the
    event-level ?require_cookie=true page even though the cart-add async task
    has completed.
    """
    try:
        landing_url = page.evaluate(
            """
            () => {
              const current = new URL(window.location.href);
              const next = current.searchParams.get("next");

              if (next) {
                const url = new URL(next, window.location.origin);
                url.searchParams.set("require_cookie", "true");
                return url.toString();
              }

              // Fallback: if already on a slot/detail URL, add require_cookie.
              const url = new URL(window.location.href);
              url.searchParams.set("require_cookie", "true");
              return url.toString();
            }
            """
        )

        log.info("Navigating to pretix require_cookie landing: %s", landing_url)
        page.goto(landing_url, wait_until="domcontentloaded", timeout=timeout_ms)

    except Exception as e:
        log.warning("require_cookie landing navigation failed: %s", e)


# ---------------------------------------------------------------------------
# Cart add — primary path (fetch-reload + POST in single evaluate)
# ---------------------------------------------------------------------------

def fire_cart_add(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Fetch-reload + cart-add POST + async poll, all inside a single page.evaluate()
    call. No browser reload or rendering is needed for the primary path.

    Instead of calling page.reload(), this fetches the current redeem page HTML
    via fetch(), parses it with DOMParser, extracts the cart-add form fields,
    and POSTs them. After pretix reports the async cart-add as ready, this
    function navigates the browser to the slot-specific require_cookie landing
    so checkout/start can enter /checkout/questions/.

    Error classification:
    - NO_CART_BUTTON/NO_CART_FORM: slot not yet released -> return False so
      the strike script can retry with another fetch-reload.
    - POLL_TIMEOUT: cart may already be held server-side -> return True and
      let complete_checkout() verify.
    - Other errors: fall back to browser reload + click-based approach.
    """
    try:
        result = page.evaluate(
            """
            async ({ pageUrl, timeoutMs }) => {
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

              // Step 1: Fetch page HTML. This replaces browser reload for the
              // primary path and avoids layout, paint, and sub-resource fetches.
              const pageRes = await fetch(pageUrl, {
                credentials: "same-origin",
                headers: { "Accept": "text/html" },
              });
              if (!pageRes.ok) {
                throw new Error("RELOAD_HTTP_" + pageRes.status);
              }
              const html = await pageRes.text();

              // Step 2: Parse inert HTML.
              const doc = new DOMParser().parseFromString(html, "text/html");
              const btn = doc.querySelector("#btn-add-to-cart");
              if (!btn) {
                throw new Error("NO_CART_BUTTON");
              }
              const form = btn.closest("form");
              if (!form) {
                throw new Error("NO_CART_FORM");
              }

              // Step 3: Extract form fields.
              //
              // Important: include pretix item_* checkboxes even if the inert
              // HTML lacks a literal checked attribute. The normal live form may
              // be effectively selected by browser/JS state; for cart-add we need
              // the server-rendered item_* field in the POST.
              const params = new URLSearchParams();
              for (const el of form.querySelectorAll("input, select, textarea")) {
                const name = el.getAttribute("name");
                if (!name) continue;

                const tag = el.tagName;
                const type = (el.getAttribute("type") || "").toLowerCase();

                if (type === "checkbox" || type === "radio") {
                  if (el.hasAttribute("checked") || name.startsWith("item_")) {
                    params.append(name, el.getAttribute("value") || "on");
                  }
                  continue;
                }

                if (tag === "SELECT") {
                  const selected = el.querySelector("option[selected]");
                  if (selected) {
                    params.append(name, selected.getAttribute("value") || "");
                  } else {
                    const first = el.querySelector("option");
                    params.append(name, first ? first.getAttribute("value") || "" : "");
                  }
                  continue;
                }

                params.append(name, el.getAttribute("value") || "");
              }
              params.set("ajax", "1");

              // Step 4: POST cart-add. Resolve action against fetched page URL,
              // not the currently displayed prewarm URL.
              const formAction = new URL(
                form.getAttribute("action") || "",
                pageRes.url,
              ).toString();

              const postRes = await fetch(formAction, {
                method: "POST",
                body: params.toString(),
                credentials: "same-origin",
                headers: {
                  "Content-Type": "application/x-www-form-urlencoded",
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
              let checkUrl = postJson.check_url
                ? absoluteUrl(postJson.check_url)
                : null;

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

              // Step 5: Poll pretix async cart task.
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
            {"pageUrl": page.url, "timeoutMs": timeout_ms},
        )

        log.info("Fetch-reload cart-add phase: %s", result.get("phase"))

        # If pretix returned a redirect, follow it. Otherwise always perform the
        # slot-specific require_cookie landing so checkout/start does not bounce.
        redirect = result.get("redirect")
        if redirect:
            if redirect.startswith("/"):
                redirect = "https://tix.htw.stura-dresden.de" + redirect
            try:
                log.info("Navigating to pretix cart-add redirect: %s", redirect)
                page.goto(redirect, wait_until="domcontentloaded", timeout=5000)
            except Exception as e:
                log.warning("Cart-add redirect navigation failed: %s", e)
                _navigate_to_require_cookie_landing(page, timeout_ms=5000)
        else:
            _navigate_to_require_cookie_landing(page, timeout_ms=5000)

        return True

    except Exception as e:
        msg = str(e)

        # Slot not yet released -> return False so the caller can retry quickly.
        if "NO_CART_BUTTON" in msg or "NO_CART_FORM" in msg:
            log.warning("Fetch-reload: cart button/form not in HTML")
            return False

        # POST was sent, async task may already hold the slot. Proceeding to
        # checkout is safer than retrying and risking a double-hold.
        if "POLL_TIMEOUT" in msg:
            log.warning(
                "Fetch-reload: async polling timed out; proceeding to "
                "checkout because a cart hold may already exist: %s",
                e,
            )
            _navigate_to_require_cookie_landing(page, timeout_ms=5000)
            return True

        # Any other failure: fall back to proven browser reload + click.
        log.warning(
            "Fetch-reload cart-add failed, falling back to reload+click: %s",
            e,
        )
        return fire_cart_add_click_fallback(
            page,
            timeout_ms=min(timeout_ms, 2500),
        )


# ---------------------------------------------------------------------------
# Cart add — click-based fallback
# ---------------------------------------------------------------------------

def fire_cart_add_click_fallback(page: Page, timeout_ms: int = 2500) -> bool:
    """
    Click-based cart-add fallback. Does a browser reload first because the
    fetch-based primary path does not navigate/render fresh HTML into the live
    page DOM, then clicks the button and confirms.
    """
    CART_CONFIRM_TIMEOUT_S = 1.5
    CART_POLL_INTERVAL_S = 0.1

    confirm_markers = (
        "deinem warenkorb hinzugefügt",
        "dein warenkorb",
        "minuten für dich reserviert",
    )

    # The fetch-based path did not update the live DOM. A real browser reload is
    # needed for the click path.
    try:
        page.reload(wait_until="domcontentloaded", timeout=5000)
    except Exception as e:
        log.warning("Click fallback reload failed: %s", e)
        return False

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

    # One fallback reload only.
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
    /?require_cookie=true at checkout/start. This should normally be solved by
    fire_cart_add() navigating to the slot-specific require_cookie landing first.
    The retry loop remains as a backstop.
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
