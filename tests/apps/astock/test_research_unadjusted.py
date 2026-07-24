"""Research-unadjusted must force raw prices even when factors exist."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import numpy as np

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent, day_bars_to_adj


def test_research_unadjusted_uses_raw_not_adj_fills():
    """Construct bars where adj prices differ sharply from raw."""
    dates = [20240102, 20240103, 20240104, 20240105]
    code = "SSE.STK.600000"
    raw = [
        DayBar(20240102, 10, 11, 9, 10, 1, 1000),
        DayBar(20240103, 10, 11, 9, 10, 1, 1000),  # signal
        DayBar(20240104, 20, 21, 19, 20, 1, 1000),  # buy open 20 raw
        DayBar(20240105, 22, 23, 21, 22, 1, 1000),  # sell open 22 raw
    ]
    # Causal scale base=first factor. Event mid-series: f drops to 0.5 on buy day.
    # scale = [1,1,0.5,0.5] => adj buy open = 20*0.5 = 10 (raw remains 20).
    fac = np.array([1.0, 1.0, 0.5, 0.5])
    adj = day_bars_to_adj(raw, fac)
    assert abs(adj[2].open - 10.0) < 1e-9  # adjusted buy open
    assert abs(raw[2].open - 20.0) < 1e-9

    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0.0, 0.0, 0.0, 0.0)

    events = [SignalEvent(code, 20240103, "DAY", "t")]
    cal = TradeCalendar(dates)

    # dual_price_v1: bars_by_code is always RAW execution; adj is audit only.
    # Formal and research_unadjusted both fill at raw open=20 on buy day.
    bt_formal = PortfolioBacktester(cfg, cal, {code: raw}, adj_bars_by_code={code: adj})
    res_formal = bt_formal.run(
        events, hold=1, period="DAY", formal_ok=True, _skip_zero_replay=True
    )
    buy_formal = [f for f in res_formal.fills if f.side == "BUY"][0]

    bt_raw = PortfolioBacktester(cfg, cal, {code: raw}, adj_bars_by_code={code: adj})
    res_raw = bt_raw.run(
        events,
        hold=1,
        period="DAY",
        formal_ok=True,
        research_unadjusted=True,
        _skip_zero_replay=True,
    )
    buy_raw = [f for f in res_raw.fills if f.side == "BUY"][0]

    assert abs(buy_formal.price - 20.0) < 1e-6  # raw execution (not adj 10)
    assert abs(buy_raw.price - 20.0) < 1e-6
    # adjusted reference should still be available on formal path
    assert buy_formal.adjusted_reference_price is None or abs(
        float(buy_formal.adjusted_reference_price) - 10.0
    ) < 1e-6
    assert res_raw.status == "research_unadjusted"
