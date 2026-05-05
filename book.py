#!/usr/bin/env python3
"""
book.py

Completes a pretix booking after a successful cart-add. Used by strike.py
to chain straight from "slot held" to "booking confirmed" in one shot.

Flow (steps 3-9 of the booking sequence):
  3. GET /checkout/start                      -> redirects to questions
  4. GET /checkout/questions/                 -> loads form
  5. POST /checkout/questions/                -> submit email
  6. GET /checkout/confirm/                   -> loads confirm page
  7. POST /checkout/confirm/?ajax=1           -> returns async_id
  8. GET /checkout/confirm/?async_id=...      -> poll until 302
  9. Follow the redirect                     -> order page

Returns the order URL on success, None on any failure.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("book")

CHECKOUT_BASE = "https://tix.htw.stura-dresden.de/beachplatz/buchung-beachplatz/checkout"
CHECKOUT_TIMEOUT = 15
ASYNC_POLL_INTERVAL_S = 0.5
ASYNC_POLL_TIMEOUT_S = 30


def _csrf(session: requests.Session) -> str | None:
    return session.cookies.get("__Host-pretix_csrftoken")


def _find_confirm_field(html: str) -> str | None:
    """
    The terms-acceptance checkbox has a dynamic name (e.g.
    confirm_confirm_text_0). Parse the confirm page HTML to find the
    real name. This makes the bot resilient to pretix re-numbering.
    """
    soup = BeautifulSoup(html, "html.parser")
    for inp in soup.find_all("input", attrs={"type": "checkbox"}):
        name = inp.get("name", "")
        if name.startswith("confirm_"):
            return name
    return None


def _submit_questions(session: requests.Session, email: str) -> bool:
    """Step 3-5: navigate to checkout and submit the email field."""
    csrf = _csrf(session)
    if not csrf:
        log.error("No CSRF token before checkout/start")
        return False

    try:
        # Step 3: triggers session-level checkout state, redirects to questions
        r = session.get(f"{CHECKOUT_BASE}/start", timeout=CHECKOUT_TIMEOUT,
                        allow_redirects=True)
        if r.status_code != 200:
            log.error("/checkout/start status %s, url=%s", r.status_code, r.url)
            return False

        # Step 5: POST email. Use latest CSRF from cookie jar.
        csrf = _csrf(session)
        questions_url = f"{CHECKOUT_BASE}/questions/"
        payload = {
            "csrfmiddlewaretoken": csrf,
            "email": email,
        }
        headers = {"Referer": questions_url}
        r = session.post(questions_url, data=payload, headers=headers,
                         timeout=CHECKOUT_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            log.error("POST /questions/ status %s", r.status_code)
            return False
        # After redirect we should be on /checkout/confirm/
        if "/checkout/confirm" not in r.url:
            log.error("Expected to land on /checkout/confirm/, got %s", r.url)
            return False
        return True
    except requests.RequestException as e:
        log.error("Questions step failed: %s", e)
        return False


def _submit_confirm(session: requests.Session) -> str | None:
    """
    Step 6-9: load confirm page, post confirmation, poll async, follow
    redirect, return the final order URL.
    """
    confirm_url = f"{CHECKOUT_BASE}/confirm/"

    try:
        # Step 6: GET confirm page (already there from the questions redirect,
        # but fetch again to ensure fresh CSRF + parse the dynamic field name).
        r = session.get(confirm_url, timeout=CHECKOUT_TIMEOUT)
        if r.status_code != 200:
            log.error("GET /confirm/ status %s", r.status_code)
            return None
        confirm_field = _find_confirm_field(r.text)
        if not confirm_field:
            log.error("Could not find confirm_* checkbox in confirm page")
            return None

        # Step 7: POST confirmation with ajax=1, get async_id
        csrf = _csrf(session)
        payload = {
            "csrfmiddlewaretoken": csrf,
            confirm_field: "yes",
            "ajax": "1",
        }
        headers = {
            "Referer": confirm_url,
            "X-Requested-With": "XMLHttpRequest",
        }
        r = session.post(confirm_url, data=payload, headers=headers,
                         timeout=CHECKOUT_TIMEOUT, allow_redirects=False)
        if r.status_code != 200:
            log.error("POST /confirm/ status %s", r.status_code)
            return None

        # Response body contains JSON with async_id, e.g.
        # {"async_id": "abc-123", "check_url": "/...?async_id=...&ajax=1"}
        try:
            data = r.json()
        except ValueError:
            log.error("POST /confirm/ did not return JSON: %s",
                      r.text[:200])
            return None
        check_url = data.get("check_url")
        if not check_url:
            # Fallback: build it ourselves
            async_id = data.get("async_id")
            if not async_id:
                log.error("No async_id or check_url in confirm response")
                return None
            check_url = f"/beachplatz/buchung-beachplatz/checkout/confirm/?async_id={async_id}&ajax=1"

        full_check_url = urljoin(confirm_url, check_url)

        # Step 8: poll until pretix says it's done
        deadline = time.time() + ASYNC_POLL_TIMEOUT_S
        order_url = None
        while time.time() < deadline:
            r = session.get(full_check_url, timeout=CHECKOUT_TIMEOUT,
                            allow_redirects=False)
            # While pending, status 200 and JSON with "ready": false
            # When done, pretix returns JSON with "redirect" to the order URL
            if r.status_code in (301, 302):
                # Some pretix versions redirect directly
                location = r.headers.get("Location")
                if location:
                    order_url = urljoin(full_check_url, location)
                    break
            elif r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    # Could be the order page itself rendered as HTML
                    if "/order/" in r.url:
                        order_url = r.url
                        break
                    log.warning("Async poll: non-JSON response, retrying")
                    time.sleep(ASYNC_POLL_INTERVAL_S)
                    continue
                if data.get("ready"):
                    redirect = data.get("redirect")
                    if redirect:
                        order_url = urljoin(full_check_url, redirect)
                        break
                # Otherwise still pending
            time.sleep(ASYNC_POLL_INTERVAL_S)

        if not order_url:
            log.error("Async poll timed out after %ds", ASYNC_POLL_TIMEOUT_S)
            return None

        log.info("Booking confirmed: %s", order_url)
        return order_url

    except requests.RequestException as e:
        log.error("Confirm step failed: %s", e)
        return None


def complete_booking(session: requests.Session, email: str) -> str | None:
    """
    Top-level entry point. Assumes session already has a slot in the cart
    (call after fire_cart_add succeeds in strike.py).

    Returns the final order URL on success, None on any failure.
    """
    if not _submit_questions(session, email):
        return None
    return _submit_confirm(session)
