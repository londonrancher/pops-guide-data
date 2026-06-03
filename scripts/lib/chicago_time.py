"""Central Time (America/Chicago) helpers — handles CDT/CST automatically."""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def now_ct() -> datetime:
    """Current wall clock in America/Chicago."""
    return datetime.now(CT)


def now_iso_ct() -> str:
    """ISO 8601 timestamp in CT with offset (e.g. 2026-06-03T08:00:00-05:00)."""
    return now_ct().isoformat(timespec="seconds")


def stamp_log_ct() -> str:
    """Compact CT timestamp suitable for log lines: '2026-06-03 08:00 CT'."""
    return now_ct().strftime("%Y-%m-%d %H:%M CT")


def utc_iso() -> str:
    """ISO 8601 UTC timestamp with Z suffix (e.g. 2026-06-03T13:00:00Z)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
