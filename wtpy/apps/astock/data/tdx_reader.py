"""Tongdaxin (.day) binary daily bar reader.

Record layout (32 bytes, little-endian):
  date(u32), open(u32), high(u32), low(u32), close(u32),
  amount(f32), volume(u32), reserved(u32)

Prices are stored as integer fen (price * 100).
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

RECORD_SIZE = 32
RECORD_FMT = "<IIIIIfII"


@dataclass(frozen=True)
class DayBar:
    date: int  # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    amount: float
    volume: float
    reserved: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class DayParseError(ValueError):
    pass


def _price_from_fen(v: int) -> float:
    return round(v / 100.0, 2)


def validate_bar(bar: DayBar, *, strict: bool = True) -> List[str]:
    issues: List[str] = []
    if bar.date < 19900101 or bar.date > 21001231:
        issues.append(f"invalid_date:{bar.date}")
    for name, px in (
        ("open", bar.open),
        ("high", bar.high),
        ("low", bar.low),
        ("close", bar.close),
    ):
        if px <= 0:
            issues.append(f"non_positive_{name}:{px}")
    if bar.high < bar.low:
        issues.append(f"high_lt_low:{bar.high}<{bar.low}")
    if bar.high < max(bar.open, bar.close) - 1e-9:
        issues.append("high_below_oc")
    if bar.low > min(bar.open, bar.close) + 1e-9:
        issues.append("low_above_oc")
    if bar.volume < 0:
        issues.append(f"negative_volume:{bar.volume}")
    if strict and issues:
        raise DayParseError(";".join(issues))
    return issues


def parse_day_bytes(
    data: bytes,
    *,
    strict: bool = False,
    collect_issues: bool = True,
) -> Tuple[List[DayBar], List[dict]]:
    if len(data) % RECORD_SIZE != 0:
        raise DayParseError(
            f"file length {len(data)} is not a multiple of {RECORD_SIZE}"
        )
    bars: List[DayBar] = []
    issues: List[dict] = []
    n = len(data) // RECORD_SIZE
    for i in range(n):
        chunk = data[i * RECORD_SIZE : (i + 1) * RECORD_SIZE]
        date, o, h, l, c, amount, vol, reserved = struct.unpack(RECORD_FMT, chunk)
        bar = DayBar(
            date=int(date),
            open=_price_from_fen(o),
            high=_price_from_fen(h),
            low=_price_from_fen(l),
            close=_price_from_fen(c),
            amount=float(amount),
            volume=float(vol),
            reserved=int(reserved),
        )
        if collect_issues:
            bar_issues = validate_bar(bar, strict=False)
            if bar_issues:
                issues.append({"index": i, "date": bar.date, "issues": bar_issues})
        if strict:
            validate_bar(bar, strict=True)
        bars.append(bar)

    # date order / duplicates
    if collect_issues and bars:
        prev = bars[0].date
        for i in range(1, len(bars)):
            d = bars[i].date
            if d < prev:
                issues.append({"index": i, "date": d, "issues": ["date_out_of_order"]})
            if d == prev:
                issues.append({"index": i, "date": d, "issues": ["duplicate_date"]})
            prev = d
    return bars, issues


def parse_day_file(
    path: Path | str,
    *,
    strict: bool = False,
) -> Tuple[List[DayBar], List[dict]]:
    path = Path(path)
    data = path.read_bytes()
    return parse_day_bytes(data, strict=strict)


def bars_to_arrays(bars: Sequence[DayBar]) -> Dict[str, np.ndarray]:
    if not bars:
        return {
            "date": np.array([], dtype=np.int64),
            "open": np.array([], dtype=np.float64),
            "high": np.array([], dtype=np.float64),
            "low": np.array([], dtype=np.float64),
            "close": np.array([], dtype=np.float64),
            "amount": np.array([], dtype=np.float64),
            "volume": np.array([], dtype=np.float64),
        }
    return {
        "date": np.array([b.date for b in bars], dtype=np.int64),
        "open": np.array([b.open for b in bars], dtype=np.float64),
        "high": np.array([b.high for b in bars], dtype=np.float64),
        "low": np.array([b.low for b in bars], dtype=np.float64),
        "close": np.array([b.close for b in bars], dtype=np.float64),
        "amount": np.array([b.amount for b in bars], dtype=np.float64),
        "volume": np.array([b.volume for b in bars], dtype=np.float64),
    }


class TdxDayReader:
    """Read-only Tongdaxin daily bar reader."""

    def __init__(self, tdx_root: Path | str = r"D:\通达信"):
        self.tdx_root = Path(tdx_root)
        self.sh_dir = self.tdx_root / "vipdoc" / "sh" / "lday"
        self.sz_dir = self.tdx_root / "vipdoc" / "sz" / "lday"
        self.bj_dir = self.tdx_root / "vipdoc" / "bj" / "lday"

    def resolve_path(self, code: str) -> Path:
        code = code.lower().replace(".", "")
        if code.startswith("sh") or code.startswith("sz") or code.startswith("bj"):
            exch = code[:2]
            name = code + ".day"
        elif code.isdigit() and len(code) == 6:
            # guess exchange
            if code.startswith(("5", "6", "9")):
                exch, name = "sh", f"sh{code}.day"
            else:
                exch, name = "sz", f"sz{code}.day"
        else:
            raise FileNotFoundError(f"cannot resolve code: {code}")
        base = {"sh": self.sh_dir, "sz": self.sz_dir, "bj": self.bj_dir}[exch]
        path = base / name
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    def read(self, code: str) -> Tuple[List[DayBar], List[dict]]:
        return parse_day_file(self.resolve_path(code))

    def iter_files(
        self, *, include_bj: bool = False
    ) -> Iterable[Tuple[str, Path]]:
        for exch, d in (("sh", self.sh_dir), ("sz", self.sz_dir)):
            if not d.exists():
                continue
            for p in sorted(d.glob("*.day")):
                yield p.stem, p
        if include_bj and self.bj_dir.exists():
            for p in sorted(self.bj_dir.glob("*.day")):
                yield p.stem, p
