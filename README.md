# Beachplatz Watcher


Polls the StuRa HTW Dresden beach court booking page and sends a Telegram
notification when previously-closed slots become bookable.

Runs entirely on GitHub Actions. No server required.

## Setup

### 1. Create a Telegram bot

1. Open Telegram, search for `@BotFather`, send `/newbot`.
2. Follow the prompts. You'll get a token like `123456789:ABCdef...`.
3. Send `/start` to your new bot in Telegram (so it can message you).
4. Get your chat ID: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   in a browser after sending the bot a message. Look for `"chat":{"id": 123456789, ...}`.

### 2. Create the GitHub repo

1. Push this folder as a new repo on GitHub. A private repo is fine.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Add two secrets:
   - `TELEGRAM_BOT_TOKEN` — the token from BotFather
   - `TELEGRAM_CHAT_ID` — your chat ID

### 3. Enable Actions

1. Go to the **Actions** tab of the repo.
2. If prompted, enable workflows.
3. The schedule starts automatically. To test immediately, click the
   **Beachplatz Watcher** workflow → **Run workflow**.

The first run will report any currently open slots as "newly open"
because it has no prior state. After that, you only get notified on
genuine closed → open transitions.

## Configuration

Edit the top of `beachplatz_watcher.py`:

- `WEEK_OFFSETS` — which ISO weeks to watch, **relative to today**. The
  booking system opens slots about 4 weeks ahead ("Buchung 4 Wochen
  vorher möglich"), so the default is `[4]`. The bot computes the
  actual week string at runtime, so you never have to update it as
  time passes. Set to e.g. `[3, 4, 5]` if you want a defensive window.
- `OPEN_MARKER` — the product line that defines "open for me".
  Default is `"Mitglied Student:innenschaft HTWD"`.
- `WEEKDAYS_FILTER`, `TIME_SLOTS_FILTER`, `FIELDS_FILTER` — optional
  narrowing filters. Empty list = match all.

Edit `.github/workflows/watcher.yml`:

- The cron line `*/10 * * * *` controls frequency. GitHub minimum is 5
  minutes but in practice scheduled runs are often delayed 10–30 min
  at peak times, so 10 minutes is a reasonable target.

## How it works

1. For each watched week, fetch `?date=YYYY-Www` and parse out all slot
   detail page links.
2. For each slot (after filters), fetch its detail page and check whether
   it contains the `OPEN_MARKER` string. That product block only renders
   when the slot is genuinely open for booking — the "Jetzt buchen" link
   on the overview page is misleading and appears even when the slot is
   not yet released.
3. Compare against `state/beachplatz_state.json` (restored from the
   Actions cache). For any newly-open slot, send a Telegram message.
4. Save new state back to the cache.

## Notes

- The Actions cache is *usually* persistent but can be evicted after 7
  days of no access or when the cache hits the 10 GB repo limit. If the
  state is lost, the next run treats all currently open slots as new and
  re-notifies once. Not the end of the world.
- A weekly keepalive commit prevents GitHub from auto-disabling the
  schedule after 60 days of repo inactivity.
- If GitHub Actions is delayed or down, you simply get the notification
  later. Slots typically stay open for hours/days once released, so this
  is fine.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python beachplatz_watcher.py
```

Without the env vars set, notifications print to stdout instead.
