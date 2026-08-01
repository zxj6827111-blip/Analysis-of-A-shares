# -*- coding: utf-8 -*-
"""Explicit corporate-action ledger integration tests."""

from __future__ import annotations

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.ca_ledger import (
    CA_CASH_DIVIDEND,
    CA_SHARE_RATIO,
    CorporateActionEvent,
)
from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


CODE = "SSE.STK.600000"
DATES = [20240102, 20240103, 20240104, 20240105]


def _bar(day: int, price: float) -> DayBar:
    return DayBar(
        date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        amount=10_000_000.0,
        volume=1_000_000.0,
    )


def _cfg(tmp_path) -> AStockConfig:
    cfg = AStockConfig(
        project_root=tmp_path,
        storage_root=tmp_path / "storage",
        output_root=tmp_path / "outputs",
        indicator_dir=tmp_path / "indicators",
    )
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
        slippage=0.0,
        note="explicit-ca-test",
    )
    return cfg


def _run(tmp_path, *, post_event_price: float, event=None):
    bars = {
        CODE: [
            _bar(20240102, 10.0),
            _bar(20240103, 10.0),
            _bar(20240104, post_event_price),
            _bar(20240105, post_event_price),
        ]
    }
    factors = {
        CODE: {
            20240102: 1.0,
            20240103: 1.0,
            20240104: 2.0,
            20240105: 2.0,
        }
    }
    events_by_code = {CODE: [event]} if event is not None else None
    result = PortfolioBacktester(
        _cfg(tmp_path),
        TradeCalendar(DATES),
        bars,
        factor_by_code=factors,
        ca_events_by_code=events_by_code,
        corporate_action_policy="event_ledger" if event is not None else "fail_closed",
    ).run(
        [SignalEvent(CODE, 20240102, "DAY", "test")],
        hold=2,
        entry_lag=1,
        start=20240102,
        end=20240105,
        formal_ok=True,
        run_id="explicit_ca_test",
    )
    return result


def test_cash_dividend_credits_cash_and_rebases_factor(tmp_path):
    result = _run(
        tmp_path,
        post_event_price=9.0,
        event=CorporateActionEvent(
            std_code=CODE,
            date=20240104,
            event_type=CA_CASH_DIVIDEND,
            cash_per_share=1.0,
            source="tushare_dividend",
        ),
    )

    assert result.status == "ok"
    assert result.metrics["corporate_action_policy"] == "event_ledger"
    assert result.metrics["n_corporate_actions"] == 1
    assert result.metrics["total_return"] == pytest.approx(0.0)
    sell = next(fill for fill in result.fills if fill.side == "SELL")
    assert sell.shares == 1000
    assert sell.corporate_action_cash_received == pytest.approx(1000.0)
    assert sell.position_cost_basis == pytest.approx(10_000.0)
    assert any("cash_div" in note for note in result.notes)


def test_share_ratio_updates_shares_and_preserves_total_cost_basis(tmp_path):
    result = _run(
        tmp_path,
        post_event_price=5.0,
        event=CorporateActionEvent(
            std_code=CODE,
            date=20240104,
            event_type=CA_SHARE_RATIO,
            share_multiplier=2.0,
            source="tushare_dividend",
        ),
    )

    assert result.status == "ok"
    assert result.metrics["n_corporate_actions"] == 1
    assert result.metrics["total_return"] == pytest.approx(0.0)
    assert result.metrics["gross_profit"] == pytest.approx(0.0)
    assert result.metrics["gross_loss"] == pytest.approx(0.0)
    sell = next(fill for fill in result.fills if fill.side == "SELL")
    assert sell.shares == 2000
    assert sell.price == pytest.approx(5.0)
    assert sell.position_cost_basis == pytest.approx(10_000.0)
    assert any("share_ratio" in note for note in result.notes)


def test_factor_jump_without_explicit_event_remains_fail_closed(tmp_path):
    result = _run(tmp_path, post_event_price=5.0, event=None)

    assert result.status == "unsupported_corporate_action"
    assert result.metrics["n_corporate_actions"] == 0
    assert any("factor changed while open" in note for note in result.notes)