#!/usr/bin/env python3
"""
strike_playwright.py

Browser-driven strike script. Pre-warms one Playwright browser context
per target slot to the redeem page, then at strike time (17:00 or 20:00
Berlin) polls each context's cart-add button by reloading the page and
checking whether the button is present and enabled.

Polling is sequential across targets (one browser at a time, in a loop)
because Playwright's sync API is not thread-safe. With ~330ms per
reload-check and 2-4 targets, each individual slot is rechecked every
~660-1300ms during the strike window.

Hard guardrails:
- Single concurrent booking (loop breaks on first success)
- Daily booking limit (DAILY_BOOKING_LIMIT, default 2)
- Friday exclusion (EXCLUDED_WEEKDAYS)
- Sleep until the actual strike-hour boundary so polling starts at
  release time, not "whenever pre-warm finishes"
"""

from __future__ import annotations

import logging
import os
import sys
import time
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

# Days the bot does NOT strike on.
EXCLUDED_WEEKDAYS = {"Fr"}

# How long after the strike hour to keep polling.
POLL_DURATION_SECONDS = 60

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
# Target discovery (uses requests for the lightweight week-overview fetch)
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

            # Sleep until the actual strike-hour boundary in Berlin time.
            sleep_until_strike_time()

            log.info("Strike time. Polling sequentially across %d target(s) "
                     "for %ds. Bookings used today: %d/%d.",
                     len(targets), POLL_DURATION_SECONDS,
                     booked_today, DAILY_BOOKING_LIMIT)

            deadline = time.time() + POLL_DURATION_SECONDS
            winner: dict | None = None
            rounds_per_target = {t.slot["slot_id"]: 0 for t in targets}

            # Sequential polling loop. Iterate over all targets, then repeat.
            # Each iteration of the outer loop is one "round" across all
            # targets; each inner iteration polls one target.
            while time.time() < deadline and winner is None:
                for target in targets:
                    if time.time() >= deadline:
                        break
                    sid = target.slot["slot_id"]
                    rounds_per_target[sid] += 1

                    try:
                        if not cart_add_button_enabled(target.page):
                            continue  # Slot not yet open or button absent
                        log.info("BUTTON ENABLED %s after %d rounds",
                                 sid, rounds_per_target[sid])
                    except Exception as e:
                        log.warning("Poll error on %s: %s", sid, e)
                        continue

                    # Button is enabled. Attempt the booking.
                    try:
                        if not fire_cart_add(target.page):
                            log.error("Cart-add failed for %s after button enabled",
                                      sid)
                            continue
                        log.info("Cart added for %s, proceeding to checkout", sid)

                        order_url = complete_checkout(target.page, email)
                        if not order_url:
                            log.error("Checkout failed for %s after cart-add",
                                      sid)
                            # Don't try another slot -- the 5-minute hold from
                            # this attempt is occupying our session and we'd
                            # double-hold if we proceed.
                            winner = None
                            break
                    except Exception as e:
                        log.error("Booking exception for %s: %s", sid, e)
                        continue

                    winner = {
                        "slot_id": sid,
                        "description": (
                            f"{target.slot['weekday']} {target.slot['date']} "
                            f"{target.slot['time']} {target.slot['field']}"
                        ),
                        "slot_url": target.slot["url"],
                        "order_url": order_url,
                        "booked_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    break  # Exit inner for-loop; outer while will see winner

            # Cleanup all contexts
            for t in targets:
                try:
                    t.context.close()
                except Exception:
                    pass

            if not winner:
                summary = ", ".join(
                    f"{sid}:{n}" for sid, n in rounds_per_target.items()
                )
                log.info("Strike ended with no booking. Rounds per target: %s",
                         summary)
                notify_telegram(
                    "⚠️ Strike: no slots booked",
                    "Polling completed but no slot was successfully booked.\n"
                    f"Rounds per target: {summary}",
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
