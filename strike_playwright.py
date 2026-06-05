#!/usr/bin/env python3
"""
strike_playwright.py

Browser-driven direct strike script. Pre-warms one Playwright browser context
per target slot to the redeem page, wakes up 1 second before the real release
boundary (17:00 or 20:00 Berlin), then at the exact release boundary reloads
once and immediately clicks "Zum Warenkorb hinzufügen".

This version intentionally skips the repeated detect/poll loop. It assumes the
slot is available at the release boundary, while still doing the one required
reload because pretix pages are server-rendered: a pre-warmed page from before
release will not automatically show the cart-add button.

Strike-hour-aware target filtering:
- The 17:00 strike targets only Feld 1 slots.
- The 20:00 strike targets only Feld 2 slots.

Hard guardrails:
- Single concurrent booking (loop breaks on first success)
- Daily booking limit (DAILY_BOOKING_LIMIT, default 2)
- Weekday exclusion (EXCLUDED_WEEKDAYS): Wed and Fri reserved for non-bot users
- Wake 1 second early, then click only at the exact release boundary
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
    complete_checkout,
    fire_cart_add,
    prewarm_to_redeem,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Strike hours in Berlin time. Script aligns the final direct cart-add to these.
STRIKE_HOURS = (17, 20)

# Wake up this many seconds before the real release boundary.
PRE_STRIKE_WAKE_SECONDS = 1

# Daily booking cap.
DAILY_BOOKING_LIMIT = 2

# Days the bot does NOT strike on. Wed and Fri reserved for non-bot users.
EXCLUDED_WEEKDAYS = {"Mi", "Fr"}

# Per-browser User-Agent so Chromium looks like normal Chrome.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Resource types to block during the strike reload. Only the HTML (and the
# JS needed for the form) matters for cart-add; images, CSS, fonts, and media
# just waste bandwidth and parsing time.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "media"})


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("strike_pw")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
UTC_TZ = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def next_release_time_berlin() -> datetime:
    """Return the next real strike release boundary in Berlin time."""
    now_berlin = datetime.now(BERLIN_TZ)
    candidates: list[datetime] = []

    for hour in STRIKE_HOURS:
        candidate = now_berlin.replace(
            hour=hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if candidate <= now_berlin:
            candidate += timedelta(days=1)
        candidates.append(candidate)

    return min(candidates)


def poll_start_time_utc(release_time_berlin: datetime) -> datetime:
    """Return UTC wake time: PRE_STRIKE_WAKE_SECONDS before release."""
    return (
        release_time_berlin - timedelta(seconds=PRE_STRIKE_WAKE_SECONDS)
    ).astimezone(UTC_TZ)


def sleep_until(target_utc: datetime, label: str, warn_if_more_than_s: int = 600) -> None:
    """Sleep until a UTC target time, unless already late."""
    now = datetime.now(UTC_TZ)
    wait = (target_utc - now).total_seconds()

    if wait <= 0:
        log.info("%s already passed (%.3fs late). Continuing now.", label, -wait)
        return

    if wait > warn_if_more_than_s:
        log.warning(
            "%s is %.0fs away (>%.0fmin). Continuing now anyway.",
            label,
            wait,
            warn_if_more_than_s / 60,
        )
        return

    log.info(
        "Sleeping %.3fs until %s %s",
        wait,
        label,
        target_utc.astimezone(BERLIN_TZ).isoformat(timespec="milliseconds"),
    )
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

def discover_target_slots(active_field: str | None = None) -> list[dict]:
    """
    Find all slots matching today's weekday and the PREFERRED_SLOTS list.
    If active_field is provided (e.g. "Feld 1" or "Feld 2"), further
    restrict results to that field only.
    """
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
        and (active_field is None or slot["field"] == active_field)
    ]

    log.info(
        "Found %d target slot(s) for %s in week %s (field filter: %s)",
        len(targets),
        today_abbr,
        weeks[0],
        active_field or "all",
    )
    for slot in targets:
        log.info(
            "  - %s %s %s [id=%s]",
            slot["weekday"],
            slot["time"],
            slot["field"],
            slot["slot_id"],
        )
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
        ctx = browser.new_context(
            locale="de-DE",
            user_agent=BROWSER_UA,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        if not prewarm_to_redeem(page, slot["slot_id"], voucher):
            ctx.close()
            return None
        return TargetBrowser(slot=slot, context=ctx, page=page)
    except Exception as e:
        log.error("prewarm error for %s: %s", slot["slot_id"], e)
        return None


def _block_non_essential(route):
    """Abort requests for images, CSS, fonts, media; pass everything else."""
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def arm_resource_blocking(target: TargetBrowser) -> None:
    """
    Install the resource-blocking route handler on a pre-warmed page.

    Call this AFTER prewarm completes (so the initial page load fetches
    everything it needs) but BEFORE the strike reload, so the reload only
    transfers HTML + JS — no images, CSS, fonts, or media.
    """
    target.page.route("**/*", _block_non_essential)
    log.info("Resource blocking armed for %s", target.slot["slot_id"])


def direct_booking_attempt(target: TargetBrowser, email: str) -> dict | None:
    """
    Reload once at release time, click cart-add immediately, then checkout.
    Returns winner dict on success, None on failure.
    """
    sid = target.slot["slot_id"]

    try:
        # "commit" fires as soon as the browser receives the first response
        # bytes from the server — before full HTML parsing completes.
        # Combined with the resource-blocking route (armed earlier), the DOM
        # is ready almost instantly after commit because there are no
        # sub-resource fetches to stall on.
        target.page.reload(wait_until="commit", timeout=8000)

        # The form/button may not be in the DOM the instant commit fires.
        # Wait for the submit button to appear (state="attached" rather than
        # "visible" — with CSS blocked the element is still interactable but
        # Playwright's visibility heuristic might be overly cautious).
        target.page.wait_for_selector(
            "button[type='submit'], input[type='submit']",
            state="attached",
            timeout=5000,
        )

        log.info("Attempting direct cart-add for %s", sid)
        if not fire_cart_add(target.page):
            log.error("Direct cart-add failed for %s", sid)
            return None

        log.info("Cart added for %s, proceeding to checkout", sid)
        order_url = complete_checkout(target.page, email)
        if not order_url:
            log.error("Checkout failed for %s after cart-add", sid)
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
        log.error("Booking exception for %s: %s", sid, e)
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
        log.info(
            "Daily booking limit (%d) reached; skipping strike",
            DAILY_BOOKING_LIMIT,
        )
        return 0

    # Determine the real release boundary first. Do NOT subtract one second
    # from this value, because the hour determines the active field.
    release_time_berlin = next_release_time_berlin()
    strike_hour = release_time_berlin.hour

    if strike_hour == 17:
        active_field = "Feld 1"
    elif strike_hour == 20:
        active_field = "Feld 2"
    else:
        log.error("Unexpected strike hour %d; aborting", strike_hour)
        return 1

    log.info(
        "Strike for %02d:00 Berlin -- targeting %s only",
        strike_hour,
        active_field,
    )

    target_slots = discover_target_slots(active_field)
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
            log.info("Pre-warming %d target browser context(s)...", len(target_slots))
            targets: list[TargetBrowser] = []
            for slot in target_slots:
                tb = prewarm_target(browser, slot, voucher)
                if tb:
                    targets.append(tb)
                else:
                    log.warning("Pre-warm failed for %s; skipping", slot["slot_id"])

            if not targets:
                log.error("No targets pre-warmed; exiting")
                return 0

            # Arm resource blocking on every pre-warmed page. This is done
            # AFTER prewarm (which needs the full page) but BEFORE the strike
            # reload, so the reload only fetches HTML + JS.
            for t in targets:
                arm_resource_blocking(t)

            # Wake 1 second early so setup is definitely done, but do not reload
            # and click until the exact release boundary.
            wake_utc = poll_start_time_utc(release_time_berlin)
            release_utc = release_time_berlin.astimezone(UTC_TZ)
            sleep_until(wake_utc, "pre-strike wake time")
            sleep_until(release_utc, "release time", warn_if_more_than_s=PRE_STRIKE_WAKE_SECONDS + 5)

            log.info(
                "Release time. Direct booking across %d target(s). "
                "Bookings used today: %d/%d.",
                len(targets),
                booked_today,
                DAILY_BOOKING_LIMIT,
            )

            winner: dict | None = None
            attempts_per_target = {t.slot["slot_id"]: 0 for t in targets}

            # Single pass: no detect/poll loop. Try each target once, in order,
            # and stop immediately on first successful booking.
            for target in targets:
                sid = target.slot["slot_id"]
                attempts_per_target[sid] += 1

                winner = direct_booking_attempt(target, email)
                if winner:
                    break

            # Cleanup all contexts
            for t in targets:
                try:
                    t.context.close()
                except Exception:
                    pass

            if not winner:
                summary = ", ".join(
                    f"{sid}:{n}" for sid, n in attempts_per_target.items()
                )
                log.info(
                    "Direct strike ended with no booking. Attempts per target: %s",
                    summary,
                )
                notify_telegram(
                    "Strike: no slots booked",
                    "Direct booking attempt completed but no slot was successfully booked.\n"
                    f"Attempts per target: {summary}",
                )
                return 0

            record_booking(state, winner)
            state["last_run"] = datetime.now().isoformat(timespec="seconds")
            state.pop("active_hold", None)
            save_state(state)

            title = f"BOOKED: {winner['description']}"
            body = (
                f"Booking confirmed (0 EUR with voucher).\n\n"
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
