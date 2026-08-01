"""Tests that signal cache keys are isolated across data sources."""
import pytest

from wtpy.apps.astock.research.signal_cache import signal_cache_key


class TestSignalCacheSourceIsolation:
    def _base_kwargs(self):
        return dict(
            indicator_ids=["rule_a"],
            period="DAY",
            start=20200101,
            end=20240701,
            universe_hash="univ_abc",
            adjust_mode="asof_forward_qfq",
        )

    def test_tdxquant_vs_tushare_different_keys(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            adjustment="front",
            dataset_id="tdxquant_front_1d_20260724_a3f2b1c4d5e6",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tushare",
            adjustment="qfq",
            dataset_id="tushare_qfq_1d_20260724_7f8e9d0c1b2a",
        )
        assert k1 != k2

    def test_same_source_same_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            adjustment="front",
            dataset_id="ds_1",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            adjustment="front",
            dataset_id="ds_1",
        )
        assert k1 == k2

    def test_different_dataset_id_different_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            dataset_id="ds_old",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            dataset_id="ds_new",
        )
        assert k1 != k2

    def test_weekly_bar_mode_affects_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            weekly_bar_mode="local_aggregate",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tdxquant",
            weekly_bar_mode="vendor_native",
        )
        assert k1 != k2

    def test_anchor_date_affects_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tushare",
            anchor_date=20260720,
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            data_source="tushare",
            anchor_date=20260724,
        )
        assert k1 != k2

    def test_execution_data_source_affects_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            execution_data_source="tdx_local",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            execution_data_source="other",
        )
        assert k1 != k2

    def test_legacy_empty_source_still_works(self):
        k = signal_cache_key(**self._base_kwargs())
        assert isinstance(k, str)
        assert len(k) == 32

    def test_universe_version_affects_key(self):
        k1 = signal_cache_key(
            **self._base_kwargs(),
            universe_version="v1",
        )
        k2 = signal_cache_key(
            **self._base_kwargs(),
            universe_version="v2",
        )
        assert k1 != k2
