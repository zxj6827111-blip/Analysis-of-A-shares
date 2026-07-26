"""Tests for dual-source experiment variant generation."""
import pytest

from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    DataSource,
    SIGNAL_SOURCE_ADJUSTMENT,
)
from wtpy.apps.astock.service.backtest_request import BacktestRequest


class TestDualSourceVariants:
    def test_dual_source_generates_two_variants(self):
        base_params = {
            "rule_ids": ["rule_a"],
            "period": "DAY",
            "hold": 5,
            "start": 20200101,
            "end": 20240701,
        }
        sources = [
            (DataSource.TDXQUANT, AdjustmentMode.FRONT),
            (DataSource.TUSHARE, AdjustmentMode.QFQ),
        ]
        variants = []
        for src, adj in sources:
            variants.append({
                **base_params,
                "signal_data_source": src.value,
                "signal_adjustment": adj.value,
            })
        assert len(variants) == 2
        assert variants[0]["signal_data_source"] == "tdxquant"
        assert variants[1]["signal_data_source"] == "tushare"

    def test_dual_source_same_other_params(self):
        base = {
            "rule_ids": ["rule_a"],
            "period": "WEEK",
            "hold": 3,
            "stop_loss": 0.05,
            "take_profit": 0.10,
            "account_mode": "portfolio",
        }
        v1 = BacktestRequest(
            **base,
            signal_data_source="tdxquant",
            signal_adjustment="front",
        )
        v2 = BacktestRequest(
            **base,
            signal_data_source="tushare",
            signal_adjustment="qfq",
        )
        assert v1.rule_ids == v2.rule_ids
        assert v1.period == v2.period
        assert v1.hold == v2.hold
        assert v1.stop_loss == v2.stop_loss
        assert v1.take_profit == v2.take_profit
        assert v1.account_mode == v2.account_mode
        assert v1.execution_data_source == v2.execution_data_source

    def test_dual_source_different_signal_source_only(self):
        v1 = BacktestRequest(
            rule_ids=["r"], signal_data_source="tdxquant", signal_adjustment="front",
        )
        v2 = BacktestRequest(
            rule_ids=["r"], signal_data_source="tushare", signal_adjustment="qfq",
        )
        assert v1.signal_data_source != v2.signal_data_source
        assert v1.signal_adjustment != v2.signal_adjustment
        assert v1.execution_data_source == v2.execution_data_source

    def test_signal_source_adjustment_mapping(self):
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.TDXQUANT] == AdjustmentMode.FRONT
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.TUSHARE] == AdjustmentMode.QFQ
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.INTERNAL] == AdjustmentMode.ASOF_QFQ

    def test_single_source_no_duplicate(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            signal_adjustment="front",
        )
        assert req.signal_data_source == "tdxquant"
        assert req.signal_adjustment == "front"
