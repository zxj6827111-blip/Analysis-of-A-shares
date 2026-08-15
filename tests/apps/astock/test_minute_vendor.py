"""Unit tests for the vendor-exported 60-minute CSV reader."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from wtpy.apps.astock.data.minline_reader import (
    MIN_VENDOR_60MIN_MAX_DATE,
    load_min60_daybars_any,
)
from wtpy.apps.astock.data.minute_vendor import (
    MinuteVendorReader,
    canonical_to_vendor_code,
    vendor_code_to_canonical,
)


def _make_minute_csv(code: str, day: int = 20260707) -> bytes:
    """Synthetic 4-bar 60-min CSV for one trading day (real vendor format:
    datetime is "YYYY-MM-DD HH:MM:SS", columns open,close,high,low)."""
    ds = f"{day // 10000:04d}-{(day // 100) % 100:02d}-{day % 100:02d}"
    lines = ["datetime,code,name,open,close,high,low,volume,amount,pct_chg,amplitude"]
    for i, t in enumerate(("10:30", "11:30", "14:00", "15:00")):
        o = 10.0 + i
        c = 10.5 + i
        lines.append(
            f"{ds} {t}:00,{code},测试,{o},{c},{o + 0.2},{o - 0.2},"
            f"{1000 + i},{2000000 + i},0,0"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def vendor_root(tmp_path: Path) -> Path:
    root = tmp_path / "股票历史数据"
    hs = root / "沪深分钟数据" / "Stock_60min_2000-now"
    bj = root / "京市分钟数据" / "StockJ_60min_2005-now"
    hs.mkdir(parents=True)
    bj.mkdir(parents=True)
    # monthly zip (sh code)
    with zipfile.ZipFile(hs / "2026-07_60min.zip", "w") as zf:
        zf.writestr("2026-07_60min/20260707_60min/sh600000.csv",
                    _make_minute_csv("sh600000"))
        zf.writestr("2026-07_60min/20260707_60min/sz000001.csv",
                    _make_minute_csv("sz000001"))
    # annual zip with year-suffixed name (BSE)
    with zipfile.ZipFile(bj / "2025_60min.zip", "w") as zf:
        zf.writestr("2025_60min/bj920906_2025.csv",
                    _make_minute_csv("bj920906", day=20250701))
    return root


def test_code_mapping():
    assert canonical_to_vendor_code("SSE.STK.688136") == "sh688136"
    assert canonical_to_vendor_code("BSE.STK.920906") == "bj920906"
    assert vendor_code_to_canonical("sh688136") == "SSE.STK.688136"
    assert vendor_code_to_canonical("bj920906") == "BSE.STK.920906"
    assert vendor_code_to_canonical("bj920748_2025") == "BSE.STK.920748"


def test_read_hs_monthly(vendor_root: Path):
    r = MinuteVendorReader(vendor_root)
    bars = r.read_symbol_minutes("SSE.STK.600000", start=20260707, end=20260707)
    assert len(bars) == 4
    assert bars[0].date == 2026070701
    assert bars[-1].date == 2026070704
    assert bars[0].open == 10.0
    # unit conversion: 手->股 x100, 千元->元 x1000
    assert bars[0].volume == 100000.0
    assert bars[0].amount == 2000000000.0
    assert bars[0].reserved == 20260707


def test_read_bse_annual(vendor_root: Path):
    r = MinuteVendorReader(vendor_root)
    bars = r.read_symbol_minutes("BSE.STK.920906", start=20250701, end=20250701)
    assert len(bars) == 4
    assert bars[0].reserved == 20250701


def test_load_min60_daybars_any_csv(vendor_root: Path):
    bars, src = load_min60_daybars_any(
        tdx_root=vendor_root,  # no .lc1 anywhere -> fallback would be empty
        minute_vendor_root=vendor_root,
        std_code="SSE.STK.600000",
        start=20260707,
        end=20260707,
    )
    assert src == "vendor_csv"
    assert len(bars) == 4


def test_truncation_constant():
    assert MIN_VENDOR_60MIN_MAX_DATE == 20260717
