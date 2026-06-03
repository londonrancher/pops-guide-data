#!/usr/bin/env python3
"""Weekly external-URL health check across all three Pops' Guide sites.

Fetches each site's index.html from its live origin, extracts every
external URL, and does a HEAD (with GET fallback) on each one. Writes
data/link-health.json with a categorized summary plus the full result
list. The front-end could surface a "stale links detected" badge to
the maintainer; the JSON is also useful as a manual punch-list.

Doesn't try to dynamically follow JS-loaded URLs or interact with the
site — purely static external-link health.
"""
from __future__ import annotations
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from lib.chicago_time import now_iso_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "link-health.json"

SITES = {
    "lakeway": "https://lakewayguide.com/",
    "hills": "https://thehillsguide.com/",
    "costabella": "https://costabellaguide.com/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}

URL_PATTERN = re.compile(
    r'(?<![\w])(https?://[a-zA-Z0-9][a-zA-Z0-9._\-]+(?::\d+)?(?:/[^\s"\'<>)]*)?)',
)

# Skip these — they're known false-positives (anti-bot blocks HEAD, return
# 4xx unconditionally, or are JS/network resources rather than user links).
SKIP_DOMAIN_SUFFIXES = (
    # Member portals — always block HEAD
    "buurt.ccmcnet.com",
    "members.invitedclubs.com",
    "engage.goenumerate.com",
    "portal.camanagers.com",
    "lakewaytx-energovweb.tylerhost.net",
    # iCompass anti-bot blocks HEAD
    "lakeway-tx.civicweb.net",
    # MUD / utility sites with CDN that blocks HEAD/non-browser UAs
    "lakewaymud.org",
    "hurstcreekmud.org",
    "wcid17.org",
    "ltfrpermits.com",
    "pec.coop",
    "spectrum.com",
    "map.mypec.com",
    # Facebook never returns 200 to HEAD even for live pages
    "facebook.com",
    "m.facebook.com",
    # Realtor.com / Zillow 429/403 to HEAD
    "realtor.com",
    "zillow.com",
    # Outage maps 401 to anonymous HEAD
    "outagemap.austinenergy.com",
    "outagemap.austinenergy",
    # Aerial / WebTrac portal: 403s on HEAD
    "txlakewayweb.myvscloud.com",
    # Shortener doesn't respond to HEAD
    "qrco.de",
    # Network resources, not user-facing links
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "googletagmanager.com",
    "google-analytics.com",
    "cloudflareinsights.com",
    "static.cloudflareinsights.com",
)


def should_skip(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in SKIP_DOMAIN_SUFFIXES)


def fetch_site_html(url: str) -> str | None:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[fetch_site_html] {url} → {e}", file=sys.stderr)
        return None


def extract_external_urls(html: str, origin_host: str) -> set[str]:
    out = set()
    for m in URL_PATTERN.finditer(html or ""):
        url = m.group(1).rstrip(".,;:!?)]\"'>")
        host = (urlparse(url).hostname or "").lower()
        if not host:
            continue
        if host == origin_host or host.endswith("." + origin_host):
            continue
        out.add(url)
    return out


def check_url(url: str) -> dict:
    """HEAD first, fall back to GET on 405/403. Return outcome dict."""
    result = {"url": url, "ok": False, "status": None, "method": None, "error": None}
    try:
        r = httpx.head(url, headers=HEADERS, timeout=15.0, follow_redirects=True)
        result["status"] = r.status_code
        result["method"] = "HEAD"
        if r.status_code < 400:
            result["ok"] = True
            return result
        if r.status_code in (405, 403, 429):
            # Some servers refuse HEAD; try GET.
            r2 = httpx.get(url, headers=HEADERS, timeout=20.0, follow_redirects=True)
            result["status"] = r2.status_code
            result["method"] = "GET"
            result["ok"] = r2.status_code < 400
            return result
        return result
    except httpx.HTTPError as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def main() -> int:
    all_urls: dict[str, set[str]] = defaultdict(set)
    unreachable_sites: list[str] = []

    for site_key, site_url in SITES.items():
        host = (urlparse(site_url).hostname or "").lower()
        html = fetch_site_html(site_url)
        if not html:
            unreachable_sites.append(site_key)
            continue
        urls = extract_external_urls(html, host)
        for u in urls:
            all_urls[u].add(site_key)

    # Filter out skip-listed domains
    to_check = {u: sites for u, sites in all_urls.items() if not should_skip(u)}
    skipped = {u: sites for u, sites in all_urls.items() if should_skip(u)}

    print(f"[{stamp_log_ct()}] link_validator: {len(to_check)} URLs to probe, {len(skipped)} skipped, {len(unreachable_sites)} sites unreachable")

    results = []
    # Parallel HEAD/GET — kindly to remote servers (cap concurrency).
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(check_url, url): url for url in to_check}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"url": url, "ok": False, "status": None, "error": str(e)}
            r["sites"] = sorted(to_check[url])
            results.append(r)

    # Sort: broken first, then by URL
    results.sort(key=lambda r: (r["ok"], r["url"]))

    broken = [r for r in results if not r["ok"]]
    payload = {
        "lastChecked": now_iso_ct(),
        "lastCheckedUtc": utc_iso(),
        "sites": SITES,
        "unreachableSites": unreachable_sites,
        "totalUrls": len(to_check) + len(skipped),
        "probed": len(to_check),
        "skipped": len(skipped),
        "brokenCount": len(broken),
        "broken": [
            {
                "url": r["url"],
                "status": r.get("status"),
                "method": r.get("method"),
                "error": r.get("error"),
                "sites": r["sites"],
            }
            for r in broken
        ],
        "skippedUrls": [{"url": u, "sites": sorted(s)} for u, s in skipped.items()],
    }

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    print(
        f"[{stamp_log_ct()}] link_validator: probed {len(to_check)}, broken {len(broken)} | {note}"
    )
    if broken:
        for b in broken[:20]:
            print(f"  ✗ {b.get('status') or 'ERR'}  {b['url']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
