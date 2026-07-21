# -*- coding: utf-8 -*-
"""Optional Parquet writers for research artifacts (falls back to CSV/JSON)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def parquet_available() -> bool:
    try:
        import pandas  # noqa: F401

        # prefer pyarrow but pandas may use other engines
        return True
    except Exception:
        return False


def write_records_parquet(
    path: Path,
    records: Sequence[Dict[str, Any]],
    *,
    fallback_jsonl: bool = True,
) -> Path:
    """Write list-of-dicts to parquet; if engine missing, write .jsonl beside path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(records or [])
    try:
        import pandas as pd

        df = pd.DataFrame(rows)
        # try parquet
        try:
            df.to_parquet(path, index=False)
            return path
        except Exception:
            # no pyarrow/fastparquet
            pass
    except Exception:
        pass
    if fallback_jsonl:
        alt = path.with_suffix(".jsonl")
        import json

        with alt.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return alt
    raise RuntimeError("parquet write failed and fallback disabled")


def write_events_parquet(path: Path, events: Sequence[Any]) -> Path:
    rows: List[dict] = []
    for e in events or []:
        if hasattr(e, "to_dict"):
            rows.append(e.to_dict())
        elif isinstance(e, dict):
            rows.append(e)
        else:
            rows.append(
                {
                    "std_code": getattr(e, "std_code", None),
                    "date": getattr(e, "date", None),
                    "period": getattr(e, "period", None),
                    "indicator_id": getattr(e, "indicator_id", None),
                }
            )
    return write_records_parquet(path, rows)
