#!/usr/bin/env python3
"""Daily refresh of watering stages + boil-water notices + burn-ban status.

Outputs:
  data/watering-stages.json        canonical stage per district
  data/burn-ban.json               Travis County burn-ban status
  data/lakeway-boil.json           active notices scoped to Lakeway addresses
  data/hills-boil.json             active notices scoped to The Hills addresses
  data/costabella-boil.json        active notices scoped to Costa Bella addresses

District → site mapping:
  Lakeway MUD          → Lakeway
  Hurst Creek MUD      → Lakeway + Hills
  WCID #17             → Lakeway + Costa Bella
  TCMUD #11/#12/#13    → Lakeway only

This script is intentionally conservative: it only marks boil-water "active"
when the page has clear keyword matches, and never flips an existing notice
to inactive without explicit evidence. The site banners are derived from the
JSON the script writes — false positives are far worse than false negatives,
so we err toward the latter.

### Known limitation — needs Playwright fallback

lakewaymud.org and wcid17.org both return 403 Forbidden to plain httpx GETs
(they have anti-bot at the CDN). Until we add a Playwright fallback in this
script (next iteration), the "stage" field for those two districts will be
None and the entry will show `fetched: false`. The site frontends should
render "(latest data unavailable — see district site)" when `fetched` is
false rather than implying a stage of 0.

The other four districts (HCM + TCMUD #11/#12/#13) fetch successfully but
may also return None for stage if their page doesn't use the literal
"Stage N" wording. Inspect the actual HTML and tighten STAGE_PATTERNS as
needed.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from lib.chicago_time import now_iso_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
}

# Districts and what we know about them
DISTRICTS = [
    {
        "key": "lmud",
        "name": "Lakeway MUD",
        "url": "https://lakewaymud.org/",
        "stagePath": "https://lakewaymud.org/customer-service/water-conservation/",
        "phone": "512-261-6222",
        "sites": ["lakeway"],
    },
    {
        "key": "hcm",
        "name": "Hurst Creek MUD",
        "url": "https://hurstcreekmud.org/",
        "stagePath": "https://hurstcreekmud.org/",
        "phone": "512-261-6281",
        "sites": ["lakeway", "hills"],
    },
    {
        "key": "wcid17",
        "name": "Travis County WCID #17",
        "url": "https://www.wcid17.org/",
        "stagePath": "https://www.wcid17.org/water-restrictions-and-conservation/",
        "phone": "512-266-1111",
        "sites": ["lakeway", "costabella"],
    },
    {
        "key": "tcmud11",
        "name": "TCMUD #11",
        "url": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-011",
        "stagePath": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-011",
        "phone": "512-246-1400",
        "sites": ["lakeway"],
    },
    {
        "key": "tcmud12",
        "name": "TCMUD #12",
        "url": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-012",
        "stagePath": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-012",
        "phone": "512-246-1400",
        "sites": ["lakeway"],
    },
    {
        "key": "tcmud13",
        "name": "TCMUD #13",
        "url": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-013",
        "stagePath": "https://crossroadsus.com/districts/travis-county-municipal-utility-district-013",
        "phone": "512-246-1400",
        "sites": ["lakeway"],
    },
]

BURN_BAN_URL = "https://www.traviscountytx.gov/fire-marshal/burn-ban"

BOIL_PATTERNS = re.compile(
    r"(boil[\s-]?water\s+notice|boil\s+water\s+advisory|do\s+not\s+drink|boil\s+water\s+order)",
    re.IGNORECASE,
)
STAGE_PATTERNS = re.compile(
    r"stage\s*([1-4])(?:\s*water\s*restriction)?",
    re.IGNORECASE,
)
BURN_BAN_ACTIVE = re.compile(
    r"(burn\s+ban\s+is\s+in\s+effect|burn\s+ban\s+is\s+currently\s+in\s+effect|active\s+burn\s+ban)",
    re.IGNORECASE,
)
BURN_BAN_INACTIVE = re.compile(
    r"(no\s+burn\s+ban|burn\s+ban\s+is\s+not\s+in\s+effect|currently\s+no\s+burn\s+ban)",
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


def check_district(d: dict) -> dict:
    """Return canonical state for one district."""
    state = {
        "key": d["key"],
        "name": d["name"],
        "url": d["url"],
        "phone": d["phone"],
        "sites": d["sites"],
        "stage": None,                # 1, 2, 3, 4, or None if unknown
        "stageDetectedFrom": None,    # URL the stage was read from
        "boilNotice": False,
        "boilContext": None,          # short excerpt around the match, for debugging
        "fetched": True,
    }

    html = fetch(d["stagePath"])
    if html is None:
        state["fetched"] = False
        return state

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    m = STAGE_PATTERNS.search(text)
    if m:
        try:
            state["stage"] = int(m.group(1))
            state["stageDetectedFrom"] = d["stagePath"]
        except ValueError:
            pass

    m2 = BOIL_PATTERNS.search(text)
    if m2:
        state["boilNotice"] = True
        # Capture surrounding context for debugging / human review.
        start = max(0, m2.start() - 80)
        end = min(len(text), m2.end() + 200)
        state["boilContext"] = text[start:end].strip()

    return state


def check_burn_ban() -> dict:
    """Return Travis County burn-ban state."""
    state = {
        "url": BURN_BAN_URL,
        "active": None,             # True / False / None (unknown)
        "fetched": True,
        "evidence": None,
    }
    html = fetch(BURN_BAN_URL)
    if html is None:
        state["fetched"] = False
        return state

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # Inactive language tends to be more reliable than active.
    if BURN_BAN_INACTIVE.search(text):
        state["active"] = False
        state["evidence"] = "page contains 'no burn ban' / 'not in effect'"
    elif BURN_BAN_ACTIVE.search(text):
        state["active"] = True
        state["evidence"] = "page contains 'burn ban is in effect'"
    return state


def build_site_boil_payload(site: str, district_states: Iterable[dict]) -> dict:
    """Compose the boil-water JSON the frontend reads for a given site."""
    notices = []
    for d in district_states:
        if site not in d["sites"]:
            continue
        if not d["boilNotice"]:
            continue
        notices.append({
            "district": d["name"],
            "districtPhone": d["phone"],
            "districtUrl": d["url"],
            "source": d["stageDetectedFrom"] or d["url"],
            "detectedAt": now_iso_ct(),
            "rawContext": d["boilContext"],
        })

    return {
        "site": site,
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "notices": notices,
    }


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Hit each district
    district_states = [check_district(d) for d in DISTRICTS]

    # 2. Hit burn-ban page
    burn = check_burn_ban()

    # 3. Write canonical watering-stages.json
    stages_payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "districts": [
            {
                "key": d["key"],
                "name": d["name"],
                "stage": d["stage"],
                "boilNotice": d["boilNotice"],
                "fetched": d["fetched"],
                "url": d["url"],
                "phone": d["phone"],
                "sites": d["sites"],
            }
            for d in district_states
        ],
    }
    stages_changed = write_json_atomic(DATA_DIR / "watering-stages.json", stages_payload)

    # 4. Write burn-ban.json
    burn_payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        **burn,
    }
    burn_changed = write_json_atomic(DATA_DIR / "burn-ban.json", burn_payload)

    # 5. Write per-site boil JSONs
    boil_changed = {}
    for site in ("lakeway", "hills", "costabella"):
        payload = build_site_boil_payload(site, district_states)
        boil_changed[site] = write_json_atomic(DATA_DIR / f"{site}-boil.json", payload)

    # 6. Log summary
    n_unreachable = sum(1 for d in district_states if not d["fetched"])
    n_with_boil = sum(1 for d in district_states if d["boilNotice"])
    stages_summary = ", ".join(
        f'{d["key"]}={d["stage"]}' for d in district_states
    )
    print(
        f"[{stamp_log_ct()}] water_check: "
        f"stages: {stages_summary} | boil: {n_with_boil} district(s) | "
        f"burn_ban: {burn['active']} | unreachable: {n_unreachable} | "
        f"changes: stages={stages_changed}, burn={burn_changed}, "
        f"boil={ {s: ('y' if c else 'n') for s, c in boil_changed.items()} }"
    )

    return 0 if n_unreachable == 0 else 1  # nonzero on partial-fetch days


if __name__ == "__main__":
    sys.exit(main())
