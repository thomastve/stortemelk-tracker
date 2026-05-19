# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.44",
#   "httpx>=0.27",
# ]
# ///
"""
Stortemelk camping availability monitor.

Checks every 15 minutes whether any camping spot is available for the
configured dates. Sends a push notification via ntfy.sh when spots open up.

HOW IT WORKS
------------
The booking page (zoek-en-boek) loads a Tommy Booking Support calendar widget
that automatically fires a /widget/calendar API call on page load. That response
contains day-by-day availability for the full coming year. We intercept it and
check if any of our target dates flips to available=True.

SETUP (run once)
----------------
  uv run stortemelk_monitor.py --install-browser

RUN
---
  uv run stortemelk_monitor.py            # headless, runs every 15 minutes
  uv run stortemelk_monitor.py --test     # single check, then exit (for testing)
"""

import asyncio
import datetime
import logging
import os
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

# Dates to watch — a notification fires ONLY when EVERY date in this list is
# marked available (continuous stay). Partial availability (e.g. one stray date
# flipping open) is not bookable as a full stay, so it should not alert.
# Format: "YYYY-MM-DD"
WATCH_DATES = [f"2026-08-{d:02d}" for d in range(26, 32)]  # Aug 26 – Aug 31 (6 nights)

# The booking page URL (pre-selects the camping / Kamperen category)
BOOKING_URL = (
    "https://www.stortemelk.nl/zoek-en-boek"
    "?period_modifier=3&house=%5B%2283%22%5D"
)

# How often to check
CHECK_INTERVAL_MIN = 15

# After this many consecutive failures, pause for 1 hour before retrying
MAX_ERRORS = 5

# Set False to keep monitoring even after a notification has been sent
STOP_AFTER_ALERT = True

# ntfy.sh push notification
# 1. Change NTFY_TOPIC to something unique and hard to guess (e.g. add random digits)
# 2. Open https://ntfy.sh/<your-topic> in a browser, or subscribe in the ntfy app
# When running on GitHub Actions, set NTFY_TOPIC as a repository secret instead.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "stortemelk-tracker-abc123")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"

# Optional email forwarding. When set, ntfy.sh forwards the message body to these
# addresses (in addition to the push notification). Comma-separated for multiple
# recipients — we send one ntfy POST per address because ntfy's `Email:` header
# only accepts a single address. Free public ntfy instance limits ~5 emails/day
# per egress IP, which is fine because STOP_AFTER_ALERT=True caps us at one
# notification event (so at most N emails where N = number of recipients).
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()
EMAIL_RECIPIENTS = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

# ── LOGGING ───────────────────────────────────────────────────────────────────

log_path = Path(__file__).parent / "stortemelk_monitor.log"

_stream_handler = logging.StreamHandler()
_stream_handler.stream = open(
    sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _stream_handler,
        logging.FileHandler(log_path, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── CORE CHECK ────────────────────────────────────────────────────────────────

async def check_availability() -> tuple[bool, list[str]]:
    """
    Load the booking page and intercept the calendar API response.

    Returns:
        (all_nights_open, available_dates) — `all_nights_open` is True only if
        EVERY date in WATCH_DATES is marked available (continuous stay).
        `available_dates` is the sorted subset of WATCH_DATES currently flagged
        available — may be partial; useful for debug logging when not all open.

    Raises:
        RuntimeError  if no calendar API response was captured
        PlaywrightTimeout  if the page never loaded
    """
    captured: dict = {}
    watch_set = set(WATCH_DATES)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="nl-NL",
            timezone_id="Europe/Amsterdam",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def on_response(response):
            if "widget/calendar" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    entries = body.get("data", [])
                    if entries:
                        captured["entries"] = entries
                        captured["url"] = response.url
                        log.debug("Intercepted calendar API: %s (%d entries)", response.url[:80], len(entries))
                except Exception as e:
                    log.debug("Could not parse calendar response: %s", e)

        page.on("response", on_response)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
            # Dismiss cookie popup if present
            for sel in [
                "button#gdpr-cookie-accept",
                "button:has-text('Akkoord')",
                "button:has-text('Accepteren')",
                "button:has-text('Accept all')",
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        log.debug("Dismissed cookie popup")
                        break
                except Exception:
                    pass
            # Wait for the calendar API call to complete
            await page.wait_for_timeout(6_000)
        finally:
            await browser.close()

    if "entries" not in captured:
        raise RuntimeError(
            "No calendar API response captured — the widget may not have loaded. "
            f"URL attempted: {BOOKING_URL}"
        )

    available_dates = sorted(
        entry.get("date", "")
        for entry in captured["entries"]
        if entry.get("date", "") in watch_set and entry.get("available") is True
    )
    all_nights_open = set(available_dates) == watch_set
    return all_nights_open, available_dates


# ── NOTIFICATION ──────────────────────────────────────────────────────────────

def send_notification(available_dates: list[str]):
    dates_str = ", ".join(sorted(available_dates))
    message = (
        f"Kampeerplaatsen beschikbaar op Stortemelk!\n"
        f"Beschikbare datum(s): {dates_str}\n"
        f"Boek nu: {BOOKING_URL}"
    )
    base_headers = {
        "Title": "Stortemelk beschikbaar!",
        "Priority": "urgent",
        "Tags": "tent,camping,netherlands",
    }
    # First POST always sends the push notification. If there are email
    # recipients, attach the first one to this POST and POST again (push-only)
    # for each additional recipient — ntfy's `Email:` header is single-address.
    posts: list[dict[str, str]] = []
    if EMAIL_RECIPIENTS:
        for addr in EMAIL_RECIPIENTS:
            posts.append({**base_headers, "Email": addr})
    else:
        posts.append(base_headers)

    for headers in posts:
        try:
            httpx.post(
                NTFY_URL,
                content=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
        except Exception as e:
            log.error("Failed to send ntfy notification (headers=%s): %s", headers, e)
    log.info(
        "ntfy notification sent -> %s%s",
        NTFY_URL,
        f" + email to {', '.join(EMAIL_RECIPIENTS)}" if EMAIL_RECIPIENTS else "",
    )


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

async def main():
    log.info("Stortemelk monitor started - checking every %d minutes", CHECK_INTERVAL_MIN)
    log.info("Watching dates: %s", ", ".join(WATCH_DATES))
    log.info("ntfy topic: %s", NTFY_URL)

    consecutive_errors = 0

    while True:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            all_open, available_dates = await check_availability()
            consecutive_errors = 0

            if all_open:
                log.info("[%s] ALL NIGHTS AVAILABLE! Dates: %s — sending notification!", ts, ", ".join(available_dates))
                send_notification(available_dates)
                if STOP_AFTER_ALERT:
                    log.info("Stopping monitor (STOP_AFTER_ALERT=True). Book fast!")
                    break
                else:
                    log.info("Continuing to monitor (STOP_AFTER_ALERT=False).")
            elif available_dates:
                log.info(
                    "[%s] Partial availability only: %s (need all of %s). Next check in %d min.",
                    ts, ", ".join(available_dates), ", ".join(sorted(WATCH_DATES)), CHECK_INTERVAL_MIN,
                )
            else:
                log.info("[%s] Not available. Next check in %d min.", ts, CHECK_INTERVAL_MIN)

        except PlaywrightTimeout as e:
            consecutive_errors += 1
            log.warning("[%s] Page load timeout (error %d/%d): %s", ts, consecutive_errors, MAX_ERRORS, e)
        except Exception as e:
            consecutive_errors += 1
            log.error("[%s] Check failed (error %d/%d): %s", ts, consecutive_errors, MAX_ERRORS, e)

        if consecutive_errors >= MAX_ERRORS:
            log.error("Too many consecutive errors - pausing 1 hour before retrying.")
            await asyncio.sleep(3600)
            consecutive_errors = 0
            continue

        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)


# ── ENTRY POINTS ──────────────────────────────────────────────────────────────

async def run_single_test():
    """Run one check and print the result, then exit (no notification sent)."""
    log.info("TEST MODE - running a single check (no notification will be sent)...")
    all_open, dates = await check_availability()
    if all_open:
        log.info("RESULT: ALL NIGHTS AVAILABLE on %s", ", ".join(dates))
    elif dates:
        log.info(
            "RESULT: Partial only: %s (need all of %s)",
            ", ".join(dates),
            ", ".join(sorted(WATCH_DATES)),
        )
    else:
        log.info("RESULT: Not available (all dates blocked, as expected)")
    log.info("Test complete.")


async def run_once():
    """
    Run one check, send a notification if ALL watched nights are available,
    then exit. Used by GitHub Actions — runs on every scheduled trigger.
    """
    log.info("ONCE MODE - single check (notification sent only if all %d nights available)...", len(WATCH_DATES))
    all_open, dates = await check_availability()
    if all_open:
        log.info("ALL NIGHTS AVAILABLE on %s - sending notification!", ", ".join(dates))
        send_notification(dates)
    elif dates:
        log.info(
            "Partial availability only: %s (need all of %s) - no notification.",
            ", ".join(dates),
            ", ".join(sorted(WATCH_DATES)),
        )
    else:
        log.info("Not available.")


def send_heartbeat():
    """
    Send a daily 'still running' notification so you know the system is alive.
    Includes a direct link to the booking page so you can check manually too.
    """
    message = (
        f"Stortemelk tracker is actief - geen beschikbaarheid gevonden voor 26-31 augustus 2026.\n"
        f"Bekijk zelf: {BOOKING_URL}"
    )
    try:
        httpx.post(
            NTFY_URL,
            content=message.encode("utf-8"),
            headers={
                "Title": "Stortemelk tracker: nog steeds actief",
                "Priority": "low",
                "Tags": "white_check_mark,camping",
            },
            timeout=10,
        )
        log.info("Heartbeat notification sent -> %s", NTFY_URL)
    except Exception as e:
        log.error("Failed to send heartbeat notification: %s", e)


if __name__ == "__main__":
    if "--install-browser" in sys.argv:
        import subprocess
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    elif "--test" in sys.argv:
        asyncio.run(run_single_test())
    elif "--once" in sys.argv:
        asyncio.run(run_once())
    elif "--heartbeat" in sys.argv:
        send_heartbeat()
    else:
        asyncio.run(main())
