#!/usr/bin/env python3
"""
strike.py

Strike mode for the beachplatz watcher. Runs ~10s before 17:00 and 20:00 to
race against other bots competing for slots that open at those times.

Optimisations vs. the regular watcher:
- Discovers target slot URLs once at startup (single week-overview fetch).
- Maintains a persistent requests.Session per target slot so HTTP connections
  stay warm (TLS handshake amortised, cookies live in jar).
- Pre-builds the cart-add POST URL, headers, and payload per slot. When a
  slot opens, only a single POST is needed -- no GET+parse+POST.
- Reads the CSRF token from the live cookie jar at POST time, so it stays
  fresh even after many polling GETs.
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

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beachplatz_watcher import (  # noqa: E402
    BASE_URL,
    HOLD_DURATION_SECONDS,
    OPEN_MARKER,
    PREFERRED_SLOTS,
    TIMEOUT,
    USER_AGENT,
    WEEK_OFFSETS,
    get_week_slots,
    has_active_hold,
    load_state,
    notify_telegram,
    save_state,
    todays_weekday_abbr,
    weeks_to_watch,
)
from book import complete_booking  # noqa: E402

# ---------------------------------------------------------------------------
# Strike-specific config
# ---------------------------------------------------------------------------

# Total time to keep polling once the script starts. We're triggered ~10s
# early, so 130s comfortably covers the actual 17:00 release window plus
# margin for clock drift.
STRIKE_DURATION_SECONDS = 130

# Polling interval per target slot, in milliseconds. Each target has its
# own thread, so this is per-slot, not aggregate.
POLL_INTERVAL_MS = 100

# Hours (Berlin time) at which slots actually open. Script triggers ~90s
# early via systemd; after pre-warming, it sleeps until the next of these
# moments before starting the polling loop. This guarantees polling starts
# AT release time, not whenever pre-warm finishes.
STRIKE_HOURS = (17, 20)

# Maximum bookings the bot may create per calendar day. Counted via the
# state file's "booking_log" entry.
DAILY_BOOKING_LIMIT = 2

# Weekdays the bot will NOT strike on (in addition to weekend exclusion
# handled by the systemd timer). Fridays are reserved for non-bot users.
EXCLUDED_WEEKDAYS = {"Fr"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("strike")


# ---------------------------------------------------------------------------
# Pre-warmed target
# ---------------------------------------------------------------------------

@dataclass
class Target:
    slot: dict
    session: requests.Session
    cart_url: str  # Pre-built cart-add POST URL


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "de,en;q=0.7",
    })
    return s


def discover_target_slots(session: requests.Session) -> list[dict]:
    """Find slots in week +4 on today's weekday matching PREFERRED_SLOTS."""
    today_abbr = todays_weekday_abbr()
    weeks = weeks_to_watch(WEEK_OFFSETS)
    if not weeks:
        return []

    all_slots: list[dict] = []
    for week in weeks:
        try:
            all_slots.extend(get_week_slots(session, week))
        except requests.RequestException as e:
            log.error("Week overview failed: %s", e)
            return []

    targets = [
        s for s in all_slots
        if s["weekday"] == today_abbr
        and (s["weekday"], s["time"], s["field"]) in PREFERRED_SLOTS
    ]
    log.info("Found %d target slot(s) for %s in week %s",
             len(targets), today_abbr, weeks[0])
    for s in targets:
        log.info("  - %s %s %s [id=%s]",
                 s["weekday"], s["time"], s["field"], s["slot_id"])
    return targets


def prepare_target(slot: dict) -> Target | None:
    """
    Pre-warm a session for a single slot:
    - Create a fresh requests.Session
    - GET the slot detail page once (acquires pretix_session + csrftoken cookies)
    - Build the cart-add POST URL once

    Returns a Target with everything ready to fire when the slot opens.
    """
    session = make_session()
    try:
        r = session.get(slot["url"], timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Could not warm session for %s: %s", slot["slot_id"], e)
        return None

    if not session.cookies.get("__Host-pretix_csrftoken"):
        log.error("No CSRF cookie after warming %s", slot["slot_id"])
        return None

    cart_url = (
        f"{BASE_URL}cart/add"
        f"?next=/beachplatz/buchung-beachplatz/{slot['slot_id']}/"
    )
    return Target(slot=slot, session=session, cart_url=cart_url)


def fire_cart_add(target: Target, voucher: str) -> dict | None:
    """
    Fire the pre-built cart-add POST. Reads the *current* CSRF from the
    cookie jar to handle any rotation that happened during polling.

    Returns hold-state dict on success, None on failure.
    """
    csrf = target.session.cookies.get("__Host-pretix_csrftoken")
    if not csrf:
        log.error("CSRF cookie disappeared from %s session", target.slot["slot_id"])
        return None

    payload = {
        "csrfmiddlewaretoken": csrf,
        "subevent": target.slot["slot_id"],
        "_voucher_code": voucher,
    }
    headers = {"Referer": target.slot["url"]}

    try:
        r = target.session.post(
            target.cart_url, data=payload, headers=headers,
            timeout=TIMEOUT, allow_redirects=True,
        )
    except requests.RequestException as e:
        log.error("Cart-add POST failed for %s: %s", target.slot["slot_id"], e)
        return None

    final_url = r.url
    body_lower = r.text.lower() if r.text else ""
    looks_like_cart = (
        "/cart" in final_url
        or "/checkout" in final_url
        or "warenkorb" in body_lower
    )
    if r.status_code != 200 or not looks_like_cart:
        log.error("Cart-add did not land in cart (status=%s, url=%s)",
                  r.status_code, final_url)
        return None

    held_until = datetime.now() + timedelta(seconds=HOLD_DURATION_SECONDS)
    log.info("HOLD SUCCESS %s until %s",
             target.slot["slot_id"], held_until.isoformat(timespec="seconds"))

    return {
        "slot_id": target.slot["slot_id"],
        "description": (
            f"{target.slot['weekday']} {target.slot['date']} "
            f"{target.slot['time']} {target.slot['field']}"
        ),
        "slot_url": target.slot["url"],
        "cart_url": final_url,
        "held_at": datetime.now().isoformat(timespec="seconds"),
        "held_until": held_until.isoformat(timespec="seconds"),
    }


def poll_target(target: Target, deadline: float, voucher: str,
                stop_signal: dict, hold_lock) -> dict | None:
    """
    Loop polling a single target until either:
    - the slot opens (then fire cart-add and return result)
    - another thread already won (stop_signal["held"] is truthy)
    - deadline is reached

    Each poll is a GET on the slot URL using the pre-warmed session,
    so connections stay alive via HTTP keep-alive.

    The hold_lock + stop_signal pair enforces the single-slot rule:
    only one thread can be inside the cart-add critical section at a
    time, and that thread re-checks stop_signal *while holding the lock*
    so a second thread can't race past.
    """
    poll_interval = POLL_INTERVAL_MS / 1000.0
    sid = target.slot["slot_id"]
    rounds = 0

    while time.time() < deadline:
        if stop_signal.get("held"):
            return None  # Another thread won; we exit politely

        rounds += 1
        round_start = time.time()
        try:
            r = target.session.get(target.slot["url"], timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("Poll error on %s: %s", sid, e)
            time.sleep(poll_interval)
            continue

        if r.status_code == 200 and OPEN_MARKER in r.text:
            log.info("OPEN DETECTED %s after %d rounds", sid, rounds)

            # Critical section: serialize cart-add attempts. Re-check
            # stop_signal *inside* the lock so a thread that's been
            # waiting on the lock can't fire after another already won.
            with hold_lock:
                if stop_signal.get("held"):
                    log.info("Another thread already won; aborting %s", sid)
                    return None
                hold = fire_cart_add(target, voucher)
                if hold:
                    stop_signal["held"] = True
                    # Return both the hold dict AND the target, so the
                    # caller can use the target's session to complete
                    # checkout.
                    return {"hold": hold, "target": target}
                else:
                    log.warning("Saw %s open but cart-add failed; abandoning",
                                sid)
                return None

        elapsed = time.time() - round_start
        if elapsed < poll_interval:
            time.sleep(poll_interval - elapsed)

    log.info("Deadline reached for %s without opening (%d rounds)",
             sid, rounds)
    return None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

BERLIN_TZ = ZoneInfo("Europe/Berlin")


def next_strike_time() -> datetime:
    """
    Returns the next datetime (in UTC) at which a strike-hour boundary
    occurs in Berlin time. Used to align the polling start with the
    actual slot release moment.
    """
    now_berlin = datetime.now(BERLIN_TZ)
    candidates = []
    for hour in STRIKE_HOURS:
        # Today at HH:00:00 Berlin
        candidate = now_berlin.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now_berlin:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates).astimezone(ZoneInfo("UTC"))


def sleep_until_strike_time() -> None:
    """
    Sleep until the next 17:00 or 20:00 Berlin time, whichever comes first.
    Caps wait at 5 minutes -- if we're somehow more than 5 minutes early
    we proceed immediately rather than block forever.
    """
    target_utc = next_strike_time()
    now_utc = datetime.now(ZoneInfo("UTC"))
    wait_seconds = (target_utc - now_utc).total_seconds()

    if wait_seconds <= 0:
        log.info("Strike time already passed (%.2fs late). Polling now.",
                 -wait_seconds)
        return
    if wait_seconds > 300:
        log.warning("Strike time is %.0fs away (>5min). Polling now anyway.",
                    wait_seconds)
        return

    log.info("Pre-warm complete. Sleeping %.2fs until strike time %s",
             wait_seconds, target_utc.astimezone(BERLIN_TZ).isoformat(timespec="seconds"))
    time.sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Booking limit helpers
# ---------------------------------------------------------------------------

def bookings_today(state: dict) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    log_entries = state.get("booking_log", [])
    return sum(1 for e in log_entries if e.get("booked_at", "").startswith(today))


def record_booking(state: dict, booking: dict) -> None:
    """Append a successful booking to the persistent log."""
    log_entries = state.setdefault("booking_log", [])
    log_entries.append(booking)
    # Trim log to last 50 entries to keep file size bounded
    if len(log_entries) > 50:
        state["booking_log"] = log_entries[-50:]


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
        log.info("Today (%s) is in EXCLUDED_WEEKDAYS; skipping strike",
                 today_abbr)
        return 0

    state = load_state()

    # Hard daily-limit gate.
    booked_today = bookings_today(state)
    if booked_today >= DAILY_BOOKING_LIMIT:
        log.info("Daily booking limit (%d) already reached; skipping strike",
                 DAILY_BOOKING_LIMIT)
        return 0

    # Discover targets.
    discover_session = make_session()
    target_slots = discover_target_slots(discover_session)
    if not target_slots:
        log.info("No target slots to strike for; exiting")
        return 0

    log.info("Pre-warming %d target session(s)...", len(target_slots))
    targets: list[Target] = []
    for s in target_slots:
        t = prepare_target(s)
        if t:
            targets.append(t)
        else:
            log.warning("Skipping %s due to warm-up failure", s["slot_id"])

    if not targets:
        log.error("No targets could be warmed; exiting")
        return 0

    # Sleep until the next strike-hour boundary in Berlin time. We were
    # triggered ~90s early by systemd; pre-warm consumes some of that
    # margin; whatever's left, we wait so polling starts AT release time.
    sleep_until_strike_time()

    log.info("Strike time reached. Polling for %ds, %dms per cycle. "
             "Bookings used today: %d/%d.",
             STRIKE_DURATION_SECONDS, POLL_INTERVAL_MS,
             booked_today, DAILY_BOOKING_LIMIT)

    deadline = time.time() + STRIKE_DURATION_SECONDS
    stop_signal: dict = {"held": False}
    hold_lock = threading.Lock()
    winner = None

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [
            pool.submit(poll_target, t, deadline, voucher, stop_signal, hold_lock)
            for t in targets
        ]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                winner = result
                break

    if not winner:
        log.info("Strike ended with no hold")
        notify_telegram(
            "⚠️ Strike: no slots held",
            "Polling completed but no slot was successfully held.",
        )
        return 0

    held_slot = winner["hold"]
    target = winner["target"]
    log.info("Held slot %s. Proceeding to complete booking...",
             held_slot["slot_id"])

    # Complete the booking using the same session that did the cart-add.
    order_url = complete_booking(target.session, email)

    if not order_url:
        log.error("Cart-add succeeded but booking completion failed for %s",
                  held_slot["slot_id"])
        notify_telegram(
            "⚠️ Booking incomplete",
            f"Held {held_slot['description']} but checkout failed. "
            f"Slot will release in 5 minutes.\n\n"
            f"Cart URL (try in browser ASAP):\n{held_slot['cart_url']}",
        )
        return 0

    # Success.
    booking_record = {
        "slot_id": held_slot["slot_id"],
        "description": held_slot["description"],
        "slot_url": held_slot["slot_url"],
        "order_url": order_url,
        "booked_at": datetime.now().isoformat(timespec="seconds"),
    }
    record_booking(state, booking_record)
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    # Don't keep "active_hold" — the booking is done, hold is moot.
    state.pop("active_hold", None)
    save_state(state)

    title = f"✅ BOOKED: {held_slot['description']}"
    body = (
        f"Booking confirmed and paid (€0 with voucher).\n\n"
        f"Order details / cancel link:\n{order_url}\n\n"
        f"Who's coming? Reply in this channel.\n"
        f"Slot used today: {booked_today + 1}/{DAILY_BOOKING_LIMIT}."
    )
    notify_telegram(title, body)
    return 0


if __name__ == "__main__":
    sys.exit(strike())
