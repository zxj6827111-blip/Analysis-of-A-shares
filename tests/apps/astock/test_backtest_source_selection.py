"""Tests for BacktestRequest multi-source fields."""
import pytest

from wtpy.apps.astock.service.backtest_request import BacktestRequest


class TestBacktestSourceSelection:
    def test_default_fields(self):
        req = BacktestRequest(rule_ids=["rule_a"])
        assert req.signal_data_source is None
        assert req.signal_adjustment is None
        assert req.dataset_id is None
        assert req.weekly_bar_mode == "local_aggregate"
        assert req.execution_data_source == "local_vendor"
        assert req.execution_dataset_id is None

    def test_tdxquant_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            signal_adjustment="front",
            dataset_id="tdxquant_front_1d_20260724_a3f2b1c4d5e6",
        )
        assert req.signal_data_source == "tdxquant"
        assert req.signal_adjustment == "front"
        assert req.dataset_id == "tdxquant_front_1d_20260724_a3f2b1c4d5e6"

    def test_tushare_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tushare",
            signal_adjustment="qfq",
            dataset_id="tushare_qfq_1d_anchor20260724_7f8e9d0c1b2a",
        )
        assert req.signal_data_source == "tushare"
        assert req.signal_adjustment == "qfq"

    def test_internal_source(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="internal",
            signal_adjustment="asof_qfq",
        )
        assert req.signal_data_source == "internal"
        assert req.signal_adjustment == "asof_qfq"

    def test_execution_default_local_vendor(self):
        req = BacktestRequest(rule_ids=["rule_a"])
        assert req.execution_data_source == "local_vendor"

    def test_to_dict_includes_new_fields(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            dataset_id="ds_123",
            weekly_bar_mode="vendor_native",
        )
        d = req.to_dict()
        assert d["signal_data_source"] == "tdxquant"
        assert d["dataset_id"] == "ds_123"
        assert d["weekly_bar_mode"] == "vendor_native"
        assert d["execution_data_source"] == "local_vendor"

    def test_vendor_native_weekly_mode(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            weekly_bar_mode="vendor_native",
        )
        assert req.weekly_bar_mode == "vendor_native"
