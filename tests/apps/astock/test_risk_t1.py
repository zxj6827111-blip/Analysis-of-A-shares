"""Same-day risk trigger + T+1 execution, deferral reason preservation."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import pytest

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester, validate_risk_pct
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def _bt(bars, dates):
    return PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)


def test_entry_day_stop_loss_sells_next_open():
    """Codex case: buy 10 on 1/4, low 9 same day -> mark SL; sell 1/5 open; no same-day SELL."""
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),  # signal
            DayBar(20240104, 10, 10.2, 9.0, 9.5, 1, 1000),  # buy open 10; low 9 hits 3%
            DayBar(20240105, 9.2, 9.5, 9.0, 9.3, 1, 1000),  # sell open
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and buys[0].date == 20240104
    assert len(sells) == 1 and sells[0].date == 20240105
    assert sells[0].reason == "stop_loss"
    # no same-day buy+sell
    assert not any(f.date == buys[0].date and f.side == "SELL" for f in res.fills)


def test_entry_day_take_profit_sells_next_open():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 11.0, 9.9, 10.5, 1, 1000),  # high 11 = +10% > 8%
            DayBar(20240105, 10.6, 10.8, 10.4, 10.7, 1, 1000),
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells[0].date == 20240105
    assert sells[0].reason == "take_profit"


def test_same_bar_sl_and_tp_stop_first():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            # buy 10; low 9 (-10%) and high 11 (+10%) same day
            DayBar(20240104, 10, 11.0, 9.0, 10.0, 1, 1000),
            DayBar(20240105, 10.0, 10.2, 9.8, 10.1, 1, 1000),
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells[0].reason == "stop_loss"
    assert res.config.get("risk_conflict_policy") == "stop_first"


def test_stop_then_limit_down_defers_preserves_reason():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105, 20240108]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 10.2, 9.0, 9.5, 1, 1000),  # buy + SL
            # limit-down untradeable board: open=low=high=close at floor from prev 9.5
            DayBar(20240105, 8.55, 8.55, 8.55, 8.55, 1, 1000),
            DayBar(20240108, 8.8, 9.0, 8.7, 8.9, 1, 1000),  # sellable
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240108
    assert sells[0].reason == "stop_loss_deferred_limit_down"


def test_take_profit_then_suspend_defers():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105, 20240108]
    # no bar on 20240105 => suspension
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 11.0, 9.9, 10.5, 1, 1000),  # buy + TP
            # 20240105 missing
            DayBar(20240108, 10.6, 10.8, 10.4, 10.7, 1, 1000),
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells[0].date == 20240108
    assert sells[0].reason == "take_profit_deferred_suspended"


def test_trigger_sticky_if_price_recovers():
    """After entry-day SL mark, next day price recovers — still sell for stop_loss."""
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 10.5, 9.0, 10.2, 1, 1000),  # SL on low, close recovers
            DayBar(20240105, 10.3, 10.5, 10.1, 10.4, 1, 1000),
        ]
    }
    res = _bt(bars, dates).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells[0].reason == "stop_loss"


def test_validate_risk_pct_api():
    assert validate_risk_pct("stop_loss_pct", 0.03) == 0.03
    assert validate_risk_pct("take_profit_pct", 0.08) == 0.08
    assert validate_risk_pct("x", None) is None
    with pytest.raises(ValueError):
        validate_risk_pct("stop_loss_pct", -0.03)
    with pytest.raises(ValueError):
        validate_risk_pct("stop_loss_pct", 0)
    with pytest.raises(ValueError):
        validate_risk_pct("stop_loss_pct", 1)
    with pytest.raises(ValueError):
        validate_risk_pct("take_profit_pct", 1.5)
    with pytest.raises(ValueError):
        _bt(
            {
                "SSE.STK.600000": [
                    DayBar(20240102, 10, 11, 9, 10, 1, 1),
                    DayBar(20240103, 10, 11, 9, 10, 1, 1),
                ]
            },
            [20240102, 20240103],
        ).run(
            [SignalEvent("SSE.STK.600000", 20240102, "DAY", "t")],
            hold=1,
            formal_ok=True,
            _skip_zero_replay=True,
            stop_loss_pct=-0.03,
        )
