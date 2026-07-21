# -*- coding: utf-8 -*-
"""buy_on / sell_on: open vs close session prices."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester, parse_price_session
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_parse_price_session():
    assert parse_price_session("open") == "open"
    assert parse_price_session("close") == "close"
    assert parse_price_session("收盘") == "close"
    assert parse_price_session(None) == "open"


def test_buy_close_uses_close_price():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),  # signal
            DayBar(20240104, 10.0, 11.0, 9.9, 10.7, 1, 1000),  # buy: open 10 / close 10.7
            DayBar(20240105, 10.8, 11.2, 10.5, 11.0, 1, 1000),  # sell open 10.8
        ]
    }
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        buy_on="close",
        sell_on="open",
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert abs(buys[0].price - 10.7) < 1e-9


def test_sell_close_uses_close_price():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),
            DayBar(20240104, 10.0, 11.0, 9.9, 10.5, 1, 1000),  # buy open 10
            DayBar(20240105, 10.8, 11.2, 10.5, 11.0, 1, 1000),  # sell close 11 not open 10.8
        ]
    }
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        buy_on="open",
        sell_on="close",
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert abs(sells[0].price - 11.0) < 1e-9


def test_default_open_buy_open_sell():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),
            DayBar(20240104, 10.0, 11.0, 9.9, 10.5, 1, 1000),
            DayBar(20240105, 10.8, 11.2, 10.5, 11.0, 1, 1000),
        ]
    }
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert abs(buys[0].price - 10.0) < 1e-9
    assert abs(sells[0].price - 10.8) < 1e-9
