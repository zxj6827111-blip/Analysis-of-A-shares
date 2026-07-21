# -*- coding: utf-8 -*-
"""Phase-1: 735-style schedule smoke (synthetic prices, not full market data).

Full 16-group formal matrix is Phase-2; here we lock schedule + reason codes for:
- Friday signal → Monday open buy
- exit Tue/Wed/Thu/Fri × open/close (subset)
- weekday_exit reason
"""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import EXIT_REASON_WEEKDAY_EXIT, PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def _calendar_two_weeks():
    # Fri 1/5 signal week; Mon 1/8 .. Fri 1/12
    return [
        20240105,  # Fri
        20240108,
        20240109,
        20240110,
        20240111,
        20240112,
        20240115,
        20240116,
    ]


def test_735_schedule_mon_buy_exit_weekdays_open():
    code = "SSE.STK.600000"
    dates = _calendar_two_weeks()
    bars = {
        code: [
            DayBar(d, 10.0 + i * 0.2, 12, 9, 10.5, 1, 1000) for i, d in enumerate(dates)
        ]
    }
    # Mon buy open
    bars[code][1] = DayBar(20240108, 11.0, 12, 10, 11.5, 1, 1000)

    cal = TradeCalendar(dates)
    bt = PortfolioBacktester(_cfg(), cal, bars)

    for exit_wd, expect_exit in [
        (2, 20240109),  # Tue
        (3, 20240110),  # Wed
        (4, 20240111),  # Thu
        (5, 20240112),  # Fri
    ]:
        res = bt.run(
            [SignalEvent(code, 20240105, "DAY", "735")],
            hold=1,
            entry_lag=1,
            period="DAY",
            formal_ok=True,
            _skip_zero_replay=True,
            buy_weekday=1,
            exit_weekday=exit_wd,
            buy_on="open",
            sell_on="open",
            signal_weekdays=[5],
        )
        buys = [f for f in res.fills if f.side == "BUY"]
        sells = [f for f in res.fills if f.side == "SELL"]
        assert len(buys) == 1 and buys[0].date == 20240108, exit_wd
        assert len(sells) == 1 and sells[0].date == expect_exit, (exit_wd, sells)
        assert sells[0].reason == EXIT_REASON_WEEKDAY_EXIT
        assert abs(buys[0].price - 11.0) < 1e-9


def test_735_schedule_exit_close_session():
    code = "SSE.STK.600000"
    dates = _calendar_two_weeks()
    bars = {
        code: [DayBar(d, 10.0, 12, 9, 10.5, 1, 1000) for d in dates]
    }
    bars[code][1] = DayBar(20240108, 11.0, 12, 10, 11.2, 1, 1000)
    bars[code][3] = DayBar(20240110, 13.0, 14, 12, 13.5, 1, 1000)  # Wed close sell

    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240105, "DAY", "735")],
        hold=1,
        entry_lag=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        buy_weekday=1,
        exit_weekday=3,
        buy_on="open",
        sell_on="close",
        signal_weekdays=[5],
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240110
    assert abs(sells[0].price - 13.5) < 1e-9
    assert sells[0].reason == EXIT_REASON_WEEKDAY_EXIT
