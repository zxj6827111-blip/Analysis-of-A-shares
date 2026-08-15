"""Tongdaxin exported minute CSV reader (60-minute bars, SH/SZ/BSE).

Reads the vendor-exported minute CSV archives under a local data root:

  <root>/沪深分钟数据/Stock_60min_2000-now/    {2000..2025}_60min.zip, {YYYY-MM}_60min.zip
  <root>/京市分钟数据/StockJ_60min_2005-now/   {2020..2025}_60min.zip, {YYYY-MM}_60min.zip

CSV layout (confirmed by sampling, utf-8-sig):
  datetime,code,name,open,close,high,low,volume,amount,pct_chg,amplitude
  - datetime: "YYYY-MM-DD HH:MM:SS", 4 bars per trading day:
      10:30 / 11:30 / 14:00 / 15:00
  - code: sh688136 / sz000001 / bj920906 (2025 annual zips use bj920748_2025)
  - volume / amount units follow the vendor daily convention
    (手 -> 股 x100, 千元 -> 元 x1000) — same as local_vendor daily import;
    the caller may opt out via ``convert_units=False``.

Output is compatible with ``minline_reader.load_min60_daybars``: a list of
``DayBar`` where ``date`` is encoded as YYYYMMDD*100+(bucket+1) (bucket 0..3 =
10:30/11:30/14:00/15:00) and ``reserved`` carries the real trading day —
exactly the encoding ``aggregate_min60`` produces, so downstream
``min60_bars_to_arrays`` / MIN60 signal code works unchanged.

This module only READS the vendor CSV archives. It never writes to the
DatasetStore; importing minute data into the warehouse is a separate step.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .tdx_reader import DayBar

#: Vendor directory names under the minute data root.
HS_DIR = "沪深分钟数据"
BJ_DIR = "京市分钟数据"
HS_60_SUBDIR = "Stock_60min_2000-now"
BJ_60_SUBDIR = "StockJ_60min_2005-now"

#: Per-day bar timestamps in the exported 60-minute CSV.
BUCKET_TIMES = ("10:30", "11:30", "14:00", "15:00")
BUCKET_INDEX = {t: i for i, t in enumerate(BUCKET_TIMES)}


def canonical_to_vendor_code(symbol: str) -> str:
    """SSE.STK.688136 -> sh688136 ; BSE.STK.920906 -> bj920906."""
    parts = symbol.split(".")
    if len(parts) != 3:
        raise ValueError(f"invalid canonical symbol: {symbol}")
    exch, _, code = parts
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exch)
    if not prefix:
        raise ValueError(f"unsupported exchange: {exch}")
    return f"{prefix}{code}"


def vendor_code_to_canonical(vendor: str) -> str:
    """sh688136 -> SSE.STK.688136 ; bj920906 -> BSE.STK.920906."""
    m = re.match(r"^(sh|sz|bj)(\d{6})(?:_\d{4})?$", vendor)
    if not m:
        raise ValueError(f"invalid vendor minute code: {vendor}")
    prefix, num = m.group(1), m.group(2)
    exch = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}[prefix]
    return f"{exch}.STK.{num}"


def _find_60min_dirs(root: Path) -> List[Tuple[str, Path]]:
    """Locate the SH/SZ and BSE 60-minute archive dirs under a data root.

    The vendor export may sit under a nested subdirectory (e.g.
    incoming/股票历史数据/沪深分钟数据/Stock_60min_2000-now), so we search
    recursively but only a bounded depth. Returns [(exchange, dir)] where
    exchange is "hs" or "bj".
    """
    out: List[Tuple[str, Path]] = []
    seen = set()
    # Direct child names first (fast path), then bounded rglob.
    candidates = [
        root / HS_DIR / HS_60_SUBDIR,
        root / BJ_DIR / BJ_60_SUBDIR,
        root,
    ]
    for c in candidates:
        if c in seen or not c.is_dir():
            continue
        seen.add(c)
        if any(p.name.startswith("20") and p.name.endswith(".zip")
               for p in c.iterdir()):
            tag = "bj" if "StockJ" in str(c) or "京市" in str(c) else "hs"
            out.append((tag, c))
    if out:
        return out
    for depth in range(1, 4):
        for c in root.rglob("*"):
            if c.is_dir() and c.name in (HS_60_SUBDIR, BJ_60_SUBDIR):
                if c in seen:
                    continue
                seen.add(c)
                if any(p.name.startswith("20") and p.name.endswith(".zip")
                       for p in c.iterdir()):
                    tag = "bj" if c.name.startswith("StockJ") else "hs"
                    out.append((tag, c))
        if out:
            return out
    return out


def _iter_zip_entries(zpath: Path, target: Optional[str] = None):
    """Yield (name, raw-bytes) for CSV entries in a zip, skipping __MACOSX.

    ``target`` is the CSV filename (e.g. "sh688136.csv"); when None, yield
    every CSV (used for coverage scanning).
    """
    with zipfile.ZipFile(zpath) as zf:
        for name in zf.namelist():
            if "__MACOSX" in name or not name.endswith(".csv"):
                continue
            fname = Path(name).name
            if target is not None and fname != target:
                continue
            yield name, zf.read(name)


class MinuteVendorReader:
    """Reads exported 60-minute CSV archives (SH/SZ + BSE)."""

    def __init__(
        self,
        root: Path | str,
        *,
        convert_units: bool = True,
    ):
        self.root = Path(root)
        self.convert_units = convert_units
        self._dirs: List[Tuple[str, Path]] = _find_60min_dirs(self.root)
        self._zips: List[Tuple[str, Path]] = []
        for tag, d in self._dirs:
            for z in sorted(d.glob("20*.zip")):
                self._zips.append((tag, z))

    def health_check(self) -> bool:
        return len(self._dirs) > 0 and len(self._zips) > 0

    def archive_count(self) -> int:
        return len(self._zips)

    def _parse_csv(
        self,
        raw: bytes,
        *,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[DayBar]:
        """Parse one exported minute CSV into DayBar list (60-min encoded)."""
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        bars: List[DayBar] = []
        for row in reader:
            dt = (row.get("datetime") or "").strip()
            if len(dt) < 16:
                continue
            try:
                date_s = dt[:10].replace("-", "")
                trade_date = int(date_s)
                tstr = dt[11:16]
            except (ValueError, TypeError):
                continue
            if trade_date < 19900101 or trade_date > 21001231:
                continue
            if start and trade_date < int(start):
                continue
            if end and trade_date > int(end):
                continue
            buck = BUCKET_INDEX.get(tstr)
            if buck is None:
                # Non-standard timestamp; fall back to session bucketing.
                hhmm = int(tstr.replace(":", ""))
                buck = _fallback_bucket(hhmm)
            try:
                o = float(row.get("open"))
                c = float(row.get("close"))
                h = float(row.get("high"))
                l = float(row.get("low"))
                v = float(row.get("volume") or 0)
                a = float(row.get("amount") or 0)
            except (ValueError, TypeError):
                continue
            if self.convert_units:
                v *= 100.0      # 手 -> 股
                a *= 1000.0     # 千元 -> 元
            bars.append(
                DayBar(
                    date=trade_date * 100 + (buck + 1),
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    amount=a,
                    volume=v,
                    reserved=trade_date,
                )
            )
        bars.sort(key=lambda b: b.date)
        return bars

    def read_symbol_minutes(
        self,
        symbol: str,
        *,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[DayBar]:
        """Read 60-minute bars for a canonical symbol across all archives."""
        vendor = canonical_to_vendor_code(symbol)
        prefix = vendor[:2]
        fname = f"{vendor}.csv"
        # Annual archives (both SH/SZ and BSE) name files sh600000_2025.csv —
        # match the prefix so the year-suffixed variant is found too.
        fname_prefix = f"{vendor}_"
        by_date: Dict[int, DayBar] = {}
        for tag, z in self._zips:
            # The SH/SZ archive dir holds both sh* and sz* files; the BSE
            # archive dir holds only bj* files. Match by exchange family.
            if prefix == "bj" and tag != "bj":
                continue
            if prefix != "bj" and tag == "bj":
                continue
            try:
                for name, raw in _iter_zip_entries(z, target=None):
                    base = Path(name).name
                    if base != fname and not base.startswith(fname_prefix):
                        continue
                    for b in self._parse_csv(raw, start=start, end=end):
                        by_date[b.date] = b
            except (zipfile.BadZipFile, OSError):
                continue
        return [by_date[d] for d in sorted(by_date.keys())]

    def coverage_summary(self, sample: Sequence[str]) -> dict:
        """Quick coverage for UI banner: how many sample codes have data."""
        ok = 0
        first = None
        last = None
        for code in sample:
            bars = self.read_symbol_minutes(code)
            if not bars:
                continue
            ok += 1
            rd = [b.reserved for b in bars if b.reserved]
            if rd:
                if first is None or min(rd) < first:
                    first = min(rd)
                if last is None or max(rd) > last:
                    last = max(rd)
        return {
            "sample_size": len(sample),
            "with_min60:vendor_csv": ok,
            "min_date": first,
            "max_date": last,
            "available": ok > 0,
        }


def _fallback_bucket(hhmm: int) -> int:
    """Map HHMM to 60-min bucket when the timestamp is non-standard."""
    minutes = (hhmm // 100) * 60 + (hhmm % 100)
    if minutes < 10 * 60 + 30:
        return 0
    if minutes < 11 * 60 + 31:
        return 1
    if minutes < 14 * 60:
        return 2
    return 3
