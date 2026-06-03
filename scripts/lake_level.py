#!/usr/bin/env python3
"""Pull current Lake Travis elevation + percent-full once a day.

Writes data/lake-travis-level.json. Frontend banners on all three guides can
fetch this to show a live lake-level number ("Lake Travis: 669.85' / 82.1% full").

Primary source: Water Data for Texas — Lake Travis reservoir page. The data
is rendered server-side in plain HTML (no JS required, no anti-bot), so we
parse with BeautifulSoup.

  - Percent-full: in the page's <h2> ("Lake Travis: 82.1% full as of YYYY-MM-DD")
  - Elevation: top row of the recent-readings table
  - Conservation-pool elevation: 681.00 ft (constant; included for reference)
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from lib.chicago_time import now_iso_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "lake-travis-level.json"

WDFTX_URL = "https://www.waterdatafortexas.org/reservoirs/individual/travis"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}


def parse_wdftx(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")

    # Percent-full from "Lake Travis: 82.1% full as of 2026-06-03"
    h2 = soup.find("h2")
    percent_full = None
    percent_as_of = None
    if h2:
        m = re.search(r"([\d.]+)\s*%\s*full\s*as\s*of\s*([\d-]+)", h2.get_text(" ", strip=True), re.I)
        if m:
            percent_full = round(float(m.group(1)), 1)
            percent_as_of = m.group(2)

    # Elevation: "Today" row of the daily-readings table.
    # Header: ['', 'Date', 'Percent Full', 'Mean Water Level (ft)', ...]
    # Today:  ['Today', '2026-06-03', '82.1', '669.89', '918,001', ...]
    elevation_ft = None
    elevation_at = None
    for tbl in soup.find_all("table"):
        headers = [
            th.get_text(" ", strip=True)
            for th in tbl.find_all("th")
        ]
        if not any("Water Level" in h for h in headers):
            continue
        # Locate the column index for "Mean Water Level (ft)"
        try:
            level_col = next(i for i, h in enumerate(headers) if "Water Level" in h)
        except StopIteration:
            continue
        for row in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if not cells:
                continue
            # The "Today" row begins with the literal "Today"
            if cells[0].strip().lower() == "today":
                # Columns: Date is col 1, level is col `level_col`
                if level_col < len(cells):
                    raw_level = cells[level_col].replace(",", "")
                    try:
                        elevation_ft = float(raw_level)
                    except ValueError:
                        pass
                if len(cells) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[1]):
                    elevation_at = cells[1]
                break
        break

    if elevation_ft is None and percent_full is None:
        return None

    # Conservation pool for Lake Travis is a fixed full-pool spec (681.00 ft NAVD88+0.6).
    # We don't need to scrape it; bake it in for reference.
    conservation_pool = 681.00
    height_above_pool = None
    if elevation_ft is not None:
        height_above_pool = round(elevation_ft - conservation_pool, 2)

    return {
        "source": "waterdatafortexas.org",
        "sourceUrl": WDFTX_URL,
        "elevationFt": elevation_ft,
        "elevationAt": elevation_at,
        "percentFull": percent_full,
        "percentAsOf": percent_as_of,
        "conservationPoolFt": conservation_pool,
        "heightAboveConservationPoolFt": height_above_pool,
    }


def fetch_wdftx() -> dict | None:
    try:
        r = httpx.get(WDFTX_URL, headers=HEADERS, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return parse_wdftx(r.text)
    except Exception as e:
        print(f"[wdftx] error: {e}", file=sys.stderr)
        return None


def main() -> int:
    result = fetch_wdftx()
    if result is None:
        err = {
            "lastUpdated": now_iso_ct(),
            "lastUpdatedUtc": utc_iso(),
            "error": "Water Data for Texas page returned no parseable elevation.",
        }
        write_json_atomic(OUTPUT.parent / "lake-travis-level.error.json", err)
        print(f"[{stamp_log_ct()}] lake-level: FAILED — wrote error sentinel", file=sys.stderr)
        return 2

    payload = {
        "lastUpdated": now_iso_ct(),
        "lastUpdatedUtc": utc_iso(),
        **result,
    }
    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    elev = result.get("elevationFt")
    pct = result.get("percentFull")
    print(
        f"[{stamp_log_ct()}] lake-level: "
        f"{elev if elev is not None else '?'}' "
        f"({pct if pct is not None else '?'}% full) — {note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
