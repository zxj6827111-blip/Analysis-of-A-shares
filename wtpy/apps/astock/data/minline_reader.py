"""Tongdaxin .lc1 1-minute bar reader and 60-minute aggregation."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .tdx_reader import DayBar

# Common TDX 1-minute record: 32 bytes. First 28 used; trailing pad.
LC1_FMT = "<HHffffII"
LC1_SIZE = 32


@dataclass(frozen=True)
class MinBar:
    """Intraday bar. date=YYYYMMDD, time=HHMM (e.g. 1030)."""

    date: int
    time: int
    open: float
    high: float
    low: float
    close: float
    amount: float
    volume: float

    @property
    def datetime_key(self) -> int:
        return int(self.date) * 10000 + int(self.time)


def _decode_tdx_date(raw: int) -> int:
    """TDX packed date → YYYYMMDD."""
    y = raw // 2048 + 2004
    m = (raw % 2048) // 100
    d = (raw % 2048) % 100
    if m < 1 or m > 12 or d < 1 or d > 31:
        return 0
    return y * 10000 + m * 100 + d


def _decode_tdx_time(raw: int) -> int:
    """TDX minute time field → HHMM.

    Observed values are often like 571, 900, 1130, 1500.
    571 → 09:31 style packing in some builds; prefer if raw>=100 treat as HHMM,
    else map: hours = raw // 60, minutes = raw % 60 with 9:30 offset variants.
    """
    if raw >= 100:
        # already HHMM-ish
        hh = raw // 100
        mm = raw % 100
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 100 + mm
    # minutes from midnight or from session
    if 0 <= raw < 24 * 60:
        hh = raw // 60
        mm = raw % 60
        return hh * 100 + mm
    return int(raw)


def raw_code_from_std(std_code: str) -> str:
    """SSE.STK.600000 → sh600000."""
    parts = std_code.split(".")
    code = parts[-1]
    if std_code.startswith("SSE"):
        return "sh" + code
    if std_code.startswith("SZSE"):
        return "sz" + code
    if code.startswith(("sh", "sz")):
        return code
    return "sh" + code if code.startswith("6") else "sz" + code


def lc1_path(tdx_root: Path, std_code: str) -> Path:
    raw = raw_code_from_std(std_code)
    exch = raw[:2]
    return Path(tdx_root) / "vipdoc" / exch / "minline" / f"{raw}.lc1"


def read_lc1(
    path: Path,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> List[MinBar]:
    path = Path(path)
    if not path.exists():
        return []
    data = path.read_bytes()
    if len(data) < LC1_SIZE:
        return []
    n = len(data) // LC1_SIZE
    out: List[MinBar] = []
    for i in range(n):
        chunk = data[i * LC1_SIZE : (i + 1) * LC1_SIZE]
        try:
            d_raw, t_raw, o, h, l, c, amount, vol = struct.unpack_from(LC1_FMT, chunk)
        except struct.error:
            continue
        d = _decode_tdx_date(int(d_raw))
        if d <= 0:
            continue
        if start and d < int(start):
            continue
        if end and d > int(end):
            continue
        t = _decode_tdx_time(int(t_raw))
        out.append(
            MinBar(
                date=d,
                time=t,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                amount=float(amount),
                volume=float(vol),
            )
        )
    return out


def read_symbol_minutes(
    tdx_root: Path,
    std_code: str,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> List[MinBar]:
    return read_lc1(lc1_path(tdx_root, std_code), start=start, end=end)


def _session_bucket(time_hhmm: int) -> int:
    """Map HHMM to 60-minute bucket index within A-share session (0..3 morning, 4..7 afternoon).

    Morning 09:30-11:30 → 4 half-hours; we group into 60m: 10:30, 11:30 ends.
    Afternoon 13:00-15:00 → 14:00, 15:00 ends.
    Simpler: floor by clock hour with special 09:xx → first bucket ending 10:30.
    """
    hh = time_hhmm // 100
    mm = time_hhmm % 100
    minutes = hh * 60 + mm
    # session minutes from 9:30
    open_m = 9 * 60 + 30
    lunch_end = 13 * 60
    if minutes < open_m:
        return 0
    if minutes <= 11 * 60 + 30:
        # 0..120 minutes from open → buckets of 60 → 0,1
        return max(0, (minutes - open_m) // 60)
    if minutes < lunch_end:
        return 1
    # afternoon from 13:00
    return 2 + max(0, (minutes - lunch_end) // 60)


def aggregate_min60(bars: Sequence[MinBar]) -> List[DayBar]:
    """Aggregate 1-minute bars into ~60-minute bars.

    Returns DayBar list where ``date`` is period end trading date (YYYYMMDD)
    and multiple bars share the same date (portfolio will collapse same-day signals).
    For formula runtime we need unique sequential bars — use synthetic date keys
    YYYYMMDD * 100 + bucket (01..08) encoded carefully.

    We encode bar identity as YYYYMMDD for end-of-day alignment tools, but keep
    full sequence via separate datetime. For runtime arrays, date field uses
    YYYYMMDDHH where HH is bucket end hour (10,11,14,15) → still unique-ish.
    """
    if not bars:
        return []
    # group key: (date, bucket)
    groups: Dict[Tuple[int, int], List[MinBar]] = {}
    order: List[Tuple[int, int]] = []
    for b in bars:
        buck = _session_bucket(int(b.time))
        key = (int(b.date), buck)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)

    out: List[DayBar] = []
    for key in order:
        g = groups[key]
        d, buck = key
        # encode unique sequential date-like int: YYYYMMDD * 100 + (buck+1)
        # stays within int and sortable
        date_key = int(d) * 100 + (buck + 1)
        out.append(
            DayBar(
                date=date_key,
                open=g[0].open,
                high=max(x.high for x in g),
                low=min(x.low for x in g),
                close=g[-1].close,
                amount=sum(x.amount for x in g),
                volume=sum(x.volume for x in g),
                reserved=int(d),  # real trading day for signal mapping
            )
        )
    return out


def min60_bars_to_arrays(bars: Sequence[DayBar]) -> Dict[str, np.ndarray]:
    n = len(bars)
    return {
        "date": np.array([b.date for b in bars], dtype=np.int64),
        "open": np.array([b.open for b in bars], dtype=np.float64),
        "high": np.array([b.high for b in bars], dtype=np.float64),
        "low": np.array([b.low for b in bars], dtype=np.float64),
        "close": np.array([b.close for b in bars], dtype=np.float64),
        "volume": np.array([b.volume for b in bars], dtype=np.float64),
        "amount": np.array([b.amount for b in bars], dtype=np.float64),
        "trade_date": np.array(
            [b.reserved if b.reserved else (b.date // 100) for b in bars],
            dtype=np.int64,
        ),
    }


def load_min60_daybars(
    tdx_root: Path,
    std_code: str,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> List[DayBar]:
    mins = read_symbol_minutes(tdx_root, std_code, start=start, end=end)
    return aggregate_min60(mins)


#: Latest trading day covered by the vendor-exported 60-minute CSV archives.
#: Requests beyond this date must be rejected explicitly (never silently
#: truncated) unless .lc1 files provide newer coverage.
MIN_VENDOR_60MIN_MAX_DATE = 20260717


def load_min60_daybars_any(
    tdx_root: Path,
    minute_vendor_root: Optional[Path],
    std_code: str,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Tuple[List[DayBar], str]:
    """Load 60-minute bars: vendor CSV archives first, .lc1 binary fallback.

    Returns ``(bars, source)`` where source is one of:
      - "vendor_csv": bars came from the exported minute CSV archives;
      - "tdx_lc1":    bars came from local .lc1 binary minute files;
      - "none":       no data anywhere.

    CSV coverage ends at MIN_VENDOR_60MIN_MAX_DATE; callers that request a
    later end date must reject (or note) the truncation explicitly rather
    than silently returning a partial series.
    """
    from .minute_vendor import MinuteVendorReader

    if minute_vendor_root is not None:
        try:
            reader = MinuteVendorReader(minute_vendor_root)
            if reader.health_check():
                bars = reader.read_symbol_minutes(std_code, start=start, end=end)
                if bars:
                    return bars, "vendor_csv"
        except Exception:
            pass  # fall through to .lc1

    mins = read_symbol_minutes(tdx_root, std_code, start=start, end=end)
    if not mins:
        return [], "none"
    return aggregate_min60(mins), "tdx_lc1"


def min60_coverage_summary(tdx_root: Path, sample_codes: Sequence[str]) -> dict:
    """Quick coverage for UI banner."""
    ok = 0
    total = 0
    first = None
    last = None
    for code in sample_codes:
        total += 1
        bars = read_symbol_minutes(tdx_root, code)
        if not bars:
            continue
        ok += 1
        if first is None or bars[0].date < first:
            first = bars[0].date
        if last is None or bars[-1].date > last:
            last = bars[-1].date
    return {
        "sample_size": total,
        "with_minline": ok,
        "min_date": first,
        "max_date": last,
        "available": ok > 0,
    }
