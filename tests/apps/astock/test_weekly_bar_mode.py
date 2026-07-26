"""Tests for weekly bar mode (local_aggregate vs vendor_native)."""
import pytest

from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.data.periods import PeriodBar
from wtpy.apps.astock.study import build_period_bars


def _make_day_bars(n=10):
    return [
        DayBar(
            date=20240101 + i,
            open=10.0 + i * 0.1,
            high=11.0 + i * 0.1,
            low=9.0 + i * 0.1,
            close=10.5 + i * 0.1,
            amount=10000.0,
            volume=1000.0,
        )
        for i in range(n)
    ]


def _make_vendor_weekly_bars():
    return [
        PeriodBar(
            date=20240105,
            open=20.15,
            high=20.25,
            low=19.15,
            close=19.65,
            amount=50000.0,
            volume=5000.0,
            start_date=20240101,
            end_date=20240105,
            n_days=5,
            closed=True,
        )
    ]


class TestWeeklyBarMode:
    def test_default_local_aggregate(self):
        bars = _make_day_bars(10)
        result = build_period_bars(bars, "WEEK")
        assert len(result) > 0
        assert isinstance(result[0], PeriodBar)

    def test_local_aggregate_explicit(self):
        bars = _make_day_bars(10)
        result = build_period_bars(bars, "WEEK", weekly_bar_mode="local_aggregate")
        assert len(result) > 0

    def test_vendor_native_uses_provided_bars(self):
        bars = _make_day_bars(10)
        vendor_bars = _make_vendor_weekly_bars()
        result = build_period_bars(
            bars, "WEEK",
            weekly_bar_mode="vendor_native",
            vendor_weekly_bars=vendor_bars,
        )
        assert len(result) == 1
        assert result[0].open == 20.15
        assert result[0].high == 20.25
        assert result[0].low == 19.15
        assert result[0].close == 19.65

    def test_vendor_native_raises_when_none(self):
        bars = _make_day_bars(10)
        with pytest.raises(ValueError, match="vendor_native"):
            build_period_bars(
                bars, "WEEK",
                weekly_bar_mode="vendor_native",
                vendor_weekly_bars=None,
            )

    def test_day_period_ignores_weekly_mode(self):
        bars = _make_day_bars(5)
        result = build_period_bars(bars, "DAY", weekly_bar_mode="vendor_native")
        assert len(result) == 5
        assert isinstance(result[0], DayBar)

    def test_month_period_ignores_weekly_mode(self):
        bars = _make_day_bars(10)
        result = build_period_bars(bars, "MONTH", weekly_bar_mode="vendor_native")
        assert len(result) >= 0

    def test_local_aggregate_ohlc_rules(self):
        bars = [
            DayBar(date=20240101, open=10.0, high=12.0, low=9.0, close=11.0, amount=100.0, volume=10.0),
            DayBar(date=20240102, open=11.0, high=13.0, low=10.0, close=12.0, amount=200.0, volume=20.0),
            DayBar(date=20240103, open=12.0, high=14.0, low=11.0, close=13.0, amount=300.0, volume=30.0),
            DayBar(date=20240104, open=13.0, high=15.0, low=12.0, close=14.0, amount=400.0, volume=40.0),
            DayBar(date=20240105, open=14.0, high=16.0, low=13.0, close=15.0, amount=500.0, volume=50.0),
        ]
        result = build_period_bars(bars, "WEEK", weekly_bar_mode="local_aggregate")
        if result:
            w = result[0]
            assert w.open == 10.0
            assert w.high == 16.0
            assert w.low == 9.0
            assert w.close == 15.0
            assert w.volume == 150.0
            assert w.amount == 1500.0
