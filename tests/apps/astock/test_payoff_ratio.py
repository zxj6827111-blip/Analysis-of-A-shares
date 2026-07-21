# -*- coding: utf-8 -*-
"""payoff_ratio (盈亏比) and profit_factor from closed trades."""
from __future__ import annotations

from wtpy.apps.astock.config import CostConfig
from wtpy.apps.astock.strategy import EquityPoint, Fill, compute_metrics


def _curve(vals):
    return [EquityPoint(date=20240101 + i, cash=v, market_value=0.0, equity=v) for i, v in enumerate(vals)]


def test_payoff_and_profit_factor():
    # two round trips: win +100, loss -50 → payoff 2.0, profit factor 2.0
    fills = [
        Fill(date=1, std_code="A", side="BUY", price=10.0, shares=100, amount=1000, commission=0, stamp_tax=0, reason="e"),
        Fill(date=2, std_code="A", side="SELL", price=11.0, shares=100, amount=1100, commission=0, stamp_tax=0, reason="x"),
        Fill(date=3, std_code="A", side="BUY", price=10.0, shares=100, amount=1000, commission=0, stamp_tax=0, reason="e"),
        Fill(date=4, std_code="A", side="SELL", price=9.5, shares=100, amount=950, commission=0, stamp_tax=0, reason="x"),
    ]
    m = compute_metrics(_curve([1000, 1100, 1100, 1050]), init=1000.0, fills=fills, costs=CostConfig(0, 0, 0, 0))
    assert m["n_round_trips"] == 2
    assert abs(m["avg_win"] - 100.0) < 1e-9
    assert abs(m["avg_loss"] - 50.0) < 1e-9
    assert abs(m["payoff_ratio"] - 2.0) < 1e-9
    assert abs(m["profit_factor"] - 2.0) < 1e-9
    assert m["profit_loss_ratio"] == m["payoff_ratio"]


def test_payoff_none_when_no_losses():
    fills = [
        Fill(date=1, std_code="A", side="BUY", price=10.0, shares=100, amount=1000, commission=0, stamp_tax=0, reason="e"),
        Fill(date=2, std_code="A", side="SELL", price=11.0, shares=100, amount=1100, commission=0, stamp_tax=0, reason="x"),
    ]
    m = compute_metrics(_curve([1000, 1100]), init=1000.0, fills=fills, costs=CostConfig(0, 0, 0, 0))
    assert m["payoff_ratio"] is None
    assert m["profit_factor"] is None
