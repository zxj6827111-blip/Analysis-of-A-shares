"""Tests for universe, calendar, periods."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401
from pathlib import Path

import pytest

from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.periods import aggregate_month, aggregate_week, align_closed_state
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.data.universe import is_ashare_code, to_std_code


def test_is_ashare():
    assert is_ashare_code("sh600000")
    assert is_ashare_code("sz000001")
    assert is_ashare_code("sz300750")
    assert is_ashare_code("sh688001")
    assert not is_ashare_code("sh000001")  # index
    assert not is_ashare_code("sz399001")
    assert not is_ashare_code("sh510300")  # etf
    assert not is_ashare_code("bj430047")


def test_to_std():
    assert to_std_code("sh600000") == "SSE.STK.600000"
    assert to_std_code("sz000001") == "SZSE.STK.000001"


def _bars(dates_prices):
    out = []
    for d, c in dates_prices:
        out.append(DayBar(d, c, c + 0.1, c - 0.1, c, 1.0, 100))
    return out


def test_week_aggregate_holiday_short_week():
    # Mon-Wed only short week then next week
    bars = _bars(
        [
            (20240102, 10),  # Tue
            (20240103, 11),
            (20240104, 12),
            (20240105, 13),  # Fri
            (20240108, 14),
            (20240109, 15),
        ]
    )
    weeks = aggregate_week(bars)
    assert len(weeks) >= 2
    assert weeks[0].open == 10
    assert weeks[0].close == 13
    assert weeks[0].high == pytest.approx(13.1)
    assert weeks[0].n_days == 4


def test_month_aggregate_and_open_excluded():
    bars = _bars(
        [
            (20240102, 10),
            (20240115, 11),
            (20240131, 12),
            (20240201, 13),
            (20240215, 14),
        ]
    )
    months = aggregate_month(bars, asof=20240210, include_open=False)
    # Jan closed, Feb still open on asof 20240210
    assert all(m.end_date <= 20240131 for m in months) or months[-1].date == 20240131
    assert all(m.closed for m in months)


def test_no_future_align():
    day_dates = [20240105, 20240108, 20240109]
    from wtpy.apps.astock.data.periods import PeriodBar

    higher = [
        PeriodBar(20240105, 1, 1, 1, 1, 1, 1, 20240102, 20240105, 4, True),
        PeriodBar(20240112, 1, 1, 1, 1, 1, 1, 20240108, 20240112, 5, True),
    ]
    idx = align_closed_state(day_dates, higher)
    # on 20240105 can use first week; on 20240108/09 still only first week (second ends 12)
    assert idx[0] == 0
    assert idx[1] == 0
    assert idx[2] == 0


@pytest.mark.skipif(
    not Path(r"D:\通达信\vipdoc\sh\lday\sh000001.day").exists(),
    reason="no TDX",
)
def test_calendar_from_index():
    cal = TradeCalendar.from_tdx(r"D:\通达信")
    assert len(cal) > 1000
    assert cal.is_trading_day(20160104)
    assert cal.next_trading_day(20160104) == 20160105
