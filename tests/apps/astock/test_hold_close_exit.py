# -*- coding: utf-8 -*-
"""Time-stop (hold) exits at close; risk exits still at open."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_hold1_sells_at_close_not_open():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),  # signal
            DayBar(20240104, 10.0, 11.0, 9.9, 10.5, 1, 1000),  # buy open 10
            DayBar(20240105, 10.8, 11.2, 10.5, 11.0, 1, 1000),  # hold exit: close 11 not open 10.8
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240105
    assert abs(sells[0].price - 11.0) < 1e-9
    assert sells[0].reason == "hold_expired"


def test_stop_loss_still_sells_at_open():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 10.2, 9.0, 9.5, 1, 1000),  # buy 10; low hits 3%
            DayBar(20240105, 9.2, 9.5, 9.0, 9.3, 1, 1000),  # sell open 9.2
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240105
    assert abs(sells[0].price - 9.2) < 1e-9
    assert "stop" in (sells[0].reason or "")
