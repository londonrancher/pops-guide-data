#!/usr/bin/env python3
"""Daily refresh of watering stages + boil-water notices + burn-ban status.

Outputs:
  data/watering-stages.json        canonical stage per district
  data/burn-ban.json               Travis County burn-ban status
  data/lakeway-boil.json           active notices scoped to Lakeway addresses
  data/hills-boil.json             active notices scoped to The Hills addresses
  data/costabella-boil.json        active notices scoped to Costa Bella addresses
  data/cambridge-boil.json         Austin Water notices (City of Austin newsroom RSS) for Cambridge Tower

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
# Status language is read only from the page's status banner (the first
# .sppb-alert block, or the text right after the "You are here" breadcrumb),
# never from FAQ copy further down the page ("A burn ban is in effect or lifted").
BURN_BAN_ACTIVE = re.compile(
    r"(burn\s+ban\s+(is\s+)?(currently\s+)?in\s+effect|outdoor\s+burning\s+is\s+(currently\s+)?prohibited|active\s+burn\s+ban)",
    re.IGNORECASE,
)
BURN_BAN_INACTIVE = re.compile(
    r"(no\s+(current\s+|active\s+)?burn\s+ban|burn\s+ban\s+(has\s+been\s+|was\s+|is\s+)?(lifted|rescinded|expired)|burn\s+ban\s+is\s+not\s+in\s+effect|outdoor\s+burning\s+is\s+(currently\s+)?(allowed|permitted))",
    re.IGNORECASE,
)
BURN_BAN_ASOF = re.compile(r"as\s+of\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)

# City of Austin newsroom RSS: Austin Water announces boil-water notices there.
AUSTIN_NEWS_RSS = "https://www.austintexas.gov/site/news/rss.xml"
AUSTIN_BOIL = re.compile(r"boil[\s-]?water", re.IGNORECASE)
AUSTIN_BOIL_LIFTED = re.compile(r"(lift|lifted|lifts|rescind|rescinded|no\s+longer|ends|ended|has\s+ended|over)", re.IGNORECASE)
AUSTIN_LOOKBACK_DAYS = 21


def fetch(url: str) -> str | None:
    """Plain httpx GET. Returns HTML or None on any error."""
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[fetch] {url} → {e}", file=sys.stderr)
        return None


def fetch_with_fallback(url: str) -> tuple[str | None, str]:
    """Try httpx first; on 403 or empty result, fall back to headless Chromium.

    Returns (html, method) where method ∈ {"httpx", "playwright", "failed"}.
    """
    # Try plain httpx first — it's ~50x faster than spinning up a browser.
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return r.text, "httpx"
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status not in (403, 429, 503):
            print(f"[httpx] {url} → HTTP {status}", file=sys.stderr)
            return None, "failed"
        # Anti-bot status code → try Playwright fallback.
    except Exception as e:
        print(f"[httpx] {url} → {e}", file=sys.stderr)
        # Keep going to Playwright; many CDN failures masquerade as TLS resets.

    # Playwright fallback. Lazy import so other scripts that don't need it
    # don't pay the browser launch cost.
    try:
        from lib.browser import fetch_html
    except ImportError as e:
        print(f"[playwright] import failed: {e}", file=sys.stderr)
        return None, "failed"

    html = fetch_html(url)
    if html:
        return html, "playwright"
    print(f"[playwright] {url} returned no HTML", file=sys.stderr)
    return None, "failed"


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
        "fetchMethod": None,          # "httpx" or "playwright"
    }

    html, method = fetch_with_fallback(d["stagePath"])
    state["fetchMethod"] = method
    if html is None:
        state["fetched"] = False
        return state

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    # Stage detection: try clean text first (catches "Stage N" in visible
    # page copy), then fall back to raw HTML (catches "Stage N Water
    # Restrictions" embedded in <script> JSON, which is how Crossroads
    # publishes its TCMUD stage info — the PDF filenames carry the stage).
    m = STAGE_PATTERNS.search(text)
    if not m:
        m = STAGE_PATTERNS.search(html)
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
    """Return Travis County burn-ban state, read from the page's status banner only."""
    state = {
        "url": BURN_BAN_URL,
        "active": None,             # True / False / None (unknown)
        "fetched": True,
        "asOf": None,               # date the county states, e.g. "8/11/26"
        "statusText": None,         # the banner text the decision was based on
        "evidence": None,
    }
    html = fetch(BURN_BAN_URL)
    if html is None:
        state["fetched"] = False
        return state

    soup = BeautifulSoup(html, "html.parser")
    banner = None
    alert = soup.select_one(".sppb-alert")
    if alert is not None:
        banner = alert.get_text(" ", strip=True)
    if not banner:
        text = soup.get_text(" ", strip=True)
        i = text.find("You are here")
        if i >= 0:
            banner = text[i:i + 400]
    if not banner:
        state["evidence"] = "status banner not found; status unknown"
        return state

    banner = re.sub(r"\s+", " ", banner).strip()[:400]
    state["statusText"] = banner
    inactive = bool(BURN_BAN_INACTIVE.search(banner))
    # "No Burn Ban in Effect" contains the active phrase too; an explicit "no burn ban" wins.
    active = bool(BURN_BAN_ACTIVE.search(banner)) and not re.search(r"\bno\s+(current\s+|active\s+)?burn\s+ban", banner, re.IGNORECASE)
    if active and not inactive:
        state["active"] = True
        state["evidence"] = "status banner says a burn ban is in effect"
    elif inactive and not active:
        state["active"] = False
        state["evidence"] = "status banner says no burn ban / lifted"
    else:
        state["evidence"] = "status banner ambiguous; status unknown"
    m = BURN_BAN_ASOF.search(banner)
    if m:
        state["asOf"] = m.group(1)
    return state


def check_austin_water_boil() -> dict:
    """Look for an Austin Water boil-water notice in the City of Austin newsroom RSS.

    Conservative: a notice is active only if the newest boil-water item in the
    look-back window does not announce a lift. Returns a site payload for
    Cambridge Tower (downtown Austin)."""
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timedelta, timezone

    result = {"fetched": True, "notices": [], "evidence": None}
    xml = fetch(AUSTIN_NEWS_RSS)
    if xml is None:
        result["fetched"] = False
        return result
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(xml, "html.parser")
    cutoff = datetime.now(timezone.utc) - timedelta(days=AUSTIN_LOOKBACK_DAYS)
    hits = []
    for item in soup.find_all("item"):
        title = (item.title.get_text(" ", strip=True) if item.title else "")
        desc = BeautifulSoup(item.description.get_text() if item.description else "", "html.parser").get_text(" ", strip=True)
        link = item.find("link")
        link = link.get_text(strip=True) if link else ""
        if not link and item.guid:
            link = item.guid.get_text(strip=True)
        try:
            pd = item.find("pubdate")  # html.parser lower-cases tag names
            when = parsedate_to_datetime(pd.get_text(strip=True)) if pd else None
        except Exception:
            when = None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is None or when < cutoff:
            continue  # undated or older than the look-back window
        blob = f"{title} {desc}"
        if AUSTIN_BOIL.search(blob):
            hits.append({"title": title, "desc": desc[:300], "link": link, "when": when})
    if not hits:
        result["evidence"] = f"no boil-water items in the last {AUSTIN_LOOKBACK_DAYS} days"
        return result
    hits.sort(key=lambda h: h["when"] or cutoff, reverse=True)
    newest = hits[0]
    if AUSTIN_BOIL_LIFTED.search(newest["title"]):
        result["evidence"] = f"newest boil-water item announces a lift: {newest['title']}"
        return result
    result["evidence"] = f"newest boil-water item: {newest['title']}"
    result["notices"].append({
        "district": "Austin Water",
        "districtPhone": "3-1-1",
        "districtUrl": "https://www.austintexas.gov/department/water",
        "source": newest["link"] or AUSTIN_NEWS_RSS,
        "detectedAt": now_iso_ct(),
        "description": newest["title"],
        "rawContext": newest["desc"],
    })
    return result


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

    unreachable = [d["name"] for d in district_states if site in d["sites"] and not d["fetched"]]
    return {
        "site": site,
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "districtsUnreachable": unreachable,
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
                "fetchMethod": d["fetchMethod"],
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
    # ignore_keys=[]: lastChecked is rewritten every run so the sites can tell the data is fresh.
    burn_changed = write_json_atomic(DATA_DIR / "burn-ban.json", burn_payload, ignore_keys=[])

    # 5. Write per-site boil JSONs
    boil_changed = {}
    for site in ("lakeway", "hills", "costabella"):
        payload = build_site_boil_payload(site, district_states)
        boil_changed[site] = write_json_atomic(DATA_DIR / f"{site}-boil.json", payload, ignore_keys=[])

    # 5b. Cambridge Tower (downtown Austin): Austin Water notices from the city newsroom
    aw = check_austin_water_boil()
    cam_payload = {
        "site": "cambridge",
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "source": AUSTIN_NEWS_RSS,
        "fetched": aw["fetched"],
        "evidence": aw["evidence"],
        "districtsUnreachable": [] if aw["fetched"] else ["Austin Water (city newsroom)"],
        "notices": aw["notices"],
    }
    boil_changed["cambridge"] = write_json_atomic(DATA_DIR / "cambridge-boil.json", cam_payload, ignore_keys=[])

    # 6. Log summary
    n_unreachable = sum(1 for d in district_states if not d["fetched"])
    n_with_boil = sum(1 for d in district_states if d["boilNotice"])
    stages_summary = ", ".join(
        f'{d["key"]}={d["stage"]}' for d in district_states
    )
    print(
        f"[{stamp_log_ct()}] water_check: "
        f"stages: {stages_summary} | boil: {n_with_boil} district(s) | "
        f"burn_ban: {burn['active']} (as of {burn['asOf']}) | austin_water_boil: {len(aw['notices'])} | unreachable: {n_unreachable} | "
        f"changes: stages={stages_changed}, burn={burn_changed}, "
        f"boil={ {s: ('y' if c else 'n') for s, c in boil_changed.items()} }"
    )

    return 0 if n_unreachable == 0 else 1  # nonzero on partial-fetch days


if __name__ == "__main__":
    sys.exit(main())
