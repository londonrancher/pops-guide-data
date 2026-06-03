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


def discover_meeting_ids(page) -> list[dict]:
    """From the schedule page, find every upcoming meeting and return basic info.

    The schedule page renders each meeting as a card with a title and date,
    linking to MeetingInformation.aspx?Org=Cal&Id=<n>. We extract the link
    text, dates, and ids; later we fetch each individual page for full detail.
    """
    page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
    # iCompass renders the list client-side; allow a tick for hydration.
    page.wait_for_timeout(2500)

    raw = page.evaluate(
        """() => {
          const items = [];
          // Each meeting on the schedule page has an anchor to MeetingInformation.aspx
          const anchors = document.querySelectorAll('a[href*="MeetingInformation.aspx"]');
          anchors.forEach(a => {
            const href = a.href;
            const m = href.match(/Id=(\\d+)/);
            if (!m) return;
            // Walk up a few ancestors looking for a date string in the surrounding card
            let card = a.closest('li, article, .card, .meeting, div');
            const cardText = card ? card.innerText : a.innerText;
            items.push({
              id: parseInt(m[1], 10),
              title: a.textContent.trim(),
              href,
              cardText,
            });
          });
          // De-dup by id (each meeting tends to render in multiple lists)
          const seen = new Set();
          return items.filter(it => {
            if (seen.has(it.id)) return false;
            seen.add(it.id);
            return !!it.title;
          });
        }"""
    )
    return raw


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

    try:
        with headless_page() as page:
            discovered = discover_meeting_ids(page)
            print(f"[{stamp_log_ct()}] meetings_daily: discovered {len(discovered)} meeting entries on schedule page")

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

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    types = ",".join(sorted({m["type"] for m in meetings})) or "(none)"
    print(
        f"[{stamp_log_ct()}] meetings_daily: {len(meetings)} meeting(s) in window | "
        f"types: {types} | {note}"
        + (f" | error: {error}" if error else "")
    )
    return 0 if not error else 2


if __name__ == "__main__":
    sys.exit(main())
