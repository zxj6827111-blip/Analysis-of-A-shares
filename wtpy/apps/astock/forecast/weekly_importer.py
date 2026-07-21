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

# sheet name aliases for stock / etf tables (case-insensitive match)
_STOCK_SHEETS = ("stock-all", "stocks", "stock_all", "全部股票", "股票")
_ETF_SHEETS = ("etf-all", "etfs", "etf_all", "全部etf", "etf", "ETF")


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


def _pick_sheet(sheet_names: List[str], aliases: Tuple[str, ...]) -> Optional[str]:
    """Pick first sheet matching alias (exact, then casefold, then contains)."""
    if not sheet_names:
        return None
    lower_map = {str(s).strip().casefold(): s for s in sheet_names}
    for a in aliases:
        key = a.casefold()
        if key in lower_map:
            return lower_map[key]
    for a in aliases:
        key = a.casefold()
        for cf, orig in lower_map.items():
            if key in cf or cf in key:
                return orig
    return None


def _sheet_to_records(df: pd.DataFrame, *, kind: str = "stock") -> List[dict]:
    rows: List[dict] = []
    for _, r in df.iterrows():
        d: Dict[str, Any] = {}
        for k, v in r.items():
            key = str(k)
            d[key] = _jsonable(v)
        code_raw = d.get("code")
        d["code6"] = normalize_stock_code(code_raw)
        d["name"] = str(d.get("name") or "").strip()
        d["kind"] = kind  # stock | etf
        # common typo in export: industy -> industry
        if d.get("industry") in (None, "") and d.get("industy") not in (None, ""):
            d["industry"] = d.get("industy")
        if d.get("code6") or d.get("name"):
            rows.append(d)
    return rows


def _read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    path = Path(path)
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def import_weekly_xlsx(
    xlsx_path: Path | str,
    *,
    weekly_root: Path,
    activate: bool = True,
) -> Dict[str, Any]:
    """
    Parse weekly xlsx, write raw + snapshot, update index.

    Supports stock-all + etf-all (and common aliases). ETF rows are searchable
    via ForecastService after load_snapshot merges both tables.

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
    stock_sheet = _pick_sheet(list(xl.sheet_names), _STOCK_SHEETS)
    etf_sheet = _pick_sheet(list(xl.sheet_names), _ETF_SHEETS)
    if stock_sheet:
        stocks = _sheet_to_records(
            pd.read_excel(xlsx_path, sheet_name=stock_sheet), kind="stock"
        )
    if etf_sheet:
        etfs = _sheet_to_records(
            pd.read_excel(xlsx_path, sheet_name=etf_sheet), kind="etf"
        )

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
        stocks[0].get("week_end") if stocks else (etfs[0].get("week_end") if etfs else None)
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
        "instrument_count": len(stocks) + len(etfs),
        "stock_sheet": stock_sheet,
        "etf_sheet": etf_sheet,
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
        "etf_count": meta["etf_count"],
        "instrument_count": meta["instrument_count"],
        "source_filename": meta["source_filename"],
    }
    if activate or not index.get("active_week_key"):
        index["active_week_key"] = week_key
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    meta["active_week_key"] = index["active_week_key"]
    return meta


def load_snapshot_stocks(
    weekly_root: Path,
    week_key: str,
    *,
    include_etf: bool = True,
) -> Tuple[dict, List[dict]]:
    """Load snapshot rows for a week.

    By default merges stock-all + etf-all so quote/search can find ETF codes.
    Stocks win on code collision (same code6).
    """
    weekly_root = Path(weekly_root)
    snap_dir = weekly_root / "snapshots" / week_key
    meta_path = snap_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"week snapshot not found: {week_key}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    stocks_path = Path(meta.get("stocks_path") or (snap_dir / "stocks.jsonl"))
    etfs_path = Path(meta.get("etfs_path") or (snap_dir / "etfs.jsonl"))

    stocks = _read_jsonl(stocks_path)
    for r in stocks:
        r.setdefault("kind", "stock")
        if not r.get("code6"):
            r["code6"] = normalize_stock_code(r.get("code"))

    etfs: List[dict] = []
    if include_etf:
        etfs = _read_jsonl(etfs_path)
        for r in etfs:
            r.setdefault("kind", "etf")
            if not r.get("code6"):
                r["code6"] = normalize_stock_code(r.get("code"))
            if r.get("industry") in (None, "") and r.get("industy") not in (None, ""):
                r["industry"] = r.get("industy")

    # merge: stocks first, then ETF only if code not already present
    by_code: Dict[str, dict] = {}
    ordered: List[dict] = []
    for r in stocks:
        c = normalize_stock_code(r.get("code6") or r.get("code"))
        if c:
            by_code[c] = r
        ordered.append(r)
    for r in etfs:
        c = normalize_stock_code(r.get("code6") or r.get("code"))
        if c and c in by_code:
            continue
        if c:
            by_code[c] = r
        ordered.append(r)

    # keep meta counts accurate even if older meta lacked etf_count
    meta.setdefault("stock_count", len(stocks))
    meta.setdefault("etf_count", len(etfs) if include_etf else meta.get("etf_count", 0))
    meta["instrument_count"] = len(ordered)
    return meta, ordered


def _load_json(path: Path) -> Optional[dict]:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
