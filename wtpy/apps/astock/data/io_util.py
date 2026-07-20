"""Small IO helpers shared without circular imports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
