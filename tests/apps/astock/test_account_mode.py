# -*- coding: utf-8 -*-
"""Portfolio vs per-symbol (TDX-style) account modes."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 100_000
    cfg.max_weight = 0.1
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def _bars_two():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    def series(code, base):
        return [
            DayBar(d, base, base + 1, base - 1, base + 0.5, 1, 1000) for d in dates
        ]
    return {
        "SSE.STK.600000": series("SSE.STK.600000", 10.0),
        "SSE.STK.600001": series("SSE.STK.600001", 20.0),
    }, dates


def test_per_symbol_can_buy_both_same_day_portfolio_may_not():
    bars, dates = _bars_two()
    cal = TradeCalendar(dates)
    # two signals same day — portfolio 10% may buy both small; use tiny capital to stress
    cfg = _cfg()
    cfg.initial_capital = 50_000  # with 10% only 5k each -> may fail 600001 lot at 20
    cfg.max_weight = 0.1
    events = [
        SignalEvent("SSE.STK.600000", 20240102, "DAY", "t"),
        SignalEvent("SSE.STK.600001", 20240102, "DAY", "t"),
    ]
    port = PortfolioBacktester(cfg, cal, bars).run(
        events, hold=2, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="portfolio",
    )
    per = PortfolioBacktester(cfg, cal, bars).run(
        events, hold=2, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="per_symbol",
    )
    pb = [f for f in port.fills if f.side == "BUY"]
    qb = [f for f in per.fills if f.side == "BUY"]
    # per-symbol should fund each book with full 50k and buy both
    assert len(qb) == 2
    assert per.metrics.get("account_mode") == "per_symbol"
    assert per.metrics.get("n_symbol_accounts") == 2
    assert "mean_symbol_return" in per.metrics
    # portfolio may buy fewer when capital tight
    assert len(pb) <= 2


def test_per_symbol_mean_return_defined():
    bars, dates = _bars_two()
    events = [SignalEvent("SSE.STK.600000", 20240102, "DAY", "t")]
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        events, hold=1, period="DAY", formal_ok=True, _skip_zero_replay=True,
        account_mode="per_symbol",
    )
    assert res.metrics.get("mean_symbol_return") is not None
