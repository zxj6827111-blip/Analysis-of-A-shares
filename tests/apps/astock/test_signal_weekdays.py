# -*- coding: utf-8 -*-
"""signal_weekdays: only signals on selected ISO weekdays are tradable."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import (
    PortfolioBacktester,
    format_signal_weekdays,
    parse_signal_weekdays,
    yyyymmdd_isoweekday,
)
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_parse_signal_weekdays():
    assert parse_signal_weekdays(None) is None
    assert parse_signal_weekdays([]) is None
    assert parse_signal_weekdays("all") is None
    assert parse_signal_weekdays(5) == [5]
    assert parse_signal_weekdays("5") == [5]
    assert parse_signal_weekdays("fri") == [5]
    assert parse_signal_weekdays("五") == [5]
    assert parse_signal_weekdays("1,3,5") == [1, 3, 5]
    assert parse_signal_weekdays(["周五", 1]) == [1, 5]
    assert format_signal_weekdays([5]) == "周五"


def test_yyyymmdd_isoweekday_friday():
    # 2024-01-05 was Friday
    assert yyyymmdd_isoweekday(20240105) == 5
    # 2024-01-03 was Wednesday
    assert yyyymmdd_isoweekday(20240103) == 3


def test_friday_only_filters_wednesday_signal():
    """Wed signal ignored when only Friday allowed; Fri signal trades."""
    code = "SSE.STK.600000"
    # Wed 1/3, Thu 1/4, Fri 1/5, Mon 1/8, Tue 1/9
    dates = [20240103, 20240104, 20240105, 20240108, 20240109]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),
            DayBar(20240104, 10.0, 11.0, 9.9, 10.5, 1, 1000),
            DayBar(20240105, 10.2, 10.8, 10.0, 10.4, 1, 1000),  # Friday signal
            DayBar(20240108, 10.4, 11.0, 10.2, 10.6, 1, 1000),  # buy open
            DayBar(20240109, 10.6, 11.2, 10.4, 10.8, 1, 1000),  # sell open hold=1
        ]
    }
    cal = TradeCalendar(dates)
    bt = PortfolioBacktester(_cfg(), cal, bars)

    # Wednesday-only signal should produce no trades when filter=Friday
    res_wed = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        signal_weekdays=[5],
    )
    assert res_wed.fills == [] or all(f.side != "BUY" for f in res_wed.fills)

    # Friday signal should trade
    res_fri = bt.run(
        [SignalEvent(code, 20240105, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        signal_weekdays=[5],
    )
    buys = [f for f in res_fri.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].date == 20240108

    # no filter: Wednesday signal still trades
    res_all = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        signal_weekdays=None,
    )
    buys_all = [f for f in res_all.fills if f.side == "BUY"]
    assert len(buys_all) == 1
    assert buys_all[0].date == 20240104


def test_api_body_accepts_signal_weekdays():
    from wtpy.apps.astock.api import BacktestBody

    body = BacktestBody(rule_ids=["x"], signal_weekdays=[5])
    assert body.signal_weekdays == [5]
