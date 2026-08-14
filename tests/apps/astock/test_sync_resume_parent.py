# -*- coding: utf-8 -*-
"""Regression: the stock-chain incremental resume must never pick an
index/ETF-only dataset as its parent.

A pure index/ETF dataset (published by the ``--asset-class index|etf|all``
sync chain) shares the ``tushare/none/1d`` scope with the full-market stock
dataset. Both pass the "enough rows" heuristic, so the stock chain used to
merge the ETF parent's history into stock blobs — which matched no symbols
and silently orphaned the daily windows. The formal L1/L2 product chain then
rejected every orphaned window, freezing the composite surfaces at the last
full-history base.

The fix excludes index/ETF-only datasets structurally (``universe_type`` tag)
and by symbol-majority fallback for historical datasets, mirroring the
existing ``_infer_index_etf_parent`` filter.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.tushare_product import (
    INDEX_ETF_UNIVERSE_TYPE,
    _is_tushare_raw_base_candidate,
)

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

_MODULE = None


def _script():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "sync_resume_parent_test", SYNC_SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def _calendar_dates(start_ymd: int, n: int) -> np.ndarray:
    import datetime as _dt
    start = _dt.datetime.strptime(str(start_ymd), "%Y%m%d").date()
    return np.array(
        [int((start + _dt.timedelta(days=i)).strftime("%Y%m%d"))
         for i in range(n)],
        dtype=np.int64,
    )


def _publish_dataset(
    store: DatasetStore,
    symbols: list,
    *,
    dataset_id: str,
    cutoff: int,
    rows_per_symbol: int = 600,
    universe_type: str = "",
) -> DatasetManifest:
    """Publish a ready tushare/none/1d dataset with per-symbol history.

    rows_per_symbol defaults well above the resume ``require_rows`` (500) so
    every published dataset passes the rows heuristic — the regression only
    fires on the instrument-kind filter, not on the rows gate.
    """
    records = []
    for sym in symbols:
        dates = _calendar_dates(20250101, rows_per_symbol)
        sha = store.store_factors(sym, dates, np.full(rows_per_symbol, 1.0))
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=rows_per_symbol, quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="none",
        period="1d",
        status="ready",
        dataset_type="bars",
        symbols=records,
        symbol_count=len(records),
        row_count=sum(r.row_count for r in records),
        data_cutoff_date=cutoff,
        universe_type=universe_type,
    )
    store.publish(m)
    return m


class TestResumeParentIndexEtfExcluded:
    def test_newer_index_etf_dataset_never_selected_as_stock_parent(self, tmp_path):
        """The exact production failure: an ETF/IDX-only dataset with a newer
        cutoff and enough rows must lose to the older full-market stock set.
        Without the fix the ETF set wins and the stock chain orphans bars."""
        store = DatasetStore(tmp_path / "md")
        _publish_dataset(
            store,
            [f"SZSE.STK.{i:06d}" for i in range(1, 51)],
            dataset_id="stock_full_20260810",
            cutoff=20260810,
        )
        _publish_dataset(
            store,
            [f"SSE.IDX.{i:06d}" for i in range(1, 51)],
            dataset_id="ie_full_20260814",
            cutoff=20260814,
        )

        start, parent_id = _script()._infer_incremental_resume(
            store, source="tushare", adjustment="none")
        assert parent_id == "stock_full_20260810", (
            f"resume picked {parent_id}; an index/ETF-only dataset must never "
            f"become the stock chain parent"
        )
        assert start is not None and start <= 20260810

    def test_index_etf_universe_type_excluded_without_symbol_lookalikes(self, tmp_path):
        """Structural exclusion works even if the symbol pool has no IDX/ETF
        suffix at all — the manifest tag is authoritative."""
        store = DatasetStore(tmp_path / "md")
        _publish_dataset(
            store,
            [f"SZSE.STK.{i:06d}" for i in range(1, 51)],
            dataset_id="stock_full_20260810",
            cutoff=20260810,
        )
        _publish_dataset(
            store,
            [f"X{i:06d}.XX" for i in range(1, 51)],
            dataset_id="tagged_ie_20260814",
            cutoff=20260814,
            universe_type=INDEX_ETF_UNIVERSE_TYPE,
        )

        start, parent_id = _script()._infer_incremental_resume(
            store, source="tushare", adjustment="none")
        assert parent_id == "stock_full_20260810"

    def test_mixed_dataset_with_stock_majority_still_eligible(self, tmp_path):
        """A mixed pool where stocks are the majority is a valid stock
        parent (delisted-complement style supplement datasets remain usable)."""
        store = DatasetStore(tmp_path / "md")
        stock_syms = [f"SZSE.STK.{i:06d}" for i in range(1, 61)]
        ie_syms = [f"SSE.ETF.{i:06d}" for i in range(1, 21)]
        _publish_dataset(
            store,
            stock_syms + ie_syms,
            dataset_id="mixed_full_20260812",
            cutoff=20260812,
        )

        start, parent_id = _script()._infer_incremental_resume(
            store, source="tushare", adjustment="none")
        assert parent_id == "mixed_full_20260812"


class TestSyncDatasetUniverseTypeTag:
    def test_sync_dataset_persists_universe_type(self, tmp_path):
        """_sync_dataset writes the requested universe_type onto the manifest."""
        from wtpy.apps.astock.data.providers.base import (
            AdjustmentMode,
            BarPeriod,
            MarketBar,
            ProviderCapabilities,
        )

        class _Provider:
            def __init__(self):
                self._batch_size = 1

            def provider_version(self):
                return "fake_raw_v1"

            def capabilities(self):
                return ProviderCapabilities(
                    source="tushare", adjustments=[AdjustmentMode.NONE],
                    periods=[BarPeriod.DAY], supports_batch=False,
                    max_batch_size=1, requires_client_online=False,
                    supports_universe=True, supports_delisted=True,
                    supports_bse=True,
                )

            def fetch_bars(self, request):
                out = []
                for sym in request.symbols:
                    out.append(MarketBar(
                        symbol=sym, trade_date=20260814, period="1d",
                        open=1.0, high=1.1, low=0.9, close=1.05,
                        volume=1000.0, amount=10000.0, source="tushare",
                        adjustment="none", data_cutoff_date=20260814,
                    ))
                return out

        store = DatasetStore(tmp_path / "md")
        syms = ["SSE.IDX.000001", "SSE.ETF.510300"]
        result = _script()._sync_dataset(
            provider=_Provider(),
            store=store,
            symbols=syms,
            source="tushare",
            adjustment=AdjustmentMode.NONE,
            period=BarPeriod.DAY,
            sync_run_id="ietag1",
            end_date=20260814,
            universe_type=INDEX_ETF_UNIVERSE_TYPE,
        )
        m = store.load_manifest(result["dataset_id"])
        assert m is not None
        assert m.universe_type == INDEX_ETF_UNIVERSE_TYPE

    def test_sync_dataset_default_tag_empty(self, tmp_path):
        """Stock-chain syncs keep the default empty universe_type."""
        from wtpy.apps.astock.data.providers.base import (
            AdjustmentMode,
            BarPeriod,
            MarketBar,
            ProviderCapabilities,
        )

        class _Provider:
            def __init__(self):
                self._batch_size = 1

            def provider_version(self):
                return "fake_raw_v1"

            def capabilities(self):
                return ProviderCapabilities(
                    source="tushare", adjustments=[AdjustmentMode.NONE],
                    periods=[BarPeriod.DAY], supports_batch=False,
                    max_batch_size=1, requires_client_online=False,
                    supports_universe=True, supports_delisted=True,
                    supports_bse=True,
                )

            def fetch_bars(self, request):
                out = []
                for sym in request.symbols:
                    out.append(MarketBar(
                        symbol=sym, trade_date=20260814, period="1d",
                        open=1.0, high=1.1, low=0.9, close=1.05,
                        volume=1000.0, amount=10000.0, source="tushare",
                        adjustment="none", data_cutoff_date=20260814,
                    ))
                return out

        store = DatasetStore(tmp_path / "md")
        result = _script()._sync_dataset(
            provider=_Provider(),
            store=store,
            symbols=["SZSE.STK.000001"],
            source="tushare",
            adjustment=AdjustmentMode.NONE,
            period=BarPeriod.DAY,
            sync_run_id="stock1",
            end_date=20260814,
        )
        m = store.load_manifest(result["dataset_id"])
        assert m is not None
        assert m.universe_type == ""


class TestTushareRawBaseCandidate:
    def test_index_etf_tagged_dataset_not_base_candidate(self):
        """Formal L1/L2 base selection excludes index/ETF-tagged datasets."""
        m = DatasetManifest(
            dataset_id="ie_20260814",
            source="tushare",
            adjustment="none",
            period="1d",
            status="ready",
            dataset_type="bars",
            symbols=[
                SymbolRecord(
                    symbol="SSE.IDX.000001", blob_sha256="abc",
                    first_date=20250101, last_date=20260814,
                    row_count=400, quality="ok"),
            ],
            symbol_count=1,
            row_count=400,
            data_cutoff_date=20260814,
            universe_type=INDEX_ETF_UNIVERSE_TYPE,
        )
        assert _is_tushare_raw_base_candidate(m) is False
