#!/usr/bin/env python3
“””
beachplatz_watcher.py

Polls the StuRa HTW Dresden beach court booking system and notifies via
Telegram when previously-closed slots become bookable.

Detection: a slot’s detail page contains the product section
“Mitglied Student:innenschaft HTWD” only when the slot is actually open
for booking. We use that string as the marker.

The booking system opens slots about 4 weeks in advance, so by default
the bot only checks the ISO week 4 weeks from today. Adjust WEEK_OFFSETS
to widen the window.

Designed to run on GitHub Actions on a cron schedule. State is read from
and written to ./state/beachplatz_state.json, which is restored/saved by
the Actions cache between runs. Notifications go via Telegram using the
secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
“””

from **future** import annotations

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

# —————————————————————————

# Config – edit these

# —————————————————————————

BASE_URL = “https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/”

# Bookings open about 4 weeks in advance (“Buchung 4 Wochen vorher möglich”).

# We watch the week(s) at this offset relative to the current ISO week.

# Default: just +4. Use e.g. range(3, 6) to also watch +3 and +5 defensively.

WEEK_OFFSETS = [4]

# The product line that signals “open for me”.

# Change to “Student:in weiterer Hochschule” if that’s your category.

OPEN_MARKER = “Mitglied Student:innenschaft HTWD”

# Optional filters. Leave empty to match everything in the watched weeks.

WEEKDAYS_FILTER: list[str] = []        # e.g. [“Sa”, “So”]
TIME_SLOTS_FILTER: list[str] = []      # e.g. [“18:30”, “20:00”]
FIELDS_FILTER: list[str] = []          # e.g. [“Feld 1”]

# The booking system releases one weekday at a time, exactly 4 weeks ahead:

# on a Tuesday, only Tuesday slots in week +4 get released. When True, the

# bot auto-filters to today’s weekday (combined with WEEKDAYS_FILTER if set).

ONLY_TODAYS_WEEKDAY = True

# How many slot detail pages to fetch concurrently. 5–8 is a good range:

# fast enough to cut total runtime, polite enough not to hammer the server.

MAX_WORKERS = 6

# Polite delay between per-slot fetches (seconds, randomised). With concurrent

# requests this is per-thread, so the effective spacing is much smaller.

REQUEST_DELAY_RANGE = (0.2, 0.6)

# Paths

SCRIPT_DIR = Path(**file**).resolve().parent
STATE_DIR = SCRIPT_DIR / “state”
STATE_FILE = STATE_DIR / “beachplatz_state.json”

# HTTP

USER_AGENT = “beachplatz-watcher/1.0 (+github actions; personal use)”
TIMEOUT = 20

# —————————————————————————

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s %(levelname)s %(message)s”,
handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(“beachplatz”)

# —————————————————————————

# Telegram notifier

# —————————————————————————

def notify_telegram(title: str, body: str) -> None:
token = os.environ.get(“TELEGRAM_BOT_TOKEN”)
chat_id = os.environ.get(“TELEGRAM_CHAT_ID”)
if not token or not chat_id:
log.warning(“Telegram credentials missing; printing instead”)
print(f”\n{title}\n{’-’*len(title)}\n{body}\n”)
return

```
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
```

# —————————————————————————

# State

# —————————————————————————

def load_state() -> dict:
if STATE_FILE.exists():
try:
return json.loads(STATE_FILE.read_text(encoding=“utf-8”))
except json.JSONDecodeError:
log.warning(“State file corrupt, starting fresh”)
return {“open_slots”: {}}

def save_state(state: dict) -> None:
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE.write_text(
json.dumps(state, indent=2, ensure_ascii=False), encoding=“utf-8”
)

# —————————————————————————

# Scraping

# —————————————————————————

def iso_week_string(d: datetime) -> str:
“”“Return ISO week string like ‘2026-W22’ for a given date.”””
iso_year, iso_week, _ = d.isocalendar()
return f”{iso_year}-W{iso_week:02d}”

def weeks_to_watch(offsets: list[int], now: datetime | None = None) -> list[str]:
“”“Compute ISO week strings for the configured offsets from today.”””
base = now or datetime.now()
return [iso_week_string(base + timedelta(weeks=o)) for o in offsets]

def make_session() -> requests.Session:
s = requests.Session()
s.headers.update({“User-Agent”: USER_AGENT, “Accept-Language”: “de,en;q=0.7”})
return s

def get_week_slots(session: requests.Session, week: str) -> list[dict]:
url = f”{BASE_URL}?date={week}”
log.info(“Fetching week overview: %s”, url)
r = session.get(url, timeout=TIMEOUT)
r.raise_for_status()

```
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
```

# Map Python’s weekday() (Mon=0..Sun=6) to the page’s German abbreviations.

GERMAN_WEEKDAY_ABBR = [“Mo”, “Di”, “Mi”, “Do”, “Fr”, “Sa”, “So”]

def todays_weekday_abbr(now: datetime | None = None) -> str:
return GERMAN_WEEKDAY_ABBR[(now or datetime.now()).weekday()]

def slot_matches_filters(slot: dict) -> bool:
if ONLY_TODAYS_WEEKDAY and slot[“weekday”] != todays_weekday_abbr():
return False
if WEEKDAYS_FILTER and slot[“weekday”] not in WEEKDAYS_FILTER:
return False
if TIME_SLOTS_FILTER and slot[“time”] not in TIME_SLOTS_FILTER:
return False
if FIELDS_FILTER and slot[“field”] not in FIELDS_FILTER:
return False
return True

def is_slot_open(session: requests.Session, slot_url: str) -> bool:
r = session.get(slot_url, timeout=TIMEOUT)
if r.status_code != 200:
log.warning(“Slot %s returned HTTP %s”, slot_url, r.status_code)
return False
return OPEN_MARKER in r.text

# —————————————————————————

# Main

# —————————————————————————

def main() -> int:
state = load_state()
previously_open: dict = state.get(“open_slots”, {})
currently_open: dict = {}

```
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
                newly_open.append(entry)

if newly_open:
    title = f"🏐 {len(newly_open)} new beach slot(s) open"
    body_lines = [f"• {s['description']}\n  {s['url']}" for s in newly_open]
    notify_telegram(title, "\n\n".join(body_lines))
else:
    log.info("No new openings (currently open total: %d)", len(currently_open))

state["open_slots"] = currently_open
state["last_run"] = datetime.now().isoformat(timespec="seconds")
save_state(state)
return 0
```

if **name** == “**main**”:
sys.exit(main())
