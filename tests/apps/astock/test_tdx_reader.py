"""Unit tests: TDX day reader."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401
import struct
from pathlib import Path

import pytest

from wtpy.apps.astock.data.tdx_reader import (
    DayParseError,
    parse_day_bytes,
    parse_day_file,
)


def _pack(date, o, h, l, c, amount=1.0, vol=100, reserved=0):
    # prices in fen
    return struct.pack(
        "<IIIIIfII",
        date,
        int(round(o * 100)),
        int(round(h * 100)),
        int(round(l * 100)),
        int(round(c * 100)),
        float(amount),
        int(vol),
        int(reserved),
    )


def test_parse_basic():
    data = _pack(20160104, 12.00, 12.03, 11.23, 11.33, 660376000.0, 56349700)
    bars, issues = parse_day_bytes(data)
    assert len(bars) == 1
    assert bars[0].date == 20160104
    assert bars[0].open == 12.00
    assert bars[0].high == 12.03
    assert bars[0].low == 11.23
    assert bars[0].close == 11.33


def test_bad_length():
    with pytest.raises(DayParseError):
        parse_day_bytes(b"\x00" * 31)


def test_issues_non_positive():
    data = _pack(20160104, 0, 1, 1, 1)
    bars, issues = parse_day_bytes(data)
    assert bars
    assert any("non_positive" in x for iss in issues for x in iss["issues"])


def test_date_out_of_order():
    data = _pack(20160105, 1, 1, 1, 1) + _pack(20160104, 1, 1, 1, 1)
    bars, issues = parse_day_bytes(data)
    assert any("date_out_of_order" in x for iss in issues for x in iss["issues"])


@pytest.mark.skipif(
    not Path(r"D:\通达信\vipdoc\sh\lday\sh600000.day").exists(),
    reason="local TDX data not present",
)
def test_real_sh600000_first_last():
    bars, issues = parse_day_file(r"D:\通达信\vipdoc\sh\lday\sh600000.day")
    assert bars[0].date == 20160104
    assert bars[0].open == 18.28
    assert bars[0].high == 18.28
    assert bars[0].low == 17.55
    assert bars[0].close == 17.80
    assert bars[-1].date == 20260717
    assert bars[-1].open == 8.85
    assert bars[-1].close == 8.87


@pytest.mark.skipif(
    not Path(r"D:\通达信\vipdoc\sz\lday\sz000001.day").exists(),
    reason="local TDX data not present",
)
def test_real_sz000001_first_last():
    bars, issues = parse_day_file(r"D:\通达信\vipdoc\sz\lday\sz000001.day")
    assert bars[0].date == 20160104
    assert bars[0].open == 12.00
    assert bars[-1].date == 20260717
    assert bars[-1].close == 10.78
