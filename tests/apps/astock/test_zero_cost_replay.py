"""Zero-cost full replay control tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def test_zero_cost_full_replay_changes_shares_when_costs_bite():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    code = "SSE.STK.600000"
    bars = {code: [DayBar(d, 50, 51, 49, 50, 1, 1000) for d in dates]}
    cfg = AStockConfig()
    cfg.initial_capital = 10_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=0.001, min_commission=50.0, stamp_tax_rate=0.001, slippage=0.01
    )
    events = [SignalEvent(code, 20240102, "DAY", "t")]
    cal = TradeCalendar(dates)

    bt_cost = PortfolioBacktester(cfg, cal, bars)
    res_cost = bt_cost.run(events, hold=1, period="DAY", formal_ok=True)
    assert res_cost.metrics.get("control_method") == "full_replay"
    assert "zero_cost_return" in res_cost.metrics

    # explicit zero-cost replay for share comparison
    zcfg = AStockConfig()
    zcfg.initial_capital = cfg.initial_capital
    zcfg.max_weight = cfg.max_weight
    zcfg.lot_size = cfg.lot_size
    zcfg.costs = CostConfig(0.0, 0.0, 0.0, 0.0, note="zero")
    bt_zero = PortfolioBacktester(zcfg, cal, bars)
    res_zero = bt_zero.run(
        events, hold=1, period="DAY", formal_ok=True, _skip_zero_replay=True
    )

    cost_buys = [f for f in res_cost.fills if f.side == "BUY"]
    zero_buys = [f for f in res_zero.fills if f.side == "BUY"]
    assert cost_buys and zero_buys
    # With min commission 50 on 10k capital and 1% slip, costed run can afford fewer lots
    assert zero_buys[0].shares >= cost_buys[0].shares
    assert zero_buys[0].shares != cost_buys[0].shares or res_cost.metrics["cost_total"] > 0
    # Prefer strict share inequality when capital is tight
    assert zero_buys[0].shares > cost_buys[0].shares
    assert res_cost.metrics["zero_cost_return"] >= res_cost.metrics["total_return"] - 1e-9
