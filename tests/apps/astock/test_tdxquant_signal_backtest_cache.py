# -*- coding: utf-8 -*-
"""Gate C phase 2: tdxquant signal source — cache isolation + no legacy fallback."""
from __future__ import annotations

import pytest

from wtpy.apps.astock.research.signal_cache import signal_cache_key


def _base(**over):
    kw = dict(
        indicator_ids=["ind_735"],
        indicator_source_hash="srchash",
        period="DAY",
        start=20240101,
        end=20240701,
        universe_hash="univ_hash",
        adjust_mode="qfq",
        weekly_bar_mode="local_aggregate",
        execution_data_source="local_vendor",
        execution_dataset_id="localvendor_none_1d_exec001",
    )
    kw.update(over)
    return kw


TDX = dict(data_source="tdxquant", adjustment="front",
           dataset_id="tdxquant_front_1d_20260726_aaa")
INTERNAL = dict(data_source="internal", adjustment="tushare_factor_qfq",
                dataset_id="internal_tsfqfq_1d_20260717_bbb",
                raw_parent_dataset_id="localvendor_none_1d_raw001",
                factor_parent_dataset_id="tushare_adjfactor_1d_fac001",
                formula_version="tsqfq_v1",
                anchor_policy="last_factor_on_or_before_cutoff")


class TestTdxquantCacheIsolation:
    def test_tdxquant_vs_internal_signal_source_differ(self):
        assert signal_cache_key(**_base(**TDX)) != signal_cache_key(**_base(**INTERNAL))

    def test_two_tdxquant_datasets_differ(self):
        k1 = signal_cache_key(**_base(**TDX))
        k2 = signal_cache_key(
            **_base(**{**TDX, "dataset_id": "tdxquant_front_1d_20260801_ccc"}))
        assert k1 != k2

    def test_same_full_config_stable(self):
        assert signal_cache_key(**_base(**TDX)) == signal_cache_key(**_base(**TDX))

    def test_execution_dataset_participates(self):
        k1 = signal_cache_key(**_base(**TDX))
        k2 = signal_cache_key(
            **{**_base(**TDX), "execution_dataset_id": "localvendor_none_1d_exec002"})
        assert k1 != k2

    def test_legacy_key_unaffected_by_tdx_fields(self):
        legacy = _base()
        legacy.pop("execution_data_source")
        legacy.pop("execution_dataset_id")
        k_legacy = signal_cache_key(**legacy)
        assert k_legacy != signal_cache_key(**_base(**TDX))


class TestNoLegacyFallback:
    def test_missing_tdxquant_dataset_raises_not_fallback(self, tmp_path):
        """Repository resolution for tdxquant/front with no ready dataset must
        raise (backtest layer maps this to HTTP 400) — never legacy fallback."""
        from wtpy.apps.astock.data.dataset_store import DatasetStore
        from wtpy.apps.astock.data.repository import (
            MarketDataRepository, DatasetNotFoundError)
        repo = MarketDataRepository(DatasetStore(tmp_path / "md"))
        with pytest.raises(DatasetNotFoundError):
            repo.resolve_latest_ready(source="tdxquant", adjustment="front",
                                      period="1d")

    def test_backtest_signal_source_set_includes_tdxquant(self):
        """Wiring guard: repository L1 path must trigger for tdxquant."""
        import inspect
        from wtpy.apps.astock.service import backtest as bt
        src = inspect.getsource(bt)
        assert '"tdxquant", "tushare", "internal"' in src
