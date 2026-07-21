# -*- coding: utf-8 -*-
"""buy_weekday / exit_weekday scheduling."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester, parse_single_weekday
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_parse_single_weekday():
    assert parse_single_weekday(5) == 5
    assert parse_single_weekday("fri") == 5
    assert parse_single_weekday(None) is None


def test_calendar_next_weekday():
    # 20240103 Wed, 04 Thu, 05 Fri, 08 Mon, 09 Tue
    cal = TradeCalendar([20240103, 20240104, 20240105, 20240108, 20240109])
    assert cal.next_weekday_trading_day(20240103, 5, strict=True) == 20240105  # Fri
    assert cal.next_weekday_trading_day(20240105, 1, strict=True) == 20240108  # Mon
    assert cal.next_weekday_trading_day(20240103, 3, strict=True) == 20240110 if False else (
        cal.next_weekday_trading_day(20240103, 3, strict=True)  # next Wed — none in list
    ) is None


def test_buy_monday_exit_friday():
    """Signal Wed → buy next Mon open → exit next Fri open."""
    code = "SSE.STK.600000"
    # Wed 1/3 signal, Mon 1/8 buy, Fri 1/12 exit
    dates = [20240103, 20240104, 20240105, 20240108, 20240109, 20240110, 20240111, 20240112]
    bars = {
        code: [
            DayBar(d, 10 + i * 0.1, 11, 9, 10.5, 1, 1000) for i, d in enumerate(dates)
        ]
    }
    # set distinct open prices
    bars[code][3] = DayBar(20240108, 12.0, 13, 11, 12.5, 1, 1000)  # Mon buy open 12
    bars[code][7] = DayBar(20240112, 14.0, 15, 13, 14.5, 1, 1000)  # Fri sell open 14

    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],  # Wednesday
        hold=1,
        entry_lag=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        buy_weekday=1,  # Monday
        exit_weekday=5,  # Friday
        buy_on="open",
        sell_on="open",
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and buys[0].date == 20240108
    assert abs(buys[0].price - 12.0) < 1e-9
    assert len(sells) == 1 and sells[0].date == 20240112
    assert abs(sells[0].price - 14.0) < 1e-9
