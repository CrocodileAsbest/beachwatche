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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta

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

# ---------------------------------------------------------------------------
# Strike-specific config
# ---------------------------------------------------------------------------

# Total time to keep polling once the script starts. We're triggered ~10s
# early, so 130s comfortably covers the actual 17:00 release window plus
# margin for clock drift.
STRIKE_DURATION_SECONDS = 130

# Polling interval per target slot, in milliseconds. Each target has its
# own thread, so this is per-slot, not aggregate.
POLL_INTERVAL_MS = 80

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
                stop_signal: dict) -> dict | None:
    """
    Loop polling a single target until either:
    - the slot opens (then fire cart-add and return result)
    - another thread already won (stop_signal["held"] is truthy)
    - deadline is reached

    Each poll is a GET on the slot URL using the pre-warmed session,
    so connections stay alive via HTTP keep-alive.
    """
    poll_interval = POLL_INTERVAL_MS / 1000.0
    sid = target.slot["slot_id"]
    rounds = 0
    saw_open_at = None

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
            saw_open_at = time.time()
            log.info("OPEN DETECTED %s after %d rounds", sid, rounds)
            # Fire immediately. Don't release the GIL/sleep.
            hold = fire_cart_add(target, voucher)
            if hold:
                stop_signal["held"] = True
                return hold
            else:
                # Failed to grab even though it appeared open. Other bot
                # likely won the cart-add race or pretix rejected. Stop
                # trying this slot to avoid pointless POST spam.
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
# Main
# ---------------------------------------------------------------------------

def strike() -> int:
    voucher = os.environ.get("STURA_VOUCHER")
    if not voucher:
        log.error("STURA_VOUCHER not set; cannot strike")
        return 1

    state = load_state()
    if has_active_hold(state):
        log.info("Active hold present from earlier; skipping strike")
        return 0

    # Discover targets via shared session (one-shot, can be ephemeral).
    discover_session = make_session()
    target_slots = discover_target_slots(discover_session)
    if not target_slots:
        log.info("No target slots to strike for; exiting")
        return 0

    # Pre-warm one persistent session per target. This gives us:
    # - cookies (pretix_session, csrftoken) ready to go
    # - HTTP connection kept alive for fast polling
    # - cart-add URL pre-built
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

    log.info("Pre-warm complete. Polling for %ds, %dms per cycle.",
             STRIKE_DURATION_SECONDS, POLL_INTERVAL_MS)

    deadline = time.time() + STRIKE_DURATION_SECONDS
    stop_signal: dict = {"held": False}
    held_slot = None

    # One thread per target. Each polls its assigned slot independently.
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [
            pool.submit(poll_target, t, deadline, voucher, stop_signal)
            for t in targets
        ]
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                held_slot = result
                # stop_signal was already set by the winning thread; other
                # threads will exit on their next iteration.
                break

    if held_slot:
        title = f"🛒 STRIKE: {held_slot['description']}"
        body = (
            f"Auto-held in cart. Complete checkout within 5 minutes:\n"
            f"{held_slot['cart_url']}\n\n"
            f"Hold expires: {held_slot['held_until']}"
        )
        notify_telegram(title, body)
        state["active_hold"] = held_slot
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return 0

    log.info("Strike ended with no hold")
    notify_telegram(
        "⚠️ Strike: no slots held",
        "Polling completed but no slot was successfully held. "
        "Either no release happened, or the other bot was faster.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(strike())
