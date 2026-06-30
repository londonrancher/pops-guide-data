"""Generic headless-browser helper for sites with anti-bot protection.

Pages like `lakewaymud.org`, `wcid17.org`, and CivicWeb's iCompass portal
return 403 to plain HTTP fetches. A real headless Chromium driven via
Playwright passes their checks. Use this for any site where `httpx` fails.

Usage:
    from lib.browser import headless_page
    with headless_page() as page:
        page.goto("https://lakewaymud.org/...", wait_until="networkidle",
                  timeout=30000)
        html = page.content()
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def headless_page(
    user_agent: str | None = None,
    locale: str = "en-US",
    viewport_w: int = 1280,
    viewport_h: int = 900,
) -> Iterator:
    """Yields a Playwright page object inside a fully isolated browser context.

    The context closes (and the browser shuts down) when the `with` block
    exits, so callers can't leak processes.

    Hardened against modern anti-bot fingerprinting (Cloudflare, Akamai,
    CivicWeb's bot wall added ~2026-06-29). Key mitigations:
      • Current Chrome UA (older UAs are an immediate flag).
      • `--disable-blink-features=AutomationControlled` removes the
        `navigator.webdriver = true` signal at the launch level.
      • Init script overrides several JS properties that headless Chrome
        leaks (webdriver, languages, plugins, window.chrome).
      • Real Accept-Language header.
    Update the UA every few months to stay ahead of "outdated UA" blocks.
    """
    from playwright.sync_api import sync_playwright  # lazy import

    ua = user_agent or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": viewport_w, "height": viewport_h},
            locale=locale,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Sec-Ch-Ua": (
                    '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
                ),
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
            },
        )
        # Hide common headless-browser fingerprints BEFORE any page loads.
        ctx.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', {
              get: () => [{ name: 'Chrome PDF Plugin' }, { name: 'Native Client' }]
            });
            window.chrome = window.chrome || { runtime: {} };
            const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (originalQuery) {
              window.navigator.permissions.query = (params) =>
                params && params.name === 'notifications'
                  ? Promise.resolve({ state: Notification.permission })
                  : originalQuery(params);
            }
            """
        )
        page = ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


def fetch_html(url: str, timeout_ms: int = 30000) -> str | None:
    """One-shot helper: open a page, return its HTML, close everything.

    Returns the page's `content()` HTML, or None on any error. Use this for
    sites that don't need post-load interaction.
    """
    try:
        with headless_page() as page:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Give late-loaded JS a moment in case the anti-bot is JS-driven.
            page.wait_for_timeout(1500)
            return page.content()
    except Exception:
        return None
