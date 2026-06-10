#!/usr/bin/env python3
"""
strike_playwright.py

Browser-driven direct strike script. Pre-warms one Playwright browser context
per target slot to the redeem page and prebuilds the cart-add POST body,
wakes up shortly before the real release boundary (17:00 or 20:00 Berlin),
measures the server clock offset and warms keep-alive connections, then at
the (clock-adjusted) release boundary fires the prebuilt cart-add POST in a
single network round trip. Falls back to fetch+DOMParser and finally to a
browser reload+click if needed.

This version skips the old repeated detect/poll loop, but uses a short
post-release retry burst to handle minor server clock jitter.

Strike-hour-aware target filtering:
- The 17:00 strike targets only Feld 1 slots.
- The 20:00 strike targets only Feld 2 slots.

Hard guardrails:
- Single completed checkout only
- Daily booking limit (DAILY_BOOKING_LIMIT, default 2)
- Weekday exclusion (EXCLUDED_WEEKDAYS): Wed and Fri reserved for non-bot users
- Wake a few seconds early for clock probe + warming, then fire only at the
  (server-clock-adjusted) release boundary
- If cart-add succeeds but checkout never enters the checkout flow and only
  bounces to require_cookie, allow trying the next target.
- If checkout reaches the checkout flow and then fails, stop to avoid double-hold.
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
    build_cart_post_body,
    complete_checkout,
    fire_cart_add,
    prewarm_to_redeem,
    warm_connection,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Strike hours in Berlin time. Script aligns the final direct cart-add to these.
STRIKE_HOURS = (17, 20)

# Wake up this many seconds before the real release boundary. The window is
# used for (1) measuring the server clock offset and (2) warming keep-alive
# connections, so it must fit both with margin.
PRE_STRIKE_WAKE_SECONDS = 2.5

# Server clock offset probe: budget and sanity clamp. If the measured offset
# exceeds the clamp, it is treated as a measurement error and ignored.
CLOCK_PROBE_BUDGET_S = 1.2
MAX_CLOCK_ADJUST_S = 1.5

# Number of initial fire_cart_add attempts that use the prebuilt POST body
# (one network round trip). Later attempts fall back to fetch+parse, which is
# slower but self-correcting: it reads the real released form from the server.
PREBUILT_ATTEMPTS = 3

# pretix product ID (constant across subevents). Enables the synthetic
# prebuilt POST body even when the cart form is not rendered pre-release.
# Verified from HAR capture 2026-06-05: POST body was exactly
#   csrfmiddlewaretoken, subevent=<id>, _voucher_code, item_8=1, ajax=1
CART_ITEM_ID = os.environ.get("CART_ITEM_ID") or "8"

# After the release boundary, keep retrying fire_cart_add for a short burst.
# This handles small release/server clock jitter where the first fetch-reload
# still returns pre-release HTML without a cart button.
STRIKE_RETRY_WINDOW_SECONDS = 5.0
STRIKE_RETRY_SLEEP_SECONDS = 0.18

# Timeout for each fire_cart_add call. This covers fetch-reload + DOMParser +
# POST + async poll + require_cookie landing fallback.
STRIKE_CART_ADD_TIMEOUT_MS = 2500

# Daily booking cap.
DAILY_BOOKING_LIMIT = 2

# Days the bot does NOT strike on. Wed and Fri reserved for non-bot users.
EXCLUDED_WEEKDAYS = {"Mi", "Fr"}

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


def wake_time_utc(release_time_berlin: datetime) -> datetime:
    """Return UTC wake time: PRE_STRIKE_WAKE_SECONDS before release."""
    return (
        release_time_berlin - timedelta(seconds=PRE_STRIKE_WAKE_SECONDS)
    ).astimezone(UTC_TZ)


def measure_server_clock_offset(
    probe_url: str,
    budget_s: float = CLOCK_PROBE_BUDGET_S,
) -> float:
    """
    Estimate (server_time - local_time) in seconds by watching the HTTP Date
    header flip to a new second. The Date header has 1s resolution, but the
    moment it changes pins the server's second boundary to within roughly one
    polling interval plus half the round trip.

    Returns 0.0 if no flip is observed within the budget or the measurement
    fails sanity checks. The strike then fires on local NTP time, exactly as
    before -- this probe is insurance, not a dependency.
    """
    import requests
    from email.utils import parsedate_to_datetime

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    deadline = time.monotonic() + budget_s
    last_server_epoch: float | None = None

    try:
        while time.monotonic() < deadline:
            t0 = time.time()
            r = s.head(probe_url, timeout=0.8, allow_redirects=False)
            t1 = time.time()

            date_hdr = r.headers.get("Date")
            if not date_hdr:
                log.info("Clock probe: no Date header; skipping adjustment")
                return 0.0

            server_epoch = parsedate_to_datetime(date_hdr).timestamp()

            if last_server_epoch is not None and server_epoch > last_server_epoch:
                # The server's second boundary fell between the previous
                # response and this one. Best estimate of "when the server
                # clock read server_epoch" is the midpoint of this request.
                local_mid = t0 + (t1 - t0) / 2.0
                offset = server_epoch - local_mid
                rtt_ms = (t1 - t0) * 1000.0

                if abs(offset) > MAX_CLOCK_ADJUST_S:
                    log.warning(
                        "Clock probe: offset %.3fs exceeds clamp %.1fs; "
                        "ignoring (rtt %.0fms)",
                        offset,
                        MAX_CLOCK_ADJUST_S,
                        rtt_ms,
                    )
                    return 0.0

                log.info(
                    "Clock probe: server-local offset %.3fs (rtt %.0fms)",
                    offset,
                    rtt_ms,
                )
                return offset

            last_server_epoch = server_epoch
            time.sleep(0.04)

    except Exception as e:
        log.warning("Clock probe failed (non-fatal): %s", e)

    log.info("Clock probe: no second flip observed in %.1fs; no adjustment", budget_s)
    return 0.0


def sleep_until(target_utc: datetime, label: str, warn_if_more_than_s: int = 600) -> None:
    """Sleep until a UTC target time, unless already late."""
    now = datetime.now(UTC_TZ)
    wait = (target_utc - now).total_seconds()

    if wait <= 0:
        log.info("%s already passed (%.3fs late). Continuing now.", label, -wait)
        return

    if wait > warn_if_more_than_s:
        log.warning(
            "%s is %.0fs away (>%.0fmin). Sleeping until target time anyway.",
            label,
            wait,
            warn_if_more_than_s / 60,
        )

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
    today = datetime.now(BERLIN_TZ).strftime("%Y-%m-%d")
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
    prebuilt: dict | None = None


def prewarm_target(browser: Browser, slot: dict, voucher: str) -> TargetBrowser | None:
    """Spin up a context + page, pre-warm to the redeem step, and prebuild
    the cart-add POST body so the strike can skip the HTML fetch."""
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

        sid = str(slot["slot_id"])
        prebuilt = build_cart_post_body(
            page,
            voucher,
            subevent_id=sid if sid.isdigit() else None,
            item_id=CART_ITEM_ID,
        )
        return TargetBrowser(slot=slot, context=ctx, page=page, prebuilt=prebuilt)
    except Exception as e:
        log.error("prewarm error for %s: %s", slot["slot_id"], e)
        return None


def direct_booking_attempt(target: TargetBrowser, email: str) -> tuple[dict | None, bool]:
    """
    Try fire_cart_add for a short post-release window, then checkout.

    Returns (winner, cart_was_added).

    If cart-add succeeds but checkout never enters the checkout form flow and
    only bounces to require_cookie, return (None, False) so the caller may try
    the next target. If checkout reaches/appears to reach the checkout flow and
    fails later, return (None, True) to avoid multiple holds.
    """
    sid = target.slot["slot_id"]
    deadline = time.monotonic() + STRIKE_RETRY_WINDOW_SECONDS
    attempt = 0
    cart_was_added = False

    try:
        while time.monotonic() < deadline and not cart_was_added:
            attempt += 1

            # First attempts use the prebuilt one-round-trip POST; later
            # attempts use fetch+parse, which reads the real released form
            # and self-corrects if the prebuilt body is structurally wrong.
            use_prebuilt = target.prebuilt if attempt <= PREBUILT_ATTEMPTS else None
            log.info(
                "Attempting cart-add for %s attempt %d (%s)",
                sid,
                attempt,
                "prebuilt" if use_prebuilt else "fetch+parse",
            )

            if fire_cart_add(
                target.page,
                timeout_ms=STRIKE_CART_ADD_TIMEOUT_MS,
                prebuilt=use_prebuilt,
            ):
                cart_was_added = True
                log.info(
                    "Cart added for %s on attempt %d, proceeding to checkout",
                    sid,
                    attempt,
                )
                break

            # fire_cart_add returned False -> fresh fetched HTML did not yet
            # contain the cart form/button. Retry briefly.
            time.sleep(STRIKE_RETRY_SLEEP_SECONDS)

        if not cart_was_added:
            log.error(
                "Direct cart-add failed for %s after %d attempt(s)",
                sid,
                attempt,
            )
            return None, False

        order_url = complete_checkout(target.page, email)
        if not order_url:
            last_url = target.page.url
            if "require_cookie" in last_url and "/checkout/" not in last_url:
                log.error(
                    "Checkout for %s never entered checkout flow; last URL is %s. "
                    "Allowing next target instead of stopping as a double-hold risk.",
                    sid,
                    last_url,
                )
                return None, False

            log.error(
                "Checkout failed for %s after cart-add; stopping to avoid double-hold. "
                "Last URL: %s",
                sid,
                last_url,
            )
            return None, True

        winner = {
            "slot_id": sid,
            "description": (
                f"{target.slot['weekday']} {target.slot['date']} "
                f"{target.slot['time']} {target.slot['field']}"
            ),
            "slot_url": target.slot["url"],
            "order_url": order_url,
            "booked_at": datetime.now(BERLIN_TZ).isoformat(timespec="seconds"),
        }
        return winner, True

    except Exception as e:
        log.error("Booking exception for %s: %s", sid, e)
        return None, cart_was_added


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

            # Primary path uses fetch+DOMParser, not browser reload. Do not arm
            # resource blocking here; fallback reliability is more valuable than
            # saving subresources on a rare fallback reload.

            # Wake early so setup is definitely done, then use the wake
            # window for the clock probe and connection warming. Fire at the
            # release boundary adjusted by the measured server clock offset.
            wake_utc = wake_time_utc(release_time_berlin)
            release_utc = release_time_berlin.astimezone(UTC_TZ)
            sleep_until(wake_utc, "pre-strike wake time")

            # (1) Pin the server's second boundary so we fire on the server's
            # 17:00:00, not just our own. Falls back to 0.0 (= local NTP).
            offset_s = measure_server_clock_offset(BASE_URL)
            adjusted_release_utc = release_utc - timedelta(seconds=offset_s)
            if offset_s:
                log.info(
                    "Firing at clock-adjusted release: %s (offset %+.3fs)",
                    adjusted_release_utc.astimezone(BERLIN_TZ).isoformat(
                        timespec="milliseconds"
                    ),
                    offset_s,
                )

            # (2) Refresh keep-alive connections so the strike POST does not
            # pay a TLS handshake. Skip if we are running out of window.
            for t in targets:
                seconds_left = (
                    adjusted_release_utc - datetime.now(UTC_TZ)
                ).total_seconds()
                if seconds_left < 0.5:
                    log.warning(
                        "Skipping remaining connection warming "
                        "(%.2fs to release)",
                        seconds_left,
                    )
                    break
                warm_connection(t.page, timeout_ms=600)

            sleep_until(
                adjusted_release_utc,
                "release time (clock-adjusted)",
                warn_if_more_than_s=int(PRE_STRIKE_WAKE_SECONDS) + 5,
            )

            log.info(
                "Release time. Direct booking across %d target(s). "
                "Bookings used today: %d/%d.",
                len(targets),
                booked_today,
                DAILY_BOOKING_LIMIT,
            )

            winner: dict | None = None
            attempts_per_target = {t.slot["slot_id"]: 0 for t in targets}

            # Try each target in order. Each target gets a short post-release
            # retry burst inside direct_booking_attempt() to handle jitter.
            # If checkout never enters the checkout flow and only bounces to
            # require_cookie, try the next target. If checkout reached the flow
            # and then failed, stop to avoid multiple holds.
            for target in targets:
                sid = target.slot["slot_id"]
                attempts_per_target[sid] += 1

                winner, cart_was_added = direct_booking_attempt(target, email)
                if winner:
                    break

                if cart_was_added:
                    log.error(
                        "Cart was added but checkout failed for %s after entering/possibly entering "
                        "checkout flow; stopping to avoid double-hold.",
                        sid,
                    )
                    break

                log.info("No usable checkout for %s; trying next target if available", sid)

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
            state["last_run"] = datetime.now(BERLIN_TZ).isoformat(timespec="seconds")
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
