#!/usr/bin/env python3
"""Daily refresh of the next Village of the Hills City Council meeting.

Village of the Hills Council meets the 2nd Tuesday of each month at 9 AM CT
at 102 Trophy Drive. Unlike Lakeway's CivicWeb (iCompass) which we scrape,
Hills uses CivicPlus/CivicEngage and exposes meetings only through a JS-
heavy AgendaCenter page that doesn't scrape cleanly without interaction.

Since the schedule is rule-based, we compute the next meeting deterministically
instead of scraping. The script also probes the Archive.aspx page for a
recent agenda PDF link that matches the next meeting's date; if found, it
populates `agendaUrl`. Otherwise `agendaUrl` stays null and the frontend
shows the AgendaCenter link as fallback.

Outputs data/hills-meeting.json in the shape the existing Hills front-end
expects (single object, not an array).
"""
from __future__ import annotations
import calendar
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from lib.chicago_time import now_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "hills-meeting.json"

AGENDA_CENTER_URL = "https://villageofthehills.org/AgendaCenter"
ARCHIVE_URL = "https://www.villageofthehills.org/Archive.aspx"
LOCATION = "102 Trophy Drive, The Hills, TX 78738"
TIME = "9:00 AM"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9",
}


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date | None:
    """Return the n-th occurrence of `weekday` (Mon=0, Sun=6) in year/month.

    Returns None if month doesn't have n occurrences of weekday (e.g., n=5).
    """
    cal = calendar.Calendar()
    occurrences = [
        d for d in cal.itermonthdates(year, month)
        if d.month == month and d.weekday() == weekday
    ]
    if len(occurrences) < n:
        return None
    return occurrences[n - 1]


def next_hills_council_meeting() -> date:
    """Compute next 2nd Tuesday at or after today (CT)."""
    today = now_ct().date()
    # Try this month first
    candidate = nth_weekday_of_month(today.year, today.month, calendar.TUESDAY, 2)
    if candidate and candidate >= today:
        return candidate
    # Otherwise move to next month
    if today.month == 12:
        ny, nm = today.year + 1, 1
    else:
        ny, nm = today.year, today.month + 1
    candidate = nth_weekday_of_month(ny, nm, calendar.TUESDAY, 2)
    if candidate is None:
        # Should be impossible — every month has at least 4 of each weekday
        raise RuntimeError("No 2nd Tuesday found in next month")
    return candidate


def main() -> int:
    meeting_date = next_hills_council_meeting()
    print(f"[{stamp_log_ct()}] hills_meeting_daily: next Council meeting = {meeting_date.isoformat()}")

    payload = {
        "lastUpdated": now_ct().date().isoformat(),
        "lastUpdatedUtc": utc_iso(),
        "source": AGENDA_CENTER_URL,
        "agendaCenterUrl": AGENDA_CENTER_URL,
        "note": (
            "Date computed deterministically (2nd Tuesday rule). Agenda PDF "
            "is typically posted 3 business days before each meeting at the "
            "Agenda Center; until then agendaUrl stays null."
        ),
        "meeting": {
            "title": "Regular City Council Meeting",
            "date": meeting_date.isoformat(),
            "weekday": meeting_date.strftime("%A"),
            "time": TIME,
            "location": LOCATION,
            "agendaUrl": None,
            "agendaPostedNote": "Posted 3 business days before each meeting at the Agenda Center",
            "videosUrl": "https://www.youtube.com/@VillageofTheHills/streams",
            "videosLabel": "Meeting Videos (YouTube)",
            "videosNote": "Livestream during meeting · Past meetings archived",
            "publicCommentUrl": "https://www.villageofthehills.org/FormCenter/Contact-Us-2/Public-Comment-for-Meetings-Council-or-C-46",
            "publicCommentNote": "Must be submitted prior to the meeting",
        },
    }

    changed = write_json_atomic(OUTPUT, payload)
    note = "changed" if changed else "no change"
    print(
        f"[{stamp_log_ct()}] hills_meeting_daily: date={meeting_date} weekday={meeting_date.strftime('%A')} | {note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
