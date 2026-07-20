"""Import weekly_analysis_v2 Excel into forecast snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .name_norm import normalize_stock_code

WEEKLY_NAME_RE = re.compile(
    r"weekly_analysis_v2_(?P<date>\d{8})-W(?P<week>\d+)", re.I
)


def detect_week_key_from_filename(name: str) -> Optional[str]:
    m = WEEKLY_NAME_RE.search(name)
    if not m:
        return None
    # Prefer Overview week_key when available; filename gives date+week number
    date = m.group("date")
    week = int(m.group("week"))
    year = date[:4]
    return f"{year}-W{week:02d}" if week < 100 else f"{year}-W{week}"


def _read_overview(path: Path) -> Dict[str, Any]:
    try:
        df = pd.read_excel(path, sheet_name="Overview")
        out = {}
        if "key" in df.columns and "value" in df.columns:
            for _, row in df.iterrows():
                out[str(row["key"])] = row["value"]
        return out
    except Exception:
        return {}


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item") and not isinstance(v, (bytes, str)):
        try:
            v = v.item()
        except Exception:
            pass
    # datetime / Timestamp / date
    if hasattr(v, "isoformat") and not isinstance(v, (str, bytes)):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _sheet_to_records(df: pd.DataFrame) -> List[dict]:
    rows: List[dict] = []
    for _, r in df.iterrows():
        d = {}
        for k, v in r.items():
            key = str(k)
            d[key] = _jsonable(v)
        code_raw = d.get("code")
        d["code6"] = normalize_stock_code(code_raw)
        d["name"] = str(d.get("name") or "").strip()
        rows.append(d)
    return rows


def import_weekly_xlsx(
    xlsx_path: Path | str,
    *,
    weekly_root: Path,
    activate: bool = True,
) -> Dict[str, Any]:
    """
    Parse weekly xlsx, write raw + snapshot, update index.

    Returns meta dict including week_key and counts.
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(str(xlsx_path))

    weekly_root = Path(weekly_root)
    raw_dir = weekly_root / "raw"
    snap_root = weekly_root / "snapshots"
    raw_dir.mkdir(parents=True, exist_ok=True)
    snap_root.mkdir(parents=True, exist_ok=True)

    sha = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    overview = _read_overview(xlsx_path)
    week_key = str(overview.get("week_key") or "").strip()
    if not week_key:
        week_key = detect_week_key_from_filename(xlsx_path.name) or time.strftime(
            "import-%Y%m%d"
        )

    # persist raw (overwrite safely; Windows may leave prior copy read-only)
    dest_raw = raw_dir / xlsx_path.name
    if xlsx_path.resolve() != dest_raw.resolve():
        if dest_raw.exists():
            try:
                dest_raw.chmod(0o666)
            except Exception:
                pass
            try:
                dest_raw.unlink()
            except Exception:
                # fall back to unique name if locked/read-only delete fails
                stem = dest_raw.stem
                dest_raw = raw_dir / f"{stem}_{int(time.time())}{dest_raw.suffix}"
        shutil.copy2(xlsx_path, dest_raw)
        try:
            dest_raw.chmod(0o666)
        except Exception:
            pass

    stocks: List[dict] = []
    etfs: List[dict] = []
    xl = pd.ExcelFile(xlsx_path)
    if "stock-all" in xl.sheet_names:
        stocks = _sheet_to_records(pd.read_excel(xlsx_path, sheet_name="stock-all"))
    if "etf-all" in xl.sheet_names:
        etfs = _sheet_to_records(pd.read_excel(xlsx_path, sheet_name="etf-all"))

    snap_dir = snap_root / week_key
    snap_dir.mkdir(parents=True, exist_ok=True)
    stocks_path = snap_dir / "stocks.jsonl"
    etfs_path = snap_dir / "etfs.jsonl"
    with stocks_path.open("w", encoding="utf-8") as f:
        for row in stocks:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with etfs_path.open("w", encoding="utf-8") as f:
        for row in etfs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    week_end = overview.get("week_end") or (
        stocks[0].get("week_end") if stocks else None
    )
    meta = {
        "week_key": week_key,
        "week_end": _jsonable(week_end),
        "report_format": _jsonable(overview.get("report_format")),
        "source_filename": xlsx_path.name,
        "source_sha256": sha,
        "raw_path": str(dest_raw),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stock_count": len(stocks),
        "etf_count": len(etfs),
        "stocks_path": str(stocks_path),
        "etfs_path": str(etfs_path),
    }
    (snap_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    index_path = weekly_root / "index.json"
    index = _load_json(index_path) or {"weeks": {}, "active_week_key": None}
    index.setdefault("weeks", {})[week_key] = {
        "week_key": week_key,
        "meta_path": str(snap_dir / "meta.json"),
        "imported_at": meta["imported_at"],
        "stock_count": meta["stock_count"],
        "source_filename": meta["source_filename"],
    }
    if activate or not index.get("active_week_key"):
        index["active_week_key"] = week_key
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    meta["active_week_key"] = index["active_week_key"]
    return meta


def load_snapshot_stocks(weekly_root: Path, week_key: str) -> Tuple[dict, List[dict]]:
    weekly_root = Path(weekly_root)
    snap_dir = weekly_root / "snapshots" / week_key
    meta_path = snap_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"week snapshot not found: {week_key}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows: List[dict] = []
    stocks_path = Path(meta.get("stocks_path") or (snap_dir / "stocks.jsonl"))
    if stocks_path.exists():
        with stocks_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return meta, rows


def _load_json(path: Path) -> Optional[dict]:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
