#!/usr/bin/env python3
"""
reminder.py

Runs daily, scans the booking log for upcoming bookings, and sends a
Telegram channel message ~24 hours before each booking. The message
prompts the channel to confirm attendance or cancel via the order URL.

Designed to run once a day via systemd timer.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beachplatz_watcher import (  # noqa: E402
    load_state,
    notify_telegram,
    save_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("reminder")

# Window: send reminder for bookings happening between (now+18h) and (now+30h).
# Daily cron at noon will reliably hit this window for any booking the next day.
REMINDER_WINDOW_START_HOURS = 18
REMINDER_WINDOW_END_HOURS = 30


def parse_slot_datetime(description: str) -> datetime | None:
    """
    Parse strings like 'Mo 1.6. 18:30 Feld 1' or 'Mo 01.06. 18:30 Feld 1'
    into a datetime. Year is current year (or next, if the date has
    already passed).
    """
    m = re.match(r"^\S+\s+(\d{1,2})\.(\d{1,2})\.\s+(\d{1,2}):(\d{2})", description)
    if not m:
        return None
    day, month, hour, minute = (int(g) for g in m.groups())
    now = datetime.now()
    candidate = datetime(now.year, month, day, hour, minute)
    # If the parsed date is more than 30 days in the past, assume next year
    if candidate < now - timedelta(days=30):
        candidate = candidate.replace(year=now.year + 1)
    return candidate


def main() -> int:
    state = load_state()
    bookings = state.get("booking_log", [])
    if not bookings:
        log.info("No bookings in log; nothing to remind about")
        return 0

    reminders_sent_for: set[str] = set(state.get("reminders_sent", []))
    now = datetime.now()
    window_start = now + timedelta(hours=REMINDER_WINDOW_START_HOURS)
    window_end = now + timedelta(hours=REMINDER_WINDOW_END_HOURS)

    upcoming = []
    for booking in bookings:
        slot_id = booking.get("slot_id")
        if not slot_id or slot_id in reminders_sent_for:
            continue
        slot_dt = parse_slot_datetime(booking.get("description", ""))
        if not slot_dt:
            log.warning("Could not parse slot datetime from: %r",
                        booking.get("description"))
            continue
        if window_start <= slot_dt <= window_end:
            upcoming.append((booking, slot_dt))

    if not upcoming:
        log.info("No bookings in the 18-30h reminder window")
        return 0

    for booking, slot_dt in upcoming:
        title = f"🏐 Tomorrow: {booking['description']}"
        body = (
            f"Reminder: a court is booked for tomorrow.\n\n"
            f"Time: {slot_dt.strftime('%a %d.%m. %H:%M')}\n"
            f"Order/cancel link: {booking.get('order_url', '(missing)')}\n\n"
            f"Confirm attendance below, or use the link above to cancel "
            f"if no one can come (please cancel by tonight at the latest "
            f"so others get a fair chance)."
        )
        notify_telegram(title, body)
        reminders_sent_for.add(booking["slot_id"])
        log.info("Sent reminder for %s", booking["slot_id"])

    state["reminders_sent"] = sorted(reminders_sent_for)
    # Trim reminders_sent if it grows unbounded
    if len(state["reminders_sent"]) > 100:
        state["reminders_sent"] = state["reminders_sent"][-100:]
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
