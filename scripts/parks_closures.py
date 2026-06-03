#!/usr/bin/env python3
"""Weekly check for closures at LCRA and Travis County parks near Lakeway.

Both sets of parks close periodically:
  - LCRA Parks: high-water/flood events, maintenance, lake-level operations.
  - Travis County Parks: flood, fire, maintenance, capacity (Hippie Hollow).

Writes data/parks-closures.json with two top-level lists. The site
frontends can render a "🟡 X closed" badge inline with the parks list
when any closures are active.

Sources:
  lcraparks.com — homepage has an "Alerts" or "Park Updates" section
  parks.traviscountytx.gov — has a "park-status" / "closures" indicator
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

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "parks-closures.json"

LCRA_URL = "https://lcraparks.com/"
TC_URL = "https://parks.traviscountytx.gov/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}

# Parks we care about — used to filter mentions on each provider's site.
LCRA_PARKS = ["Mansfield Dam", "McKinney Roughs", "Pace Bend"]   # Pace Bend is TC but historically referenced
TC_PARKS = [
    "Pace Bend",
    "Hippie Hollow",
    "Bob Wentz",
    "Mary Quinlan",
    "Sandy Creek",
    "Tom Hughes",
    "Selma Hughes",
]

CLOSED_KEYWORDS = re.compile(
    r"\b(closed|closure|temporarily\s+closed|park\s+closed|access\s+restricted)\b",
    re.IGNORECASE,
)


def fetch(url: str) -> str | None:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[fetch] {url} → {e}", file=sys.stderr)
        return None


def extract_closures(text: str, parks: list[str]) -> list[dict]:
    """Find any park name in `parks` mentioned within ~200 chars of a closure keyword."""
    out = []
    for park in parks:
        # Search ~200 chars around the park name for closure language
        for m in re.finditer(re.escape(park), text, re.IGNORECASE):
            window = text[max(0, m.start() - 200): m.end() + 200]
            if CLOSED_KEYWORDS.search(window):
                # Trim whitespace
                snippet = re.sub(r"\s+", " ", window).strip()
                out.append({
                    "park": park,
                    "context": snippet[:500],
                })
                break  # only flag once per park
    return out


def main() -> int:
    lcra_html = fetch(LCRA_URL)
    tc_html = fetch(TC_URL)

    lcra_text = BeautifulSoup(lcra_html, "html.parser").get_text(" ", strip=True) if lcra_html else ""
    tc_text = BeautifulSoup(tc_html, "html.parser").get_text(" ", strip=True) if tc_html else ""

    lcra_closures = extract_closures(lcra_text, LCRA_PARKS) if lcra_text else []
    tc_closures = extract_closures(tc_text, TC_PARKS) if tc_text else []

    payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "sources": {
            "lcra": LCRA_URL,
            "traviscounty": TC_URL,
        },
        "sourcesReachable": {
            "lcra": bool(lcra_html),
            "traviscounty": bool(tc_html),
        },
        "lcraClosures": lcra_closures,
        "traviscountyClosures": tc_closures,
    }

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    print(
        f"[{stamp_log_ct()}] parks_closures: "
        f"lcra={len(lcra_closures)} closure(s) · "
        f"tc={len(tc_closures)} closure(s) · "
        f"sources_ok=[{bool(lcra_html)},{bool(tc_html)}] · {note}"
    )
    if lcra_closures or tc_closures:
        for c in lcra_closures + tc_closures:
            print(f"  ⚠️ {c['park']}: {c['context'][:120]}…")

    return 0 if (lcra_html or tc_html) else 2


if __name__ == "__main__":
    sys.exit(main())
