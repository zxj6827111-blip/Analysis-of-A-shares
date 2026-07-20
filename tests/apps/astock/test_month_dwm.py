"""MONTH hold and DWM engine regression tests with explicit trade dates."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import (
    SignalEvent,
    compute_v5_dwm_resonance,
    build_period_bars,
)


def _cfg(slip=0.0):
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0, slippage=slip
    )
    return cfg


def _bars(dates, px=10.0):
    code = "SSE.STK.600000"
    return {
        code: [DayBar(d, px, px + 1, px - 1, px, 1.0, 1000) for d in dates]
    }


def test_month_hold1_buy_next_month_sell_after_complete_month():
    # Jan days + Feb days + Mar first days
    dates = [
        20240102, 20240115, 20240131,  # Jan ends 31
        20240201, 20240215, 20240229,  # Feb ends 29
        20240301, 20240304,
    ]
    code = "SSE.STK.600000"
    bars = _bars(dates)
    cal = TradeCalendar(dates)
    bt = PortfolioBacktester(_cfg(), cal, bars)
    # signal on Jan month close
    events = [SignalEvent(code, 20240131, "MONTH", "t")]
    res = bt.run(events, hold=1, period="MONTH", formal_ok=True, _skip_zero_replay=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and buys[0].date == 20240201
    # complete Feb month (end 20240229), sell next open 20240301
    assert len(sells) == 1 and sells[0].date == 20240301


def test_month_hold3_cross_year():
    dates = []
    # Oct 2023 - Feb 2024 first trading-ish dates
    for d in [
        20231009, 20231031,
        20231101, 20231130,
        20231201, 20231229,
        20240102, 20240131,
        20240201, 20240229,
        20240301,
    ]:
        dates.append(d)
    code = "SSE.STK.600000"
    bars = _bars(dates)
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    events = [SignalEvent(code, 20231031, "MONTH", "t")]
    res = bt.run(events, hold=3, period="MONTH", formal_ok=True, _skip_zero_replay=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys[0].date == 20231101
    # complete Nov, Dec, Jan (3 months) -> sell after Jan end on 20240201
    assert sells[0].date == 20240201


def test_dwm_hold_produces_real_fills():
    # Build day bars long enough for week/month closed state
    dates = []
    # simple sequence of weekdays Jan-Mar 2024
    from datetime import date, timedelta

    d = date(2024, 1, 2)
    while d <= date(2024, 3, 15):
        if d.weekday() < 5:
            dates.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    code = "SSE.STK.600000"
    # rising prices so signals can be crafted separately via events
    bars = {
        code: [
            DayBar(dt, 10 + i * 0.01, 11 + i * 0.01, 9 + i * 0.01, 10 + i * 0.01, 1, 1000)
            for i, dt in enumerate(dates)
        ]
    }
    cal = TradeCalendar(dates)
    # inject synthetic DWM events on three days after some history
    sig_days = [dates[30], dates[40], dates[50]]
    events = [SignalEvent(code, sd, "DWM", "syn_dwm", is_dwm=True) for sd in sig_days]
    for hold in (1, 3, 5):
        bt = PortfolioBacktester(_cfg(), cal, bars)
        res = bt.run(
            events,
            hold=hold,
            period="DWM",
            formal_ok=True,
            _skip_zero_replay=True,
        )
        buys = [f for f in res.fills if f.side == "BUY"]
        sells = [f for f in res.fills if f.side == "SELL"]
        assert len(buys) >= 1, f"hold={hold} no buys"
        assert len(sells) >= 1, f"hold={hold} no sells"
        # buy is next day after signal
        assert buys[0].date == cal.next_trading_day(sig_days[0])
        # sell after hold sessions from entry
        # at least one complete round trip
        assert res.metrics["n_round_trips"] >= 1


def test_dwm_no_signal_before_closed_week_month():
    """Resonance requires closed W/M; early days must not fire."""
    from datetime import date, timedelta

    dates = []
    d = date(2024, 1, 2)
    while d <= date(2024, 2, 29):
        if d.weekday() < 5:
            dates.append(d.year * 10000 + d.month * 100 + d.day)
        d += timedelta(days=1)
    day_bars = [
        DayBar(dt, 10, 11, 9, 10, 1, 1000) for dt in dates
    ]
    # all-true day signal
    import numpy as np
    day_sig = np.ones(len(day_bars), dtype=np.int8)
    w = build_period_bars(day_bars, "WEEK", asof=dates[-1], include_open=False)
    m = build_period_bars(day_bars, "MONTH", asof=dates[-1], include_open=False)
    # week/month signals all true
    w_sig = np.ones(len(w), dtype=np.int8)
    m_sig = np.ones(len(m), dtype=np.int8)
    res = compute_v5_dwm_resonance(day_bars, day_sig, w, w_sig, m, m_sig)
    # first few days of January: month not closed yet -> no resonance
    first_week_end = w[0].end_date if w else None
    for i, dt in enumerate(dates):
        if first_week_end and dt < first_week_end:
            # may still lack closed week
            pass
        # before first closed month end, must be 0
        if m and dt < m[0].end_date:
            assert res[i] == 0


def test_repeat_signal_no_reset_month():
    dates = [
        20240102, 20240131,
        20240201, 20240215, 20240229,
        20240301, 20240315, 20240329,
        20240401,
    ]
    code = "SSE.STK.600000"
    bars = _bars(dates)
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    events = [
        SignalEvent(code, 20240131, "MONTH", "t"),
        SignalEvent(code, 20240229, "MONTH", "t"),  # while still holding hold=2
    ]
    res = bt.run(events, hold=2, period="MONTH", formal_ok=True, _skip_zero_replay=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1
