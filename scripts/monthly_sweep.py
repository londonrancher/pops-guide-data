#!/usr/bin/env python3
"""Monthly catch-all sweep of low-frequency content checks.

Runs on the 1st of each month at 7 AM CT. Verifies items that change rarely
but are worth checking periodically:
  - 24-Hour Pharmacy 24/7 status (Walgreens 6200 William Cannon, CVS Guadalupe)
  - 24-Hour Vet (Violet Crown) 24/7 status
  - GFiber buildout status — is service live in Lakeway yet?
  - LCRA OSSF (septic) program existence
  - LTFR permits site existence
  - Voyent Alert! / Warn Central Texas program existence
  - LTHS Band flag fundraiser URL existence

Writes data/monthly-status.json with one block per check. Items have:
  status: "ok" | "stale" | "unreachable"
  evidence: short excerpt that triggered the status (or null)
  lastChecked: ISO timestamp

This is the lightest of the workflows — pure httpx, no scraping logic
needed, just URL liveness + small text searches.
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

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "monthly-status.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}


def fetch_text(url: str) -> tuple[str | None, int | None]:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=15.0, follow_redirects=True)
        body = ""
        try:
            body = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        except Exception:
            body = r.text
        return body, r.status_code
    except Exception as e:
        print(f"[fetch] {url} → {e}", file=sys.stderr)
        return None, None


def check_url_alive(url: str) -> dict:
    """Just verify the URL responds OK. Don't trust 4xx as broken; many sites HEAD-block."""
    body, status = fetch_text(url)
    if status is None:
        return {"status": "unreachable", "httpStatus": None, "evidence": None}
    if status >= 400:
        return {"status": "stale", "httpStatus": status, "evidence": f"HTTP {status}"}
    return {"status": "ok", "httpStatus": status, "evidence": None}


def check_text_contains(url: str, expected: str) -> dict:
    body, status = fetch_text(url)
    if status is None:
        return {"status": "unreachable", "httpStatus": None, "evidence": None}
    if status >= 400:
        return {"status": "stale", "httpStatus": status, "evidence": f"HTTP {status}"}
    if expected.lower() in (body or "").lower():
        # Capture small excerpt around the match
        idx = body.lower().find(expected.lower())
        excerpt = body[max(0, idx - 60): idx + len(expected) + 60].strip()
        return {"status": "ok", "httpStatus": status, "evidence": re.sub(r"\s+", " ", excerpt)[:200]}
    return {"status": "stale", "httpStatus": status, "evidence": f"'{expected}' not found on page"}


def main() -> int:
    checks: dict = {}

    # 24-Hour Pharmacies — pure URL aliveness (Walgreens/CVS sites won't say "24-hour"
    # plainly, and individual store pages are JS-driven for hours, so just verify
    # the brand site is reachable).
    checks["walgreens24"] = {
        "label": "Walgreens 24-Hour Pharmacy (6200 William Cannon, Austin)",
        "phone": "+15128921933",
        **check_url_alive("https://www.walgreens.com/locator/store/details?lat=30.180&lng=-97.870"),
    }
    checks["cvs24"] = {
        "label": "CVS 24-Hour Pharmacy (2402 Guadalupe St, Austin)",
        "phone": "+15124742323",
        **check_url_alive("https://www.cvs.com/store-locator/austin-tx-pharmacies/2402-guadalupe-st"),
    }

    # 24-Hour Vet
    checks["violetCrownVet"] = {
        "label": "Violet Crown Veterinary Specialists 24/7",
        "phone": "+15122842877",
        **check_text_contains("https://www.violetcrownvet.com/", "24"),
    }

    # GFiber buildout — change in language is the signal
    checks["gfiberAustin"] = {
        "label": "GFiber Austin buildout",
        "currentStatusInGuide": "building since Jan 2026",
        **check_url_alive("https://fiber.google.com/cities/austin/"),
    }

    # LCRA OSSF program
    checks["lcraOSSF"] = {
        "label": "LCRA OSSF (septic) program",
        **check_url_alive("https://www.lcra.org/water/permits-contracts/on-site-sewage/"),
    }

    # LTFR permits portal
    checks["ltfrPermits"] = {
        "label": "LTFR online permits portal",
        **check_url_alive("https://www.ltfrpermits.com"),
    }

    # Voyent Alert! signup
    checks["voyentAlert"] = {
        "label": "Voyent Alert! community signup",
        **check_url_alive("https://voyent-alert.com/us/community/"),
    }

    # Warn Central Texas
    checks["warnCentralTexas"] = {
        "label": "Warn Central Texas / CodeRed",
        **check_url_alive("https://www.warncentraltexas.org/"),
    }

    # LTHS Band flag fundraiser
    checks["lthsBandFlagFundraiser"] = {
        "label": "LTHS Band American Flag fundraiser",
        **check_url_alive("https://www.laketravisband.com/usflagdistribution"),
    }

    # NextRequest portal (records requests)
    checks["nextRequest"] = {
        "label": "Lakeway public-records portal (NextRequest)",
        **check_url_alive("https://cityoflakewaytx.nextrequest.com/"),
    }

    payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "checks": checks,
    }

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"

    ok_n = sum(1 for c in checks.values() if c["status"] == "ok")
    stale_n = sum(1 for c in checks.values() if c["status"] == "stale")
    unr_n = sum(1 for c in checks.values() if c["status"] == "unreachable")

    print(
        f"[{stamp_log_ct()}] monthly_sweep: {len(checks)} checks | "
        f"ok={ok_n} stale={stale_n} unreachable={unr_n} | {note}"
    )
    for key, c in checks.items():
        if c["status"] != "ok":
            print(f"  • {c['status'].upper():12}  {key:30}  {c.get('evidence') or ''}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
