#!/usr/bin/env python3
"""
beachplatz_watcher.py

Polls the StuRa HTW Dresden beach court booking system and notifies via
Telegram when previously-closed slots become bookable.

Detection: a slot's detail page contains the product section
"Mitglied Student:innenschaft HTWD" only when the slot is actually open
for booking. We use that string as the marker.

The booking system opens slots about 4 weeks in advance, so by default
the bot only checks the ISO week 4 weeks from today. Adjust WEEK_OFFSETS
to widen the window.

Designed to run on GitHub Actions on a cron schedule. State is read from
and written to ./state/beachplatz_state.json, which is restored/saved by
the Actions cache between runs. Notifications go via Telegram using the
secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config -- edit these
# ---------------------------------------------------------------------------

BASE_URL = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/"

# Bookings open about 4 weeks in advance ("Buchung 4 Wochen vorher möglich").
# We watch the week(s) at this offset relative to the current ISO week.
# Default: just +4. Use e.g. range(3, 6) to also watch +3 and +5 defensively.
WEEK_OFFSETS = [4]

# The product line that signals "open for me".
# Change to "Student:in weiterer Hochschule" if that's your category.
OPEN_MARKER = "Mitglied Student:innenschaft HTWD"

# --- Auto-hold (cart-add) settings ---
# When True, the bot will POST to pretix's cart-add endpoint for slots that
# match PREFERRED_SLOTS, holding the slot in your cart for 5 minutes.
# You then complete checkout manually via the Telegram link.
# Requires STURA_VOUCHER environment variable / GitHub secret.
AUTO_HOLD_ENABLED = True

# The bot will only auto-hold slots matching this allowlist.
# Format: list of (weekday_abbr, time, field) tuples.
# Empty list = never auto-hold (notify-only mode).
PREFERRED_SLOTS: list[tuple[str, str, str]] = [
    (weekday, time, field)
    for weekday in ("Mo", "Di", "Mi", "Do", "Fr")
    for time in ("17:00", "18:30","20:00)
    for field in ("Feld 1", "Feld 2")
]

# How long pretix holds a cart for; we use this to know when we're "free" again.
HOLD_DURATION_SECONDS = 5 * 60

# Optional filters. Leave empty to match everything in the watched weeks.
WEEKDAYS_FILTER: list[str] = []        # e.g. ["Sa", "So"]
TIME_SLOTS_FILTER: list[str] = []      # e.g. ["18:30", "20:00"]
FIELDS_FILTER: list[str] = []          # e.g. ["Feld 1"]

# The booking system releases one weekday at a time, exactly 4 weeks ahead:
# on a Tuesday, only Tuesday slots in week +4 get released. When True, the
# bot auto-filters to today's weekday (combined with WEEKDAYS_FILTER if set).
ONLY_TODAYS_WEEKDAY = True

# How many slot detail pages to fetch concurrently. 5–8 is a good range:
# fast enough to cut total runtime, polite enough not to hammer the server.
MAX_WORKERS = 6

# Polite delay between per-slot fetches (seconds, randomised). With concurrent
# requests this is per-thread, so the effective spacing is much smaller.
REQUEST_DELAY_RANGE = (0.2, 0.6)

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "state"
STATE_FILE = STATE_DIR / "beachplatz_state.json"

# HTTP
USER_AGENT = "beachplatz-watcher/1.0 (+github actions; personal use)"
TIMEOUT = 20

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("beachplatz")


# ---------------------------------------------------------------------------
# Telegram notifier
# ---------------------------------------------------------------------------

def notify_telegram(title: str, body: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram credentials missing; printing instead")
        print(f"\n{title}\n{'-'*len(title)}\n{body}\n")
        return

    text = f"*{title}*\n\n{body}"
    # Telegram message limit is 4096 chars; trim if needed.
    if len(text) > 4000:
        text = text[:3990] + "\n..."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
        r.raise_for_status()
        log.info("Telegram notification sent")
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("State file corrupt, starting fresh")
    return {"open_slots": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def iso_week_string(d: datetime) -> str:
    """Return ISO week string like '2026-W22' for a given date."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def weeks_to_watch(offsets: list[int], now: datetime | None = None) -> list[str]:
    """Compute ISO week strings for the configured offsets from today."""
    base = now or datetime.now()
    return [iso_week_string(base + timedelta(weeks=o)) for o in offsets]


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.7"})
    return s


def get_week_slots(session: requests.Session, week: str) -> list[dict]:
    url = f"{BASE_URL}?date={week}"
    log.info("Fetching week overview: %s", url)
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    slots: list[dict] = []

    for header in soup.find_all(["h3", "h4"]):
        text = header.get_text(strip=True)
        m = re.match(r"^(Mo|Di|Mi|Do|Fr|Sa|So),\s*(\d{1,2}\.\d{1,2}\.)$", text)
        if not m:
            continue
        weekday, date = m.group(1), m.group(2)
        ul = header.find_next("ul")
        if ul is None:
            continue
        for a in ul.find_all("a", href=True):
            href = a["href"]
            id_match = re.search(r"/buchung-beachplatz/(\d+)/", href)
            if not id_match:
                continue
            slot_id = id_match.group(1)
            label = " ".join(a.get_text(separator=" ", strip=True).split())
            field_match = re.search(r"(Feld\s*\d+)", label)
            time_match = re.search(r"(\d{2}:\d{2})", label)
            slots.append({
                "slot_id": slot_id,
                "url": urljoin(BASE_URL, href),
                "weekday": weekday,
                "date": date,
                "time": time_match.group(1) if time_match else "",
                "field": field_match.group(1) if field_match else "",
                "week": week,
            })
    log.info("Week %s: %d slot links found", week, len(slots))
    return slots


# Map Python's weekday() (Mon=0..Sun=6) to the page's German abbreviations.
GERMAN_WEEKDAY_ABBR = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def todays_weekday_abbr(now: datetime | None = None) -> str:
    return GERMAN_WEEKDAY_ABBR[(now or datetime.now()).weekday()]


def slot_matches_filters(slot: dict) -> bool:
    if ONLY_TODAYS_WEEKDAY and slot["weekday"] != todays_weekday_abbr():
        return False
    if WEEKDAYS_FILTER and slot["weekday"] not in WEEKDAYS_FILTER:
        return False
    if TIME_SLOTS_FILTER and slot["time"] not in TIME_SLOTS_FILTER:
        return False
    if FIELDS_FILTER and slot["field"] not in FIELDS_FILTER:
        return False
    return True


def is_slot_open(session: requests.Session, slot_url: str) -> bool:
    r = session.get(slot_url, timeout=TIMEOUT)
    if r.status_code != 200:
        log.warning("Slot %s returned HTTP %s", slot_url, r.status_code)
        return False
    return OPEN_MARKER in r.text


# ---------------------------------------------------------------------------
# Auto-hold (cart-add)
# ---------------------------------------------------------------------------

def slot_in_allowlist(slot: dict) -> bool:
    """Check if a slot matches PREFERRED_SLOTS exactly."""
    key = (slot["weekday"], slot["time"], slot["field"])
    return key in PREFERRED_SLOTS


def has_active_hold(state: dict) -> bool:
    """True if we're still inside an active 5-min hold from a prior run."""
    hold = state.get("active_hold")
    if not hold:
        return False
    try:
        held_until = datetime.fromisoformat(hold["held_until"])
    except (KeyError, ValueError):
        return False
    return datetime.now() < held_until


def try_hold_slot(slot: dict, voucher: str) -> dict | None:
    """
    Attempt to add a slot to a fresh pretix cart using the voucher.
    Returns a hold-state dict on success, None on failure.

    Each call uses a fresh session because:
    - We have no prior cart state to preserve
    - It avoids any session-leak risk between unrelated slots
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.7"})

    # 1) GET the slot page to acquire pretix_session + csrftoken cookies.
    try:
        r = sess.get(slot["url"], timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Hold prep GET failed for %s: %s", slot["slot_id"], e)
        return None

    csrf = sess.cookies.get("__Host-pretix_csrftoken")
    if not csrf:
        log.error("No CSRF token cookie after GET %s", slot["url"])
        return None

    # 2) POST to cart/add. The `next` query param tells pretix to redirect
    #    back to the slot page on success (we don't follow that). The
    #    subevent value is the slot_id; pretix uses subevent IDs for
    #    time-based variants of the same product.
    cart_url = f"{BASE_URL}cart/add?next=/beachplatz/buchung-beachplatz/{slot['slot_id']}/"
    payload = {
        "csrfmiddlewaretoken": csrf,
        "subevent": slot["slot_id"],
        "_voucher_code": voucher,
    }
    headers = {"Referer": slot["url"]}

    try:
        # allow_redirects=True so we end up on the cart page; we read its URL
        # to confirm the hold landed.
        r = sess.post(cart_url, data=payload, headers=headers,
                      timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        log.error("Cart-add POST failed for %s: %s", slot["slot_id"], e)
        return None

    # Heuristic for success: pretix redirects to a /cart/ or /checkout/ URL
    # with the slot now visible there. We also check the response body for
    # the typical cart confirmation strings.
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
    log.info("Held slot %s in cart until %s (cart url: %s)",
             slot["slot_id"], held_until.isoformat(timespec="seconds"), final_url)

    return {
        "slot_id": slot["slot_id"],
        "description": f"{slot['weekday']} {slot['date']} {slot['time']} {slot['field']}",
        "slot_url": slot["url"],
        "cart_url": final_url,
        "held_at": datetime.now().isoformat(timespec="seconds"),
        "held_until": held_until.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    previously_open: dict = state.get("open_slots", {})
    currently_open: dict = {}

    session = make_session()

    weeks = weeks_to_watch(WEEK_OFFSETS)
    log.info("Today is %s; watching weeks: %s",
             iso_week_string(datetime.now()), ", ".join(weeks))

    all_slots: list[dict] = []
    for week in weeks:
        try:
            all_slots.extend(get_week_slots(session, week))
        except requests.RequestException as e:
            log.error("Failed to fetch %s: %s", week, e)

    relevant = [s for s in all_slots if slot_matches_filters(s)]
    active_weekday = todays_weekday_abbr() if ONLY_TODAYS_WEEKDAY else "any"
    log.info("Checking %d/%d slots after filters (weekday=%s)",
             len(relevant), len(all_slots), active_weekday)

    newly_open: list[dict] = []

    def check_one(slot: dict) -> tuple[dict, bool | None]:
        """Returns (slot, open_now). open_now is None on transient failure."""
        time.sleep(random.uniform(*REQUEST_DELAY_RANGE))
        try:
            return slot, is_slot_open(session, slot["url"])
        except requests.RequestException as e:
            log.warning("Slot %s fetch failed: %s", slot["slot_id"], e)
            return slot, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(check_one, s) for s in relevant]
        for fut in as_completed(futures):
            slot, open_now = fut.result()
            sid = slot["slot_id"]

            if open_now is None:
                # Transient error: preserve previous state to avoid spurious flips.
                if sid in previously_open:
                    currently_open[sid] = previously_open[sid]
                continue

            if open_now:
                entry = {
                    "description": f"{slot['weekday']} {slot['date']} {slot['time']} {slot['field']}",
                    "url": slot["url"],
                    "week": slot["week"],
                    "first_seen_open": previously_open.get(sid, {}).get(
                        "first_seen_open", datetime.now().isoformat(timespec="seconds")
                    ),
                }
                currently_open[sid] = entry
                if sid not in previously_open:
                    # Keep both the entry (for state) and the slot dict (for hold logic).
                    newly_open.append({"entry": entry, "slot": slot})

    # --- Auto-hold logic ---
    new_hold = None
    if AUTO_HOLD_ENABLED and newly_open and not has_active_hold(state):
        voucher = os.environ.get("STURA_VOUCHER")
        if not voucher:
            log.warning("AUTO_HOLD_ENABLED but STURA_VOUCHER not set; skipping hold")
        else:
            # Pick the first newly-opened slot that matches the allowlist.
            for item in newly_open:
                if slot_in_allowlist(item["slot"]):
                    log.info("Attempting to auto-hold slot %s (%s %s %s)",
                             item["slot"]["slot_id"], item["slot"]["weekday"],
                             item["slot"]["time"], item["slot"]["field"])
                    new_hold = try_hold_slot(item["slot"], voucher)
                    if new_hold:
                        break  # Single-slot rule.
    elif AUTO_HOLD_ENABLED and has_active_hold(state):
        log.info("Active hold present; skipping any new auto-holds this run")

    # --- Notifications ---
    if new_hold:
        title = f"🛒 HELD: {new_hold['description']}"
        body = (
            f"Auto-held in cart. Complete checkout within 5 minutes:\n"
            f"{new_hold['cart_url']}\n\n"
            f"Hold expires: {new_hold['held_until']}"
        )
        notify_telegram(title, body)

    if newly_open:
        held_id = new_hold["slot_id"] if new_hold else None
        body_lines = []
        for item in newly_open:
            entry = item["entry"]
            sid = item["slot"]["slot_id"]
            marker = "🛒 (held)" if sid == held_id else "•"
            body_lines.append(f"{marker} {entry['description']}\n  {entry['url']}")
        title = f"🏐 {len(newly_open)} new beach slot(s) open"
        notify_telegram(title, "\n\n".join(body_lines))
    else:
        log.info("No new openings (currently open total: %d)", len(currently_open))

    # --- Persist state ---
    state["open_slots"] = currently_open
    if new_hold:
        state["active_hold"] = new_hold
    elif "active_hold" in state and not has_active_hold(state):
        # Expired hold: clean it up.
        del state["active_hold"]
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
