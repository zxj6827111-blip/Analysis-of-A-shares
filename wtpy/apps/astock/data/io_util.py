"""Small IO helpers shared without circular imports."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """Replace a file, tolerating brief Windows scanner/reader locks."""
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            transient = isinstance(exc, PermissionError) or getattr(
                exc, "winerror", None
            ) in (5, 32)
            if not transient or attempt == 7:
                raise
            time.sleep(0.01 * (attempt + 1))


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex}.part"
    try:
        tmp.write_text(text, encoding=encoding)
        _replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
