#!/usr/bin/env python3
"""
strike_playwright.py

Browser-driven version of strike.py. At a designated strike time
(17:00 or 20:00 Berlin), one Playwright browser per target slot is
pre-warmed up to the redeem page, then each polls its cart-add button
for the disabled->enabled transition. The first browser to detect the
transition fires the full booking flow.

Replaces strike.py for the actual race; strike.py with its requests-
based approach has been demonstrated unreliable (cart-add returns
require_cookie redirects from non-browser sessions).

Hard guardrails:
- Single concurrent booking (threading lock + stop_signal)
- Daily booking limit (DAILY_BOOKING_LIMIT, default 2)
- Friday exclusion (EXCLUDED_WEEKDAYS)
- Sleep until the actual strike-hour boundary so polling starts at
  release time, not "whenever pre-warm finishes"

Designed to be invoked by a systemd timer ~3 minutes before each
strike hour (16:57 and 19:57 Berlin) to give Playwright time to launch
4 Chromium instances and pre-warm them.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beachplatz_watcher import (  # noqa: E402
    PREFERRED_SLOTS,
    USER_AGENT,
    WEEK_OFFSETS,
    get_week_slots,
    load_state,
    notify_telegram,
    save_state,
    todays_weekday_abbr,
    weeks_to_watch,
)
from book_playwright import (  # noqa: E402
    BASE_URL,
    cart_add_button_enabled,
    complete_checkout,
    fire_cart_add,
    prewarm_to_redeem,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Strike hours in Berlin time. Script aligns polling to these moments.
STRIKE_HOURS = (17, 20)

# Daily booking cap.
DAILY_BOOKING_LIMIT = 2

# Days the bot does NOT strike on. Friday reserved for non-bot users.
EXCLUDED_WEEKDAYS = {"Fr"}

# How long after the strike hour to keep polling for the cart-add button
# to enable. Slots typically open within seconds; 60s gives margin.
POLL_DURATION_SECONDS = 60

# Polling interval per browser, in milliseconds. Each target browser polls
# its own cart-add button independently.
POLL_INTERVAL_MS = 500

# Per-browser User-Agent so Chromium looks like normal Chrome.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("strike_pw")

BERLIN_TZ = ZoneInfo("Europe/Berlin")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def next_strike_time() -> datetime:
    """Returns next datetime (UTC) at which a strike-hour boundary occurs."""
    now_berlin = datetime.now(BERLIN_TZ)
    candidates = []
    for hour in STRIKE_HOURS:
        candidate = now_berlin.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now_berlin:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates).astimezone(ZoneInfo("UTC"))


def sleep_until_strike_time() -> None:
    target = next_strike_time()
    now = datetime.now(ZoneInfo("UTC"))
    wait = (target - now).total_seconds()

    if wait <= 0:
        log.info("Strike time already passed (%.2fs late). Polling now.", -wait)
        return
    if wait > 600:
        log.warning("Strike time is %.0fs away (>10min). Polling now anyway.", wait)
        return

    log.info("Sleeping %.2fs until strike time %s",
             wait, target.astimezone(BERLIN_TZ).isoformat(timespec="seconds"))
    time.sleep(wait)


# ---------------------------------------------------------------------------
# Booking limit helpers
# ---------------------------------------------------------------------------

def bookings_today(state: dict) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(
        1 for e in state.get("booking_log", [])
        if e.get("booked_at", "").startswith(today)
    )


def record_booking(state: dict, booking: dict) -> None:
    log_entries = state.setdefault("booking_log", [])
    log_entries.append(booking)
    if len(log_entries) > 50:
        state["booking_log"] = log_entries[-50:]


# ---------------------------------------------------------------------------
# Target discovery (uses requests, since this is a non-time-critical fetch)
# ---------------------------------------------------------------------------

def discover_target_slots() -> list[dict]:
    import requests
    today_abbr = todays_weekday_abbr()
    weeks = weeks_to_watch(WEEK_OFFSETS)
    if not weeks:
        return []
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.7"})
    all_slots = []
    for week in weeks:
        try:
            all_slots.extend(get_week_slots(s, week))
        except requests.RequestException as e:
            log.error("Week overview fetch failed: %s", e)
            return []
    targets = [
        slot for slot in all_slots
        if slot["weekday"] == today_abbr
        and (slot["weekday"], slot["time"], slot["field"]) in PREFERRED_SLOTS
    ]
    log.info("Found %d target slot(s) for %s in week %s",
             len(targets), today_abbr, weeks[0])
    for s in targets:
        log.info("  - %s %s %s [id=%s]",
                 s["weekday"], s["time"], s["field"], s["slot_id"])
    return targets


# ---------------------------------------------------------------------------
# Per-target browser holder
# ---------------------------------------------------------------------------

@dataclass
class TargetBrowser:
    slot: dict
    context: BrowserContext
    page: Page


def prewarm_target(browser: Browser, slot: dict, voucher: str) -> TargetBrowser | None:
    """Spin up a context + page and pre-warm to the redeem step."""
    try:
        ctx = browser.new_context(locale="de-DE", user_agent=BROWSER_UA,
                                  viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        if not prewarm_to_redeem(page, slot["slot_id"], voucher):
            ctx.close()
            return None
        return TargetBrowser(slot=slot, context=ctx, page=page)
    except Exception as e:
        log.error("prewarm error for %s: %s", slot["slot_id"], e)
        return None


# ---------------------------------------------------------------------------
# Per-target poll-and-book worker
# ---------------------------------------------------------------------------

def poll_and_book(target: TargetBrowser, deadline: float, email: str,
                  stop_signal: dict, hold_lock: threading.Lock) -> dict | None:
    """
    Poll the target's cart-add button until it enables or deadline.
    On enable, fire cart-add and complete checkout.
    Returns booking record on success, None otherwise.
    """
    sid = target.slot["slot_id"]
    interval = POLL_INTERVAL_MS / 1000.0
    rounds = 0

    while time.time() < deadline:
        if stop_signal.get("done"):
            return None
        rounds += 1
        round_start = time.time()
        try:
            if cart_add_button_enabled(target.page):
                log.info("BUTTON ENABLED %s after %d rounds", sid, rounds)

                # Critical section: only one thread fires the booking.
                with hold_lock:
                    if stop_signal.get("done"):
                        log.info("Another thread already won; aborting %s", sid)
                        return None
                    stop_signal["done"] = True  # claim the slot

                    if not fire_cart_add(target.page):
                        log.error("Cart-add failed for %s", sid)
                        # Reset stop_signal so other threads can try.
                        # Note: this is a deliberate exception to the
                        # "single attempt" rule -- we only made it here
                        # by claiming the lock, and our claim failed at
                        # the cart layer. Other threads can race.
                        stop_signal["done"] = False
                        return None

                    log.info("Cart added for %s, proceeding to checkout", sid)
                    order_url = complete_checkout(target.page, email)
                    if not order_url:
                        log.error("Checkout failed for %s after cart-add",
                                  sid)
                        # Cart-add succeeded but checkout failed. The
                        # 5-minute hold is now occupying our session.
                        # Don't release stop_signal -- avoid double-grabs.
                        return None

                    return {
                        "slot_id": sid,
                        "description": (
                            f"{target.slot['weekday']} {target.slot['date']} "
                            f"{target.slot['time']} {target.slot['field']}"
                        ),
                        "slot_url": target.slot["url"],
                        "order_url": order_url,
                        "booked_at": datetime.now().isoformat(timespec="seconds"),
                    }
        except Exception as e:
            log.warning("Poll error on %s: %s", sid, e)

        elapsed = time.time() - round_start
        if elapsed < interval:
            time.sleep(interval - elapsed)

    log.info("Deadline reached for %s without button enabling (%d rounds)",
             sid, rounds)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def strike() -> int:
    voucher = os.environ.get("STURA_VOUCHER")
    email = os.environ.get("BOOKING_EMAIL")
    if not voucher:
        log.error("STURA_VOUCHER not set; cannot strike")
        return 1
    if not email:
        log.error("BOOKING_EMAIL not set; cannot complete booking")
        return 1

    today_abbr = todays_weekday_abbr()
    if today_abbr in EXCLUDED_WEEKDAYS:
        log.info("Today (%s) is excluded; skipping strike", today_abbr)
        return 0

    state = load_state()
    booked_today = bookings_today(state)
    if booked_today >= DAILY_BOOKING_LIMIT:
        log.info("Daily booking limit (%d) reached; skipping strike",
                 DAILY_BOOKING_LIMIT)
        return 0

    target_slots = discover_target_slots()
    if not target_slots:
        log.info("No target slots; exiting")
        return 0

    with sync_playwright() as p:
        log.info("Launching browser...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            log.info("Pre-warming %d target browser context(s)...",
                     len(target_slots))
            targets: list[TargetBrowser] = []
            for slot in target_slots:
                tb = prewarm_target(browser, slot, voucher)
                if tb:
                    targets.append(tb)
                else:
                    log.warning("Pre-warm failed for %s; skipping",
                                slot["slot_id"])

            if not targets:
                log.error("No targets pre-warmed; exiting")
                return 0

            # Sleep until the strike-hour boundary in Berlin time.
            sleep_until_strike_time()

            log.info("Strike time. Polling for %ds, %dms per cycle. "
                     "Bookings used today: %d/%d.",
                     POLL_DURATION_SECONDS, POLL_INTERVAL_MS,
                     booked_today, DAILY_BOOKING_LIMIT)

            deadline = time.time() + POLL_DURATION_SECONDS
            stop_signal: dict = {"done": False}
            hold_lock = threading.Lock()
            winner = None

            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                futures = [
                    pool.submit(poll_and_book, t, deadline, email,
                                stop_signal, hold_lock)
                    for t in targets
                ]
                for fut in as_completed(futures):
                    result = fut.result()
                    if result:
                        winner = result
                        break

            # Cleanup all contexts
            for t in targets:
                try:
                    t.context.close()
                except Exception:
                    pass

            if not winner:
                log.info("Strike ended with no booking")
                notify_telegram(
                    "⚠️ Strike: no slots booked",
                    "Polling completed but no slot was successfully booked.",
                )
                return 0

            record_booking(state, winner)
            state["last_run"] = datetime.now().isoformat(timespec="seconds")
            state.pop("active_hold", None)
            save_state(state)

            title = f"✅ BOOKED: {winner['description']}"
            body = (
                f"Booking confirmed (€0 with voucher).\n\n"
                f"Order details / cancel link:\n{winner['order_url']}\n\n"
                f"Who's coming? Reply in this channel.\n"
                f"Bookings used today: {booked_today + 1}/{DAILY_BOOKING_LIMIT}."
            )
            notify_telegram(title, body)
            return 0

        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(strike())
