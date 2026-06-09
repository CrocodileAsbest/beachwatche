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
    fine: the strike script should call fire_cart_add() at release time, which
    fetches fresh HTML internally.

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
    fire_cart_add() which handles the reload internally.
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
# Cart add — primary path (fetch-reload + POST in single evaluate)
# ---------------------------------------------------------------------------

def fire_cart_add(page: Page, timeout_ms: int = 5000) -> bool:
    """
    Fetch-reload + cart-add POST + async poll, all inside a single
    page.evaluate() call. No browser reload or rendering needed.

    Instead of calling page.reload() (which triggers layout, paint, and
    sub-resource fetches), this fetches the page HTML via fetch(), parses
    it with DOMParser (no rendering), extracts the cart-add form fields,
    and POSTs them — all in one JavaScript execution context with one
    Playwright IPC round-trip.

    Error classification:
    - NO_CART_BUTTON/NO_CART_FORM: slot not yet released → return False
      so the strike script can retry with another fetch-reload.
    - POLL_TIMEOUT: cart may already be held server-side → return True,
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

              // ── Step 1: Fetch page HTML (replaces browser reload) ──────
              // No layout, no paint, no sub-resource fetches. The browser
              // sends session cookies automatically via same-origin.
              const pageRes = await fetch(pageUrl, {
                credentials: "same-origin",
                headers: { "Accept": "text/html" },
              });
              if (!pageRes.ok) {
                throw new Error("RELOAD_HTTP_" + pageRes.status);
              }
              const html = await pageRes.text();

              // ── Step 2: Parse HTML with DOMParser ──────────────────────
              // Creates an inert document — no scripts execute, no images
              // or CSS are fetched.
              const doc = new DOMParser().parseFromString(html, "text/html");
              const btn = doc.querySelector("#btn-add-to-cart");
              if (!btn) {
                throw new Error("NO_CART_BUTTON");
              }
              const form = btn.closest("form");
              if (!form) {
                throw new Error("NO_CART_FORM");
              }

              // ── Step 3: Extract form fields ────────────────────────────
              // Manual extraction rather than new FormData(form) because
              // FormData on DOMParser documents may not pick up all fields
              // reliably across engines. On an inert document el.value
              // equals the value attribute, which is correct for server-
              // rendered hidden inputs (CSRF, item IDs, voucher fields).
              const params = new URLSearchParams();
              for (const el of form.querySelectorAll("input, select, textarea")) {
                const name = el.getAttribute("name");
                if (!name) continue;
                const type = (el.getAttribute("type") || "").toLowerCase();

                if (type === "checkbox" || type === "radio") {
                  if (el.hasAttribute("checked") || name.startsWith("item_")) {
                  params.append(name, el.getAttribute("value") || "on");
                  }
                  continue;
                } else if (el.tagName === "SELECT") {
                  const selected = el.querySelector("option[selected]");
                  params.append(name, selected ? selected.getAttribute("value") || "" : "");
                } else {
                  params.append(name, el.getAttribute("value") || "");
                }
              }
              params.set("ajax", "1");

              // ── Step 4: POST cart-add ───────────────────────────────────
              // Resolve the form action against the fetched page URL (not
              // the pre-warm URL, in case they differ).
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

              // ── Step 5: Poll async task ────────────────────────────────
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

        # If pretix returned a redirect, navigate there so the session
        # reaches the expected post-cart-add state. Not strictly required
        # (complete_checkout navigates to checkout/start directly), but
        # may help pretix settle its cookie/session state.
        redirect = result.get("redirect")
        if redirect:
            if redirect.startswith("/"):
                redirect = "https://tix.htw.stura-dresden.de" + redirect
            try:
                page.goto(redirect, wait_until="domcontentloaded", timeout=5000)
            except Exception as e:
                log.warning("Cart-add redirect navigation failed: %s", e)

        return True

    except Exception as e:
        msg = str(e)

        # Slot not yet released — return False so the caller can retry.
        if "NO_CART_BUTTON" in msg or "NO_CART_FORM" in msg:
            log.warning("Fetch-reload: cart button not in HTML")
            return False

        # POST was sent, Celery task may already hold the slot. Proceeding
        # to checkout is safer than retrying and risking a double-hold.
        if "POLL_TIMEOUT" in msg:
            log.warning(
                "Fetch-reload: async polling timed out; proceeding to "
                "checkout because a cart hold may already exist: %s",
                e,
            )
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
    Click-based cart-add fallback. Does a browser reload first (the
    fetch-based primary path does not navigate, so the page DOM is stale),
    then clicks the button and confirms.
    """
    CART_CONFIRM_TIMEOUT_S = 1.5
    CART_POLL_INTERVAL_S = 0.1

    confirm_markers = (
        "deinem warenkorb hinzugefügt",
        "dein warenkorb",
        "minuten für dich reserviert",
    )

    # The fetch-based path didn't navigate, so the live DOM is still the
    # pre-warm page. A real browser reload is needed for the click path.
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
