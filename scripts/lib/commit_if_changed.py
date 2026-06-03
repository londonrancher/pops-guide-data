"""Atomic JSON writer + 'changed?' helper.

Scripts shouldn't call git directly — they just write JSON, and the workflow
runs `git diff --quiet --cached || git commit` after the script finishes.
That way local runs don't accidentally try to push.
"""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

# Default timestamp-ish keys that callers usually want excluded from the
# "did this payload actually change?" comparison. Updating only one of these
# while the rest of the data is identical should NOT produce a commit.
DEFAULT_IGNORE_KEYS = frozenset({
    "lastUpdated",
    "lastUpdatedUtc",
    "lastChecked",
    "lastCheckedUtc",
    "detectedAt",
})


def _filter(d: Any, ignore_keys: Iterable[str]) -> Any:
    """Recursively drop any dict keys in `ignore_keys`.

    Treats lists transparently (still descends into elements). Anything that
    isn't a dict or list is returned as-is.
    """
    if isinstance(d, dict):
        return {k: _filter(v, ignore_keys) for k, v in d.items() if k not in ignore_keys}
    if isinstance(d, list):
        return [_filter(x, ignore_keys) for x in d]
    return d


def write_json_atomic(
    path: str | Path,
    payload: Any,
    ignore_keys: Iterable[str] | None = None,
) -> bool:
    """Write payload as pretty-printed JSON. Returns True if the file changed.

    Atomic: writes to a sibling temp file, then renames into place. A crash
    mid-write never leaves a half-written file.

    Semantic for the return value: payload-level comparison after stripping
    `ignore_keys` (default: timestamp-ish fields). If the meaningful data is
    unchanged, the on-disk file is LEFT UNTOUCHED — keeping its old timestamps
    too — and the function returns False. That way, identical-data days
    produce zero git diffs and zero commits.

    If the meaningful data did change, the file is rewritten with the fresh
    payload (including new timestamps) and the function returns True.

    Pass `ignore_keys=[]` (empty iterable) to opt out of the timestamp
    exclusion and use byte-equality comparison instead.
    """
    path = Path(path)
    keys_to_ignore = (
        DEFAULT_IGNORE_KEYS if ignore_keys is None else frozenset(ignore_keys)
    )

    if path.exists():
        try:
            old_payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old_payload = None

        if old_payload is not None:
            old_meaningful = _filter(old_payload, keys_to_ignore)
            new_meaningful = _filter(payload, keys_to_ignore)
            if old_meaningful == new_meaningful:
                return False

    new_text = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
