"""Tests that execution cache keys are isolated across datasets."""
import pytest

from wtpy.apps.astock.research.execution_cache import execution_cache_key


class TestExecutionCacheDatasetIsolation:
    def _base_payload(self):
        return {
            "engine": "full",
            "engine_result_version": "v3",
            "signal_price_mode": "asof_forward_qfq",
            "execution_price_mode": "raw",
            "rule_ids": ["rule_a"],
            "period": "DAY",
            "hold": 5,
            "start": 20200101,
            "end": 20240701,
            "universe": "univ_hash",
            "costs": {"commission": 0.0003},
        }

    def test_different_signal_dataset_different_key(self):
        p1 = {**self._base_payload(), "signal_dataset_id": "ds_tdx_1"}
        p2 = {**self._base_payload(), "signal_dataset_id": "ds_tdx_2"}
        assert execution_cache_key(p1) != execution_cache_key(p2)

    def test_different_execution_dataset_different_key(self):
        p1 = {**self._base_payload(), "execution_dataset_id": "exec_1"}
        p2 = {**self._base_payload(), "execution_dataset_id": "exec_2"}
        assert execution_cache_key(p1) != execution_cache_key(p2)

    def test_different_signal_source_different_key(self):
        p1 = {**self._base_payload(), "signal_data_source": "tdxquant"}
        p2 = {**self._base_payload(), "signal_data_source": "tushare"}
        assert execution_cache_key(p1) != execution_cache_key(p2)

    def test_weekly_bar_mode_affects_key(self):
        p1 = {**self._base_payload(), "weekly_bar_mode": "local_aggregate"}
        p2 = {**self._base_payload(), "weekly_bar_mode": "vendor_native"}
        assert execution_cache_key(p1) != execution_cache_key(p2)

    def test_same_payload_same_key(self):
        p = {**self._base_payload(), "signal_dataset_id": "ds_1", "signal_data_source": "tdxquant"}
        assert execution_cache_key(p) == execution_cache_key(dict(p))

    def test_legacy_payload_without_new_fields(self):
        k = execution_cache_key(self._base_payload())
        assert isinstance(k, str)
        assert len(k) == 32
