#!/usr/bin/env python3
"""
diagnose_checkout.py -- one-shot diagnostic for the checkout require_cookie
bounce after a fetch-path cart-add.

Runs the EXACT strike sequence (prewarm -> prebuilt body -> fire_cart_add
with prebuilt -> complete_checkout) against a currently-open slot, while
capturing:

  1. A HAR with response BODIES embedded (cart-add JSON, check JSON,
     checkout redirects) -> /tmp/diag_<ts>.har
  2. Set-Cookie headers on every pretix response (values redacted)
  3. Cookie request headers on every pretix document/fetch request
     (names + value prefixes) -- shows whether checkout/start carries
     the session cookie
  4. Context cookie snapshots at each stage
  5. All book_playwright INFO/WARNING logs (incl. _dump_session_cookies)

Usage:
    cd ~/beachwatche
    set -a; source .env; set +a
    .venv/bin/python diagnose_checkout.py <open_slot_id> 2>&1 | tee /tmp/diag_$(date +%H%M%S).log

WARNING: on success this creates a REAL booking for the given slot.
"""

import logging
import os
import sys
import time

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright  # noqa: E402

from book_playwright import (  # noqa: E402
    build_cart_post_body,
    cart_add_button_enabled,
    complete_checkout,
    fire_cart_add,
    prewarm_to_redeem,
)

# ---------------------------------------------------------------------------

HOST = "tix.htw.stura-dresden.de"
CART_ITEM_ID = os.environ.get("CART_ITEM_ID") or "8"  # HAR-verified product id

VOUCHER = os.environ["STURA_VOUCHER"]
EMAIL = os.environ["BOOKING_EMAIL"]

if len(sys.argv) < 2:
    print("usage: diagnose_checkout.py <open_slot_id>")
    raise SystemExit(2)
SLOT_ID = sys.argv[1]

TS = time.strftime("%H%M%S")
HAR_PATH = f"/tmp/diag_{TS}.har"


def stamp(msg: str) -> None:
    ms = int((time.time() % 1) * 1000)
    print(f"{time.strftime('%H:%M:%S')}.{ms:03d}  {msg}", flush=True)


# Route book_playwright's logger (incl. _dump_session_cookies) to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  LOG  %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)


def redact(value: str, keep: int = 8) -> str:
    if not value:
        return "<empty>"
    return f"{value[:keep]}...({len(value)} chars)"


def cookie_names_from_header(header: str) -> str:
    """'a=xyz; b=123' -> 'a=xyz...(3), b=123...(3)' with redacted values."""
    parts = []
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            n, v = chunk.split("=", 1)
            parts.append(f"{n}={redact(v, 6)}")
        elif chunk:
            parts.append(chunk)
    return ", ".join(parts) if parts else "<none>"


def snapshot_cookies(ctx, label: str) -> None:
    cookies = ctx.cookies()
    stamp(f"--- COOKIE SNAPSHOT [{label}]: {len(cookies)} cookie(s) ---")
    for c in cookies:
        stamp(
            f"    {c['name']}={redact(c.get('value', ''), 6)} "
            f"domain={c.get('domain')} path={c.get('path')} "
            f"secure={c.get('secure')} sameSite={c.get('sameSite')} "
            f"httpOnly={c.get('httpOnly')} expires={c.get('expires')}"
        )


# ---------------------------------------------------------------------------

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    ctx = browser.new_context(
        locale="de-DE",
        record_har_path=HAR_PATH,
        record_har_content="embed",   # <-- bodies included this time
        record_har_mode="full",
    )
    page = ctx.new_page()

    # --- network tracing: every pretix request/response that matters --------
    def on_request(req):
        if HOST not in req.url:
            return
        if req.resource_type not in ("document", "fetch", "xhr"):
            return
        cookie_hdr = req.headers.get("cookie", "")
        stamp(
            f"REQ  {req.method} [{req.resource_type}] {req.url}\n"
            f"        Cookie: {cookie_names_from_header(cookie_hdr)}"
        )

    def on_response(resp):
        if HOST not in resp.url:
            return
        if resp.request.resource_type not in ("document", "fetch", "xhr"):
            return
        set_cookies = [
            h["value"] for h in resp.headers_array()
            if h["name"].lower() == "set-cookie"
        ]
        loc = resp.headers.get("location", "")
        line = f"RESP {resp.status} [{resp.request.resource_type}] {resp.url}"
        if loc:
            line += f"\n        Location: {loc}"
        for sc in set_cookies:
            # 'name=value; Path=/; HttpOnly; ...' -> redact value only
            head, _, attrs = sc.partition(";")
            if "=" in head:
                n, v = head.split("=", 1)
                line += f"\n        Set-Cookie: {n}={redact(v, 6)};{attrs}"
            else:
                line += f"\n        Set-Cookie: {sc}"
        stamp(line)

    page.on("request", on_request)
    page.on("response", on_response)
    page.on(
        "framenavigated",
        lambda f: stamp(f"NAV  -> {f.url}") if f == page.main_frame else None,
    )

    # --- stage 0 -------------------------------------------------------------
    snapshot_cookies(ctx, "fresh context")

    # --- stage 1: prewarm (real navigations, redeem voucher) -----------------
    stamp("=== STAGE 1: prewarm ===")
    if not prewarm_to_redeem(page, SLOT_ID, VOUCHER):
        stamp("FAIL: prewarm")
        ctx.close(); browser.close()
        raise SystemExit(1)
    snapshot_cookies(ctx, "after prewarm")

    if not cart_add_button_enabled(page):
        stamp("button NOT enabled (slot taken or stale hold) -- try another slot")
        ctx.close(); browser.close()
        raise SystemExit(1)
    stamp("button enabled")

    # --- stage 2: prebuilt body (exactly like the strike) ---------------------
    stamp("=== STAGE 2: build prebuilt POST body ===")
    prebuilt = build_cart_post_body(
        page,
        VOUCHER,
        subevent_id=SLOT_ID if SLOT_ID.isdigit() else None,
        item_id=CART_ITEM_ID,
    )
    if prebuilt:
        stamp(f"prebuilt source={prebuilt.get('source')} action={prebuilt.get('action')}")
        stamp(f"prebuilt body fields: {prebuilt.get('body')}")
    else:
        stamp("no prebuilt body -- fire_cart_add will use fetch+parse")

    # --- stage 3: fetch-path cart-add (the strike hot path) -------------------
    stamp("=== STAGE 3: fire_cart_add (fetch path, prebuilt) ===")
    t0 = time.time()
    ok = fire_cart_add(page, timeout_ms=5000, prebuilt=prebuilt)
    stamp(f"cart-add returned {ok!r} after {time.time() - t0:.2f}s")
    snapshot_cookies(ctx, "after cart-add")
    if not ok:
        stamp("FAIL: cart-add")
        ctx.close(); browser.close()
        raise SystemExit(1)

    # --- stage 4: checkout (where the strike failed) ---------------------------
    stamp("=== STAGE 4: complete_checkout ===")
    t1 = time.time()
    url = complete_checkout(page, EMAIL)
    stamp(f"checkout returned after {time.time() - t1:.2f}s")
    snapshot_cookies(ctx, "after checkout")

    stamp(f"=== RESULT: {url} ===")
    ctx.close()   # flushes the HAR
    browser.close()

stamp(f"HAR with bodies written to {HAR_PATH}")
stamp("Send the .log and the .har for analysis.")
