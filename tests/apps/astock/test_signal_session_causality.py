# -*- coding: utf-8 -*-
"""P1.3 / P1.4: signal session timing & higher-period closed-bar causality.

Locks phase-1 policy:
- Close-confirmed signals never trade the same bar without lag (entry_lag >= 1).
- entry_lag=1 + buy_on open  -> buy T+1 open (not T).
- entry_lag=1 + buy_on close -> buy T+1 close (not T close).
- WEEK/MONTH aggregation excludes unfinished periods by default; align_closed_state
  never points at a higher-period bar that ends after the day.
"""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.periods import (
    PeriodBar,
    aggregate_month,
    aggregate_week,
    align_closed_state,
)
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent, build_period_bars


def _cfg(capital: float = 1_000_000.0) -> AStockConfig:
    cfg = AStockConfig()
    cfg.initial_capital = capital
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0, slippage=0.0
    )
    return cfg


def test_signal_T_entry_lag1_buy_on_open_is_T1_open_not_T():
    """Signal confirmed on day T close; buy next session open only."""
    code = "SSE.STK.600000"
    T, T1, T2 = 20240103, 20240104, 20240105
    dates = [T, T1, T2]
    # Distinct prices so wrong-session fills are obvious.
    bars = {
        code: [
            DayBar(T, 9.0, 9.5, 8.5, 9.2, 1.0, 1000),  # signal day: open 9 close 9.2
            DayBar(T1, 10.0, 11.0, 9.5, 10.5, 1.0, 1000),  # entry: open 10 close 10.5
            DayBar(T2, 11.0, 12.0, 10.5, 11.5, 1.0, 1000),
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, T, "DAY", "t")],
        hold=1,
        entry_lag=1,
        period="DAY",
        buy_on="open",
        sell_on="open",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].date == T1, "must not buy on signal day T"
    assert abs(buys[0].price - 10.0) < 1e-9, "must use T+1 open, not T open/close"
    # Explicit anti-look-ahead: no fill on signal date
    assert all(f.date != T for f in res.fills if f.side == "BUY")


def test_signal_T_entry_lag1_buy_on_close_is_T1_close_not_T_close():
    """buy_on=close still waits entry_lag; never same-bar close as signal confirmation."""
    code = "SSE.STK.600000"
    T, T1, T2 = 20240103, 20240104, 20240105
    dates = [T, T1, T2]
    bars = {
        code: [
            DayBar(T, 9.0, 9.5, 8.5, 9.2, 1.0, 1000),  # signal close 9.2 — forbidden fill
            DayBar(T1, 10.0, 11.0, 9.5, 10.7, 1.0, 1000),  # buy close 10.7
            DayBar(T2, 11.0, 12.0, 10.5, 11.5, 1.0, 1000),
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, T, "DAY", "t")],
        hold=1,
        entry_lag=1,
        period="DAY",
        buy_on="close",
        sell_on="open",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].date == T1
    assert abs(buys[0].price - 10.7) < 1e-9
    # Must not fill at signal bar close (9.2) or open (9.0)
    assert abs(buys[0].price - 9.2) > 1e-6
    assert abs(buys[0].price - 9.0) > 1e-6


def test_entry_lag_rejects_same_bar_zero_lag():
    """Product policy: entry_lag must be >= 1 (no same-session trade after close confirm)."""
    code = "SSE.STK.600000"
    dates = [20240103, 20240104]
    bars = {
        code: [
            DayBar(20240103, 10, 11, 9, 10, 1, 1000),
            DayBar(20240104, 10, 11, 9, 10, 1, 1000),
        ]
    }
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    try:
        bt.run(
            [SignalEvent(code, 20240103, "DAY", "t")],
            hold=1,
            entry_lag=0,
            formal_ok=True,
            _skip_zero_replay=True,
        )
        assert False, "expected ValueError for entry_lag=0"
    except ValueError as e:
        assert "entry_lag" in str(e)


def test_week_month_default_excludes_unfinished_period():
    """With asof mid-period, open week/month bars are dropped (include_open=False)."""
    from datetime import date, timedelta

    dates = []
    d = date(2024, 1, 2)
    while d <= date(2024, 2, 15):
        if d.weekday() < 5:
            dates.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    day_bars = [DayBar(dt, 10, 11, 9, 10, 1.0, 1000) for dt in dates]
    asof = 20240210  # mid-February, month not closed

    weeks = aggregate_week(day_bars, asof=asof, include_open=False)
    months = aggregate_month(day_bars, asof=asof, include_open=False)
    assert weeks, "expect at least one closed week before asof"
    assert months, "expect at least January closed month"
    assert all(w.closed for w in weeks)
    assert all(m.closed for m in months)
    assert all(w.end_date <= asof for w in weeks)
    assert all(m.end_date <= asof for m in months)
    # February still open on asof -> must not appear as a final month bar
    feb_months = [m for m in months if (m.end_date // 100) == 202402]
    assert feb_months == []

    # build_period_bars mirrors aggregate_* defaults
    w2 = build_period_bars(day_bars, "WEEK", asof=asof, include_open=False)
    m2 = build_period_bars(day_bars, "MONTH", asof=asof, include_open=False)
    assert len(w2) == len(weeks)
    assert len(m2) == len(months)


def test_align_closed_state_no_unfinished_higher_bar():
    """Day d only sees higher-period bars with closed=True and end_date <= d."""
    day_dates = [20240105, 20240108, 20240109, 20240112]
    higher = [
        PeriodBar(20240105, 1, 1, 1, 1, 1, 1, 20240102, 20240105, 4, True),
        # unfinished week ending later — must not advance index early
        PeriodBar(20240112, 1, 1, 1, 1, 1, 1, 20240108, 20240112, 5, False),
    ]
    idx = align_closed_state(day_dates, higher)
    assert idx[0] == 0  # 1/5 can use first closed week
    assert idx[1] == 0  # 1/8 still only first (second not closed)
    assert idx[2] == 0
    assert idx[3] == 0  # even on end_date, closed=False blocks advance

    # Once marked closed, end_date day can use it
    higher2 = [
        PeriodBar(20240105, 1, 1, 1, 1, 1, 1, 20240102, 20240105, 4, True),
        PeriodBar(20240112, 1, 1, 1, 1, 1, 1, 20240108, 20240112, 5, True),
    ]
    idx2 = align_closed_state(day_dates, higher2)
    assert idx2[0] == 0
    assert idx2[1] == 0
    assert idx2[2] == 0
    assert idx2[3] == 1  # on 1/12 closed week becomes visible
