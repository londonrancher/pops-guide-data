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
from typing import Any


def write_json_atomic(path: str | Path, payload: Any) -> bool:
    """Write payload as pretty-printed JSON. Returns True if the file changed.

    Atomic: writes to a sibling temp file, then renames into place. A crash
    mid-write never leaves a half-written file.

    Returns False if the new content is byte-identical to what's already on
    disk — callers can use that to short-circuit per-file commits if they want.
    """
    path = Path(path)
    new_text = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"

    if path.exists():
        old_text = path.read_text(encoding="utf-8")
        if old_text == new_text:
            return False

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
