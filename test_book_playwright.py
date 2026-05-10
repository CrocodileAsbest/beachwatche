#!/usr/bin/env python3
"""
test_book_playwright.py

Books a single known-open slot end-to-end using Playwright. Used to
verify the full booking flow works through a real browser before
wiring it into strike.py.

Usage: python test_book_playwright.py <slot_id>
"""

import os
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/"


def book_slot(slot_id: str, voucher: str, email: str, headless: bool = True) -> str | None:
    """
    Drive a real browser through the full booking flow for slot_id.
    Returns the order URL on success, None on any failure.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                locale="de-DE",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            # 1. Navigate to slot detail
            slot_url = f"{BASE_URL}{slot_id}/"
            print(f"[1] GET {slot_url}")
            page.goto(slot_url, wait_until="domcontentloaded", timeout=15000)

            # 2. Enter voucher and redeem
            print("[2] Entering voucher...")
            voucher_input = page.locator('input[name="voucher"], input#voucher, input[name="_voucher_code"]').first
            voucher_input.fill(voucher)

            # The "Gutschein einlösen" button. Match by text since the
            # button name attribute may vary.
            redeem_btn = page.get_by_role("button", name="Gutschein einlösen")
            redeem_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)

            # 3. Click "Zum Warenkorb hinzufügen" on the redemption page.
            # There may be two such buttons (the page has one before voucher
            # entry and one after); we want the one in the unlocked state.
            print("[3] Adding to cart...")
            add_btn = page.get_by_role("button", name="Zum Warenkorb hinzufügen").first
            add_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)

            # 4. We should now be on the cart or slot page with item in cart.
            # Pretix may show a separate cart page or show the slot page with
            # a "Weiter" button. Look for the next-step button.
            print("[4] Proceeding to checkout...")
            # Try common variations
            next_btn = page.get_by_role("button", name="Weiter zur Kasse").or_(
                page.get_by_role("button", name="Weiter")
            ).or_(
                page.get_by_role("link", name="Weiter zur Kasse")
            ).first
            next_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)

            # 5. Email field on /checkout/questions/
            print("[5] Submitting email...")
            page.locator('input[name="email"]').fill(email)
            submit_btn = page.get_by_role("button", name="Weiter").first
            submit_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)

            # 6. Confirm page: tick the terms checkbox and submit
            print("[6] Confirming...")
            # The terms checkbox name starts with "confirm_"
            page.locator('input[type="checkbox"][name^="confirm_"]').first.check()
            confirm_btn = page.get_by_role("button", name="Anmeldung abschicken").or_(
                page.get_by_role("button", name="Bestellung absenden")
            ).first
            confirm_btn.click()

            # 7. Wait for redirect to the order page (possibly via async polling)
            print("[7] Waiting for booking confirmation...")
            page.wait_for_url("**/order/**", timeout=60000)
            order_url = page.url
            print(f"[7] Order URL: {order_url}")
            return order_url

        except PWTimeout as e:
            print(f"Timeout: {e}")
            return None
        except Exception as e:
            print(f"Error: {e}")
            return None
        finally:
            browser.close()


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python test_book_playwright.py <slot_id>")
        return 1
    slot_id = sys.argv[1]
    voucher = os.environ.get("STURA_VOUCHER")
    email = os.environ.get("BOOKING_EMAIL")
    if not voucher or not email:
        print("STURA_VOUCHER and BOOKING_EMAIL must be set in environment")
        return 1
    result = book_slot(slot_id, voucher, email)
    if result:
        print(f"\nSUCCESS: {result}")
        return 0
    else:
        print("\nFAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
