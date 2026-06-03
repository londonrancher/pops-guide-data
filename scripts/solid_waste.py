#!/usr/bin/env python3
"""Weekly extraction of Lakeway Solid Waste rates and fees.

Pulls dollar amounts and policy timing from the City of Lakeway's official
billing page. Writes data/solid-waste-rates.json — a structured snapshot
the front-end can read instead of hard-coding the numbers.

The Mac version of this task directly edited the site HTML on every
change. The cloud version publishes JSON only; the site change to
dynamically render these numbers is a separate site-side task.

Sources are both behind a CloudFlare-style anti-bot, so we use Playwright
(same as meetings-daily).
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.browser import headless_page
from lib.chicago_time import now_iso_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "solid-waste-rates.json"

# Two pages: billing has rates + fees, solid-waste-mgmt has recycle-cart pricing.
PAYING_BILL_URL = "https://www.lakeway-tx.gov/207/Paying-Your-Bill"
SOLID_WASTE_URL = "https://www.lakeway-tx.gov/70/Solid-Waste-Management"


def fetch_text(url: str, timeout_ms: int = 30000) -> str | None:
    """Fetch a page via Playwright. Returns the body text or None on failure."""
    try:
        with headless_page() as page:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            body = page.evaluate("() => document.body.innerText || ''")
            return body
    except Exception as e:
        print(f"[fetch_text] {url} → {e}", file=sys.stderr)
        return None


# Extraction patterns — each tries multiple wordings the City has used.
def find_dollar_near(text: str, *keywords: str) -> float | None:
    """Find a dollar amount appearing within ~80 chars of any of the keywords."""
    for kw in keywords:
        # Forward and backward windows so "$80.55 per quarter for base service"
        # and "Base service quarterly fee is $80.55" both match.
        pattern = (
            r"(?:" + re.escape(kw) + r"[^$\n]{0,120}\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
            r"|\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)[^\n]{0,120}" + re.escape(kw) + ")"
        )
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            for grp in m.groups():
                if grp:
                    try:
                        return float(grp.replace(",", ""))
                    except ValueError:
                        continue
    return None


def find_day_of_month(text: str, *keywords: str) -> int | None:
    """Find a day-of-month near a keyword (e.g., '30th of the month')."""
    for kw in keywords:
        pattern = re.escape(kw) + r"[^\n]{0,120}?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+(?:the|each)\s+)?(?:billing\s+)?month"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def main() -> int:
    bill_text = fetch_text(PAYING_BILL_URL) or ""
    sw_text = fetch_text(SOLID_WASTE_URL) or ""

    if not bill_text and not sw_text:
        print(f"[{stamp_log_ct()}] solid_waste: ABORT — both sources unreachable", file=sys.stderr)
        # Preserve existing JSON rather than overwriting with empty rates.
        return 2

    # Combined text for cross-page extraction
    combined = (bill_text + "\n" + sw_text).strip()

    rates: dict = {
        "baseServiceQuarterly": find_dollar_near(combined, "base service", "1 trash cart"),
        "additionalTrashCartQuarterly": find_dollar_near(combined, "additional trash cart", "extra trash cart"),
        "additionalRecyclingCartMonthly": find_dollar_near(combined, "additional recycling cart", "extra recycling cart", "extra recycle"),
        "lateFee": find_dollar_near(combined, "late fee"),
        "reactivationFee": find_dollar_near(combined, "reactivation fee", "reactivation"),
        "reactivationDeposit": find_dollar_near(combined, "deposit"),
        "lateFeeDayOfMonth": find_day_of_month(combined, "late fee", "late"),
        "deactivationDayOfMonth": find_day_of_month(combined, "deactivat", "service deactiv"),
    }

    sources_reachable = {
        "paying-bill": bool(bill_text),
        "solid-waste-mgmt": bool(sw_text),
    }

    payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "sources": {
            "payingBill": PAYING_BILL_URL,
            "solidWasteMgmt": SOLID_WASTE_URL,
        },
        "sourcesReachable": sources_reachable,
        "rates": rates,
    }

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    rates_summary = ", ".join(
        f"{k}={v}" for k, v in rates.items() if v is not None
    ) or "(none extracted)"
    print(
        f"[{stamp_log_ct()}] solid_waste: sources_ok={list(sources_reachable.values())} | "
        f"{rates_summary} | {note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
