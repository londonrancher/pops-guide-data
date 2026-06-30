#!/usr/bin/env python3
"""Daily refresh of the Lakeway public-meeting list.

Scrapes the City of Lakeway CivicWeb (iCompass) portal for every upcoming
public meeting in the next 90 days (Council, ZAPCO, BOA, Board of Ethics,
and any other body), and writes data/lakeway-meetings.json in the shape the
existing front-end expects.

Output matches the prior Mac-task format:
  {
    "lastUpdated": "YYYY-MM-DD",
    "source": "...",
    "publicCommentPortal": "...",
    "watchLiveOrArchive": "...",
    "meetings": [
      {
        "id": 2281,
        "type": "council|zapco|boa|ethics|other",
        "title": "ZAPCO Regular Meeting",
        "date": "2026-06-03",
        "weekday": "Wednesday",
        "time": "9:00 AM",
        "location": "Lakeway City Hall, 1102 Lohmans Crossing Rd",
        "url": "https://lakeway-tx.civicweb.net/Portal/MeetingInformation.aspx?Org=Cal&Id=2281",
        "agendaUrl": "https://lakeway-tx.civicweb.net/document/.../?printPdf=true",
        "packetUrl": "https://lakeway-tx.civicweb.net/document/...",
        "livestreamed": true,
        "status": "scheduled"
      },
      ...
    ]
  }

CivicWeb is JS-rendered + has anti-bot, so we use the Playwright helper.
"""
from __future__ import annotations
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.browser import headless_page
from lib.chicago_time import now_iso_ct, now_ct, stamp_log_ct
from lib.commit_if_changed import write_json_atomic

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "lakeway-meetings.json"

SCHEDULE_URL = "https://lakeway-tx.civicweb.net/Portal/MeetingSchedule.aspx"
MEETING_URL_FMT = "https://lakeway-tx.civicweb.net/Portal/MeetingInformation.aspx?Org=Cal&Id={id}"
PUBLIC_COMMENT_PORTAL = "https://lakeway-tx.civicweb.net/Portal/CitizenEngagement.aspx"
WATCH_LIVE_OR_ARCHIVE = "https://www.lakeway-tx.gov/1062/Videos--Meetings-Events"
LOCATION = "Lakeway City Hall, 1102 Lohmans Crossing Rd"

LOOK_AHEAD_DAYS = 90

# Type classification — applied to the meeting title. Order matters: more
# specific matches first.
TYPE_PATTERNS = [
    (re.compile(r"zapco", re.I), "zapco"),
    (re.compile(r"board\s+of\s+adjustment|^boa\b|\bboa\s+(meeting|hearing)", re.I), "boa"),
    (re.compile(r"board\s+of\s+ethics|ethics\s+(meeting|commission|board)", re.I), "ethics"),
    (re.compile(r"city\s+council|council\s+meeting", re.I), "council"),
]


def classify(title: str) -> str:
    for pat, key in TYPE_PATTERNS:
        if pat.search(title):
            return key
    return "other"


# council/zapco/boa typically livestream; ethics/other usually don't.
DEFAULT_LIVESTREAMED = {
    "council": True,
    "zapco": True,
    "boa": True,
    "ethics": False,
    "other": False,
}


def parse_civicweb_date(raw: str) -> tuple[str, str] | None:
    """Parse a CivicWeb-style date string like 'Jun 03 2026' or '06/03/2026'.

    Returns (iso_date 'YYYY-MM-DD', weekday name) or None on failure.
    """
    if not raw:
        return None
    fmts = ["%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%A")
        except ValueError:
            continue
    return None


def _dump_page_diag(page, label: str) -> None:
    """Print page state to stderr for CI debugging.

    Called when the schedule page sanity check fails, so we can see in the
    GitHub Actions log what the headless browser actually rendered (vs.
    what a real browser sees). Also saves a screenshot to repo root so it
    appears in the post-job workspace if the workflow uploads it.
    """
    try:
        url = page.url
    except Exception:
        url = "<unknown>"
    try:
        title = page.title()
    except Exception:
        title = "<unknown>"
    try:
        body = page.evaluate("() => (document.body && document.body.innerText) || ''") or ""
    except Exception as e:
        body = f"<eval error: {e}>"
    try:
        anchor_count = page.evaluate(
            "() => document.querySelectorAll('a[href*=\"MeetingInformation.aspx\"]').length"
        )
    except Exception:
        anchor_count = -1
    try:
        html_len = page.evaluate("() => (document.documentElement && document.documentElement.outerHTML || '').length")
    except Exception:
        html_len = -1

    print(f"[diag:{label}] url={url}", file=sys.stderr)
    print(f"[diag:{label}] title={title!r}", file=sys.stderr)
    print(f"[diag:{label}] html_length={html_len}", file=sys.stderr)
    print(f"[diag:{label}] body_length={len(body)}", file=sys.stderr)
    print(f"[diag:{label}] meeting_anchor_count={anchor_count}", file=sys.stderr)
    # First 1500 chars of body so we can see what's actually rendered
    snippet = body[:1500].replace("\n", " ⏎ ")
    print(f"[diag:{label}] body_snippet={snippet}", file=sys.stderr)

    # Save a screenshot to the repo root so we can attach it as an artifact.
    # The workflow can pick this up via actions/upload-artifact if desired.
    try:
        screenshot_path = Path(__file__).resolve().parent.parent / f"diag-{label}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[diag:{label}] screenshot_saved={screenshot_path}", file=sys.stderr)
    except Exception as e:
        print(f"[diag:{label}] screenshot_failed={e}", file=sys.stderr)


def discover_meeting_ids(page) -> tuple[list[dict], bool]:
    """From the schedule page, find every upcoming meeting and return basic info.

    Returns (meetings, page_loaded_ok).
      - meetings: list of {id, title, href, cardText}
      - page_loaded_ok: True if the schedule page rendered enough content
        to be considered a successful fetch (even if 0 meetings).

    The schedule page renders each meeting as a card with a title and date,
    linking to MeetingInformation.aspx?Org=Cal&Id=<n>. Distinguish between
    three states so the caller can react correctly:
      A) Page loaded + ≥1 meeting → write fresh data
      B) Page loaded + 0 meetings → legitimately quiet; write empty list
      C) Page didn't load (anti-bot, timeout) → preserve existing data
    """
    page_loaded_ok = False
    try:
        response = page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
        if response is not None:
            print(
                f"[discover_meeting_ids] goto OK: status={response.status} "
                f"url={response.url}",
                file=sys.stderr,
            )
        else:
            print("[discover_meeting_ids] goto OK but no response object", file=sys.stderr)
    except Exception as e:
        print(f"[discover_meeting_ids] goto failed: {e}", file=sys.stderr)
        _dump_page_diag(page, label="goto-failed")
        return [], False

    # Wait for the network to settle. iCompass loads its meeting list
    # asynchronously; the dom content event fires well before then.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # Some sites never reach networkidle; fall through to polling.

    # Try to wait explicitly for at least one Id-bearing meeting anchor.
    # If this times out, the page may legitimately have no meetings — we'll
    # verify below by checking the body markers.
    try:
        page.wait_for_function(
            """() => {
              const a = document.querySelectorAll('a[href*="MeetingInformation.aspx"]');
              return Array.from(a).some(el => /Id=\\d+/.test(el.href));
            }""",
            timeout=15000,
        )
    except Exception:
        pass  # No meeting anchors after 15s; could be empty schedule.

    # Page-load sanity check — confirms the schedule page rendered.
    try:
        page_loaded_ok = page.evaluate(
            """() => {
              const t = document.body.innerText || '';
              return /Schedule of Meetings/i.test(t)
                  || /Upcoming Meetings/i.test(t)
                  || /Today.?s Meetings/i.test(t)
                  || /Recent Meetings/i.test(t)
                  || /Calendar/i.test(t);
            }"""
        )
    except Exception as e:
        print(f"[discover_meeting_ids] body check failed: {e}", file=sys.stderr)
        page_loaded_ok = False

    # If the sanity check failed, dump the page state so we can see what
    # the runner is actually getting (CI logs preserve this).
    if not page_loaded_ok:
        _dump_page_diag(page, label="body-check-failed")

    # Now harvest meetings. One last small wait covers any final hydration.
    page.wait_for_timeout(1000)
    raw = []
    last_count = -1
    for _ in range(5):
        raw = page.evaluate(
            """() => {
              const items = [];
              const anchors = document.querySelectorAll('a[href*="MeetingInformation.aspx"]');
              anchors.forEach(a => {
                const href = a.href;
                const m = href.match(/Id=(\\d+)/);
                if (!m) return;
                let card = a.closest('li, article, .card, .meeting, div');
                const cardText = card ? card.innerText : a.innerText;
                items.push({
                  id: parseInt(m[1], 10),
                  title: a.textContent.trim(),
                  href,
                  cardText,
                });
              });
              const seen = new Set();
              return items.filter(it => {
                if (seen.has(it.id)) return false;
                seen.add(it.id);
                return !!it.title;
              });
            }"""
        )
        if raw and len(raw) == last_count:
            break  # stable
        last_count = len(raw)
        page.wait_for_timeout(1500)

    return raw, page_loaded_ok


def parse_card_date(card_text: str) -> tuple[str, str] | None:
    """Find a date string inside a meeting card's text."""
    # Common iCompass formats: "Jun 03 2026", "JUNE 03 2026", "06/03/2026"
    patterns = [
        r"([A-Z][a-z]{2,8})\s+(\d{1,2})\s*(\d{4})",       # Jun 03 2026
        r"([A-Z][a-z]{2,8})\s+(\d{1,2}),?\s*(\d{4})",     # June 3, 2026
        r"(\d{1,2})/(\d{1,2})/(\d{4})",                    # 06/03/2026
    ]
    for pat in patterns:
        m = re.search(pat, card_text or "", re.IGNORECASE)
        if not m:
            continue
        if pat.startswith(r"(\d"):
            mm, dd, yyyy = m.groups()
            raw = f"{mm}/{dd}/{yyyy}"
        else:
            mo, dd, yyyy = m.groups()
            raw = f"{mo} {dd} {yyyy}"
        parsed = parse_civicweb_date(raw)
        if parsed:
            return parsed
    return None


def fetch_meeting_detail(page, meeting_id: int) -> dict:
    """Visit one meeting's page; pull time, agenda, packet links."""
    url = MEETING_URL_FMT.format(id=meeting_id)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    info = page.evaluate(
        """() => {
          const out = {};
          // Title — usually the most prominent text on the page
          const title = document.querySelector('h1, h2, .meeting-title');
          out.title = title ? title.textContent.trim() : null;
          out.bodyText = document.body.innerText;
          // Agenda + Packet anchors
          out.docs = Array.from(document.querySelectorAll('a'))
            .filter(a => /^(agenda|agenda packet)$/i.test((a.textContent || '').trim()))
            .map(a => ({ label: a.textContent.trim(), href: a.href }));
          return out;
        }"""
    )

    body = info.get("bodyText") or ""

    # Time — look for patterns like "9:00 AM" or "6:30 PM"
    time_match = re.search(
        r"\b(1[0-2]|0?[1-9])\s*:\s*([0-5]\d)\s*(AM|PM)\b",
        body,
        re.I,
    )
    meeting_time = None
    if time_match:
        hh, mm, ampm = time_match.groups()
        meeting_time = f"{int(hh)}:{mm} {ampm.upper()}"

    agenda_url = None
    packet_url = None
    for d in info.get("docs") or []:
        label = d.get("label", "").lower()
        href = d.get("href", "")
        if label == "agenda" and not agenda_url:
            # CivicWeb's agenda anchor goes to /document/<id>; add ?printPdf=true
            # only if not already present, to render the printable PDF version.
            if href and "printPdf=" not in href:
                href = href + ("&" if "?" in href else "?") + "printPdf=true"
            agenda_url = href
        elif label == "agenda packet" and not packet_url:
            packet_url = href

    cancelled = bool(re.search(r"\b(CANCELL?ED|CANCELLATION)\b", info.get("title") or "", re.I))
    rescheduled = bool(re.search(r"\bRESCHEDULED\b", info.get("title") or "", re.I))

    return {
        "title": info.get("title"),
        "time": meeting_time,
        "agendaUrl": agenda_url,
        "packetUrl": packet_url,
        "isCancelled": cancelled,
        "isRescheduled": rescheduled,
    }


def main() -> int:
    today = now_ct().date()
    cutoff = today + timedelta(days=LOOK_AHEAD_DAYS)
    print(f"[{stamp_log_ct()}] meetings_daily: window {today} → {cutoff} ({LOOK_AHEAD_DAYS} days)")

    meetings: list[dict] = []
    error = None
    discovered_count = 0
    page_loaded_ok = False

    try:
        with headless_page() as page:
            discovered, page_loaded_ok = discover_meeting_ids(page)
            discovered_count = len(discovered)
            print(
                f"[{stamp_log_ct()}] meetings_daily: discovered {discovered_count} meeting entries "
                f"on schedule page (page_loaded_ok={page_loaded_ok})"
            )

            for item in discovered:
                mid = item.get("id")
                if not mid:
                    continue
                card_date = parse_card_date(item.get("cardText", ""))
                if not card_date:
                    # Some meetings on the page lack a clear date in the card
                    # text (e.g., header rows); skip rather than guess.
                    continue
                iso_date, weekday = card_date
                try:
                    parsed_date = date.fromisoformat(iso_date)
                except ValueError:
                    continue
                # Window check — skip past meetings, skip far-future ones.
                if parsed_date < today or parsed_date > cutoff:
                    continue

                try:
                    detail = fetch_meeting_detail(page, mid)
                except Exception as e:
                    print(f"[{stamp_log_ct()}] meetings_daily: detail fetch Id={mid} failed: {e}", file=sys.stderr)
                    continue

                title = (detail.get("title") or item.get("title") or "").strip()
                if not title:
                    continue

                mtype = classify(title)
                entry: dict = {
                    "id": mid,
                    "type": mtype,
                    "title": title,
                    "date": iso_date,
                    "weekday": weekday,
                    "time": detail.get("time"),
                    "location": LOCATION,
                    "url": MEETING_URL_FMT.format(id=mid),
                    "livestreamed": DEFAULT_LIVESTREAMED.get(mtype, False),
                    "status": "scheduled",
                }
                if detail.get("agendaUrl"):
                    entry["agendaUrl"] = detail["agendaUrl"]
                if detail.get("packetUrl"):
                    entry["packetUrl"] = detail["packetUrl"]
                if detail.get("isCancelled"):
                    entry["status"] = "cancelled"
                elif detail.get("isRescheduled"):
                    entry["status"] = "rescheduled"

                meetings.append(entry)
    except Exception as e:
        error = f"playwright/civicweb failure: {e}"
        print(f"[{stamp_log_ct()}] meetings_daily: {error}", file=sys.stderr)

    # Sort by date ascending, then by time within the same day
    def sort_key(m: dict):
        return (m["date"], m.get("time") or "99:99 ZZ")

    meetings.sort(key=sort_key)

    payload = {
        "lastUpdated": now_iso_ct()[:10],   # YYYY-MM-DD
        "lastUpdatedAt": now_iso_ct(),
        "source": SCHEDULE_URL,
        "publicCommentPortal": PUBLIC_COMMENT_PORTAL,
        "watchLiveOrArchive": WATCH_LIVE_OR_ARCHIVE,
        "lookAheadDays": LOOK_AHEAD_DAYS,
        "meetings": meetings,
    }
    if error:
        payload["error"] = error

    # Three-state outcome:
    #   A) page_loaded_ok AND ≥1 meeting → write fresh data (normal path)
    #   B) page_loaded_ok AND 0 meetings → legitimately empty; write empty list, exit 0
    #   C) NOT page_loaded_ok → fetch failed; preserve existing JSON, exit 2
    if not page_loaded_ok:
        print(
            f"[{stamp_log_ct()}] meetings_daily: ABORT — schedule page did not load "
            f"(anti-bot or network error). Existing JSON preserved.",
            file=sys.stderr,
        )
        return 2

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    types = ",".join(sorted({m["type"] for m in meetings})) or "(none)"
    state = (
        "fresh data" if len(meetings) > 0
        else "page loaded successfully but no meetings in window — wrote empty list"
    )
    print(
        f"[{stamp_log_ct()}] meetings_daily: {len(meetings)} meeting(s) in window | "
        f"types: {types} | {note} | {state}"
        + (f" | error: {error}" if error else "")
    )
    return 0 if not error else 2


if __name__ == "__main__":
    sys.exit(main())
