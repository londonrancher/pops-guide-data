"""CivicWeb (iCompass) helpers using Playwright.

iCompass blocks plain HTTP fetches with anti-bot interstitials. Real headless
Chromium driven via Playwright bypasses that. These helpers are used by the
meetings-daily and meetings-watch workflows; not loaded by other scripts.
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def civicweb_browser() -> Iterator:
    """Yields a Playwright page with a desktop UA and JS enabled.

    Usage:
        with civicweb_browser() as page:
            page.goto("https://lakeway-tx.civicweb.net/Portal/MeetingSchedule.aspx",
                      wait_until="networkidle", timeout=30000)
            html = page.content()
    """
    from playwright.sync_api import sync_playwright  # imported lazily

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


def meeting_url(meeting_id: int | str) -> str:
    return f"https://lakeway-tx.civicweb.net/Portal/MeetingInformation.aspx?Org=Cal&Id={meeting_id}"


def schedule_url() -> str:
    return "https://lakeway-tx.civicweb.net/Portal/MeetingSchedule.aspx"
