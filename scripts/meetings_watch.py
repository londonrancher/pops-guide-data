#!/usr/bin/env python3
"""Twice-daily cancellation/reschedule check for in-window Lakeway meetings.

This is the narrower mid-day pass that catches CivicWeb changes posted
after the 6:30 AM full refresh. Runs at 11 AM + 3 PM CT.

Procedure:
  1. Read data/lakeway-meetings.json. Filter to meetings in the next
     7 days (today through today+7). If empty, log and exit.
  2. For each in-window meeting, fetch its individual CivicWeb page
     (Playwright). Classify any change:
       - title contains CANCELLED / CANCELED / CANCELLATION → 'cancelled'
       - title contains RESCHEDULED → 'rescheduled'
       - meeting ID no longer found on CivicWeb → 'cancelled'
       - time differs from JSON → update + mark 'rescheduled' with statusNote
       - agendaUrl/packetUrl now present (were null before) → update silently
       - no change → leave as-is
  3. Rewrite data/lakeway-meetings.json (atomic) only if any meeting changed.
  4. Always append a one-line summary to data/meeting-watch.log.

Hard safety guarantee: this script ALWAYS appends a log line, even when
something goes wrong. Past Mac-task scheduled runs failed silently — that's
the bug this never reproduces.
"""
from __future__ import annotations
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.browser import headless_page
from lib.chicago_time import now_ct, stamp_log_ct, utc_iso
from lib.commit_if_changed import write_json_atomic

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MEETINGS_JSON = DATA_DIR / "lakeway-meetings.json"
LOG_FILE = DATA_DIR / "meeting-watch.log"

WINDOW_DAYS = 7
MEETING_URL_FMT = "https://lakeway-tx.civicweb.net/Portal/MeetingInformation.aspx?Org=Cal&Id={id}"

CANCEL_RE = re.compile(r"\b(CANCELL?ED|CANCELLATION)\b", re.I)
RESCH_RE = re.compile(r"\bRESCHEDULED\b", re.I)
TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9])\s*:\s*([0-5]\d)\s*(AM|PM)\b", re.I)


def append_log(line: str) -> None:
    """Append one line to meeting-watch.log. Never raises."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as e:
        # Last-ditch: print to stderr so it ends up in the workflow log.
        print(f"[log-append-failed] {line.rstrip()} ({e})", file=sys.stderr)


def fetch_meeting_state(page, meeting_id: int) -> dict:
    """Probe one CivicWeb meeting page. Returns a dict with what we observed.

    Keys:
      reachable (bool)
      title (str | None)
      time (str | None)
      cancelled (bool)
      rescheduled (bool)
      notFound (bool) — page exists but no meeting record
      agendaUrl (str | None)
      packetUrl (str | None)
    """
    url = MEETING_URL_FMT.format(id=meeting_id)
    state: dict = {
        "reachable": False,
        "title": None,
        "time": None,
        "cancelled": False,
        "rescheduled": False,
        "notFound": False,
        "agendaUrl": None,
        "packetUrl": None,
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        info = page.evaluate(
            """() => {
              const titleEl = document.querySelector('h1, h2, .meeting-title');
              const title = titleEl ? titleEl.textContent.trim() : '';
              const body = document.body.innerText || '';
              const docs = Array.from(document.querySelectorAll('a'))
                .filter(a => /^(agenda|agenda packet)$/i.test((a.textContent || '').trim()))
                .map(a => ({ label: a.textContent.trim(), href: a.href }));
              return { title, body, docs };
            }"""
        )
        state["reachable"] = True
        title = (info.get("title") or "").strip()
        body = info.get("body") or ""
        state["title"] = title

        # Check for "not found" / 404-ish page text. iCompass shows a plain
        # error when the meeting is deleted.
        if not title or re.search(r"meeting\s+not\s+found|no\s+meeting\s+(was\s+)?found", body, re.I):
            state["notFound"] = True
            return state

        if CANCEL_RE.search(title):
            state["cancelled"] = True
        if RESCH_RE.search(title):
            state["rescheduled"] = True

        m = TIME_RE.search(body)
        if m:
            hh, mm, ampm = m.groups()
            state["time"] = f"{int(hh)}:{mm} {ampm.upper()}"

        for d in info.get("docs") or []:
            label = (d.get("label") or "").lower()
            href = d.get("href") or ""
            if label == "agenda" and not state["agendaUrl"]:
                if href and "printPdf=" not in href:
                    href = href + ("&" if "?" in href else "?") + "printPdf=true"
                state["agendaUrl"] = href
            elif label == "agenda packet" and not state["packetUrl"]:
                state["packetUrl"] = href
    except Exception as e:
        print(f"[civicweb] Id={meeting_id} fetch failed: {e}", file=sys.stderr)
    return state


def main() -> int:
    # Step 0 — sentinel log line, before anything that can fail.
    append_log(f"{stamp_log_ct()} | started (cloud)")

    if not MEETINGS_JSON.exists():
        append_log(f"{stamp_log_ct()} | error: lakeway-meetings.json missing — run meetings-daily first")
        print("lakeway-meetings.json missing", file=sys.stderr)
        return 2

    try:
        data = json.loads(MEETINGS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        append_log(f"{stamp_log_ct()} | error: lakeway-meetings.json malformed — {e}")
        return 2

    today = now_ct().date()
    horizon = today + timedelta(days=WINDOW_DAYS)
    in_window = []
    for m in data.get("meetings", []):
        try:
            dt = date.fromisoformat(m.get("date", ""))
        except ValueError:
            continue
        if today <= dt <= horizon:
            in_window.append(m)

    if not in_window:
        append_log(f"{stamp_log_ct()} | no in-window meetings — nothing to check")
        print(f"[{stamp_log_ct()}] meetings_watch: no in-window meetings; exit clean")
        return 0

    changes = []
    counters = {"unchanged": 0, "cancelled": 0, "rescheduled": 0, "time-shifted": 0, "unreachable": 0}

    try:
        with headless_page() as page:
            for m in in_window:
                mid = m.get("id")
                if not mid:
                    continue
                state = fetch_meeting_state(page, int(mid))

                if not state["reachable"]:
                    counters["unreachable"] += 1
                    continue

                changed = False
                if state["notFound"]:
                    # Page exists but record is gone → treat as cancelled.
                    if m.get("status") != "cancelled":
                        m["status"] = "cancelled"
                        m["statusNote"] = "Removed from CivicWeb (cancelled)"
                        counters["cancelled"] += 1
                        changes.append(f"Id={mid} → cancelled (not on CivicWeb)")
                        changed = True
                elif state["cancelled"]:
                    if m.get("status") != "cancelled":
                        m["status"] = "cancelled"
                        m["statusNote"] = "Cancellation reflected on CivicWeb"
                        counters["cancelled"] += 1
                        changes.append(f"Id={mid} → cancelled (title)")
                        changed = True
                elif state["rescheduled"]:
                    if m.get("status") != "rescheduled":
                        m["status"] = "rescheduled"
                        m["statusNote"] = "Reschedule reflected on CivicWeb"
                        counters["rescheduled"] += 1
                        changes.append(f"Id={mid} → rescheduled (title)")
                        changed = True
                elif state["time"] and m.get("time") and state["time"] != m["time"]:
                    counters["time-shifted"] += 1
                    changes.append(f"Id={mid} time {m['time']} → {state['time']}")
                    m["time"] = state["time"]
                    m["status"] = "rescheduled"
                    m["statusNote"] = f"Time updated from CivicWeb at {stamp_log_ct()}"
                    changed = True

                # Always refresh agenda/packet URLs if newly available.
                if state["agendaUrl"] and m.get("agendaUrl") != state["agendaUrl"]:
                    m["agendaUrl"] = state["agendaUrl"]
                    changed = True
                if state["packetUrl"] and m.get("packetUrl") != state["packetUrl"]:
                    m["packetUrl"] = state["packetUrl"]
                    changed = True

                if not changed:
                    counters["unchanged"] += 1
    except Exception as e:
        append_log(f"{stamp_log_ct()} | error: civicweb playwright session failed — {e}")
        print(f"playwright failure: {e}", file=sys.stderr)
        return 2

    # Rewrite JSON only if something actually changed.
    if changes:
        data["lastUpdatedAt"] = datetime.now(now_ct().tzinfo).isoformat(timespec="seconds")
        write_json_atomic(MEETINGS_JSON, data)

    summary = (
        f"in-window: {len(in_window)} | unchanged: {counters['unchanged']} | "
        f"cancelled: {counters['cancelled']} | rescheduled: {counters['rescheduled']} | "
        f"time-shifted: {counters['time-shifted']} | unreachable: {counters['unreachable']}"
    )
    append_log(f"{stamp_log_ct()} | {summary}")
    if changes:
        append_log(f"{stamp_log_ct()} | changes: {'; '.join(changes)}")

    print(f"[{stamp_log_ct()}] meetings_watch: {summary}")
    if changes:
        print(f"[{stamp_log_ct()}] meetings_watch changes: {'; '.join(changes)}")

    return 0


if __name__ == "__main__":
    try:
        rc = main()
        sys.exit(rc)
    except KeyboardInterrupt:
        append_log(f"{stamp_log_ct()} | error: interrupted")
        sys.exit(130)
    except Exception as e:
        # Never let an unexpected exception bypass the logging contract.
        append_log(f"{stamp_log_ct()} | error: unhandled — {type(e).__name__}: {e}")
        raise
