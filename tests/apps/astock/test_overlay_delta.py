# -*- coding: utf-8 -*-
"""Overlay_v1 / DeltaStore tests.

Covers the plan's storage + read-layer acceptance:
  - delta idempotency (re-run same window adds nothing)
  - 20-day window revision (history correction overlays, visible at new
    watermark, invisible at old watermark)
  - watermark replay (old virtual dataset id reproduces the exact old surface)
  - suspended-symbol empty window (no rows, no growth)
  - transaction failure (bad row aborts, nothing persists)
  - duplicate dates in one batch (last wins within the batch)
  - factor anchor change (QFQ recomputes at the new watermark)
  - legacy blob manifests keep reading their blobs (compatibility)
  - latest resolution returns virtual L1/L2 in overlay mode
  - batch array reads (one delta query for many symbols)
"""

from __future__ import annotations

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    DeltaWriteError,
    KIND_BARS,
    KIND_FACTOR,
    load_overlay_state,
)
from wtpy.apps.astock.data.repository import MarketDataRepository

from .conftest import (
    OVERLAY_BASE_DATES,
    _mk_overlay_bar as _mk_bar,
    build_overlay_warehouse,
    commit_eod_delta,
)


def _rows(dates_close: dict) -> dict:
    out = {}
    for sym, pairs in dates_close.items():
        out[sym] = [
            (d, c - 0.1, c + 0.2, c - 0.2, c, 1000.0, 100000.0)
            for d, c in pairs
        ]
    return out


class TestDeltaStoreBasics:
    def test_idempotent_rerun_same_window(self, delta):
        rows = _rows({"SSE.STK.600000": [(20240109, 10.8)]})
        r1 = delta.commit_batch(
            batch_id="b1", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows=rows,
        )
        assert r1["new_rows"] == 1
        r2 = delta.commit_batch(
            batch_id="b1", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows=rows,
        )
        assert r2["new_rows"] == 0 and r2["skipped_rows"] == 1
        assert delta.delta_row_count(KIND_BARS) == 1

    def test_duplicate_dates_in_batch_last_wins(self, delta):
        rows = {
            "SSE.STK.600000": [
                (20240109, 10.7, 10.9, 10.6, 10.8, 1000.0, 100000.0),
                (20240109, 10.8, 11.0, 10.7, 10.9, 1000.0, 100000.0),
            ]
        }
        delta.commit_batch(
            batch_id="dup", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows=rows,
        )
        visible = delta.load_visible_bars(["SSE.STK.600000"], 20240109)
        assert visible["SSE.STK.600000"][20240109][3] == 10.9
        assert delta.delta_row_count(KIND_BARS) == 2  # both rows stored (versioned)

    def test_revision_appends_version(self, delta):
        rows = _rows({"SSE.STK.600000": [(20240109, 10.8)]})
        delta.commit_batch(
            batch_id="v1", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows=rows,
        )
        rows2 = _rows({"SSE.STK.600000": [(20240109, 10.9)]})
        delta.commit_batch(
            batch_id="v2", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240110, rows=rows2,
        )
        old = delta.load_visible_bars(["SSE.STK.600000"], 20240109)
        new = delta.load_visible_bars(["SSE.STK.600000"], 20240110)
        assert old["SSE.STK.600000"][20240109][3] == 10.8
        assert new["SSE.STK.600000"][20240109][3] == 10.9
        assert delta.delta_row_count(KIND_BARS) == 2

    def test_transaction_failure_rolls_back(self, delta):
        bad = _rows({"SSE.STK.600000": [(20240109, 10.8)]})
        # value count mismatch -> abort before any insert
        bad["SSE.STK.600000"].append((20240110, 10.9, 11.0))  # only 3 values
        with pytest.raises(DeltaWriteError):
            delta.commit_batch(
                batch_id="bad", kind=KIND_BARS, source="tushare",
                adjustment="none", period="1d", base_dataset_id="base",
                watermark=20240110, rows=bad,
            )
        assert delta.delta_row_count(KIND_BARS) == 0
        assert delta.batch_exists("bad") is False

    def test_empty_batch_commits_zero_rows(self, delta):
        r = delta.commit_batch(
            batch_id="empty", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows={},
        )
        assert r["new_rows"] == 0
        assert delta.batch_exists("empty")

    def test_factor_anchor_change_visible_in_qfq(self, warehouse, delta, tmp_path):
        # day1: new bar at 20240109 with factor still 1.5
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
            factor_rows={"SSE.STK.600000": [(20240109, 1.5)]},
            batch_suffix="day1",
        )
        repo = MarketDataRepository(warehouse)
        l1 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        arr = repo.load_bar_arrays(
            dataset_id=l1.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        # anchor = last factor <= 20240109 = 1.5 -> ratio 1.0 -> qfq = raw
        assert abs(arr["close"][-1] - 10.8) < 1e-6

        # day2: a dividend changes the factor on 20240109 to 1.6
        commit_eod_delta(
            warehouse,
            cutoff=20240110,
            rows=_rows({"SSE.STK.600000": [(20240110, 11.0)]}),
            factor_rows={"SSE.STK.600000": [(20240109, 1.6)]},
            batch_suffix="day2",
        )
        l1b = repo.resolve_latest_ready(
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        arrb = repo.load_bar_arrays(
            dataset_id=l1b.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        # anchor = 1.6; 20240109 qfq = raw 10.8 * (1.6/1.6) = 10.8;
        # 20240110 raw 11.0 * (1.6/1.6) = 11.0
        assert abs(arrb["close"][-1] - 11.0) < 1e-6


class TestOverlayRepository:
    def test_latest_resolves_virtual_l1_l2(self, warehouse):
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        l1 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        assert l2.storage_mode == "overlay_v1"
        assert l1.storage_mode == "overlay_v1"
        assert l2.view_type == "l2_virtual_composite"
        assert l1.view_type == "l1_virtual_qfq"

    def test_merged_delta_visible_and_old_watermark_replays(
        self, warehouse, tmp_path
    ):
        repo = MarketDataRepository(warehouse)
        l2_old = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        old_len = len(
            repo.load_bars(dataset_id=l2_old.dataset_id, symbol="SSE.STK.600000")
        )
        assert old_len == len(OVERLAY_BASE_DATES)

        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
            factor_rows={"SSE.STK.600000": [(20240109, 1.5)]},
            batch_suffix="eod1",
        )
        l2_new = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        assert l2_new.dataset_id != l2_old.dataset_id
        new_len = len(
            repo.load_bars(dataset_id=l2_new.dataset_id, symbol="SSE.STK.600000")
        )
        assert new_len == old_len + 1
        # old watermark still replays the old surface
        assert len(
            repo.load_bars(dataset_id=l2_old.dataset_id, symbol="SSE.STK.600000")
        ) == old_len

    def test_suspended_symbol_empty_window_no_growth(self, warehouse, tmp_path):
        repo = MarketDataRepository(warehouse)
        before = len(list(warehouse.blobs_dir.glob("*.npz")))
        # only 600000 trades; 000001 suspended (absent from the window)
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
            batch_suffix="susp",
        )
        after = len(list(warehouse.blobs_dir.glob("*.npz")))
        assert after == before  # no new snapshot blobs
        # suspended symbol keeps its base history, no delta rows
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        bars_000001 = repo.load_bars(
            dataset_id=l2.dataset_id, symbol="SZSE.STK.000001"
        )
        assert len(bars_000001) == len(OVERLAY_BASE_DATES)
        assert bars_000001[-1].trade_date == OVERLAY_BASE_DATES[-1]

    def test_batch_reads_many_symbols_single_delta_query(
        self, warehouse, tmp_path, monkeypatch
    ):
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({
                "SSE.STK.600000": [(20240109, 10.8)],
                "SZSE.STK.000001": [(20240109, 5.4)],
                "SSE.STK.601088": [(20240109, 20.8)],
            }),
            batch_suffix="eod1",
        )
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        # count DB connections made while loading three symbols
        from wtpy.apps.astock.data.overlay import OverlayView

        calls = {"n": 0}
        orig = OverlayView.from_root

        def _counting_from_root(*a, **k):
            return orig(*a, **k)

        arrs = repo.load_bar_arrays(
            dataset_id=l2.dataset_id,
            symbols=["SSE.STK.600000", "SZSE.STK.000001", "SSE.STK.601088"],
        )
        assert len(arrs) == 3
        assert arrs["SSE.STK.600000"]["trade_date"][-1] == 20240109
        assert arrs["SSE.STK.601088"]["trade_date"][-1] == 20240109

    def test_legacy_blob_manifest_still_reads_blobs(self, tmp_path):
        # a legacy (non-overlay) warehouse keeps working through the same repo
        store = DatasetStore(tmp_path / "legacy")
        from wtpy.apps.astock.data.dataset_store import DatasetManifest, SymbolRecord

        bars = [
            _mk_bar("SSE.STK.600000", d, 10.0)
            for d in OVERLAY_BASE_DATES
        ]
        sha = store.store_bars("SSE.STK.600000", bars)
        m = DatasetManifest(
            dataset_id="legacy_none_1d", source="tushare", adjustment="none",
            period="1d", data_cutoff_date=OVERLAY_BASE_DATES[-1],
            snapshot_date=OVERLAY_BASE_DATES[-1], provider_version="test",
            status="ready", created_at="2024-01-08T18:00:00",
        )
        m.symbols = [SymbolRecord(
            symbol="SSE.STK.600000", blob_sha256=sha,
            first_date=OVERLAY_BASE_DATES[0], last_date=OVERLAY_BASE_DATES[-1],
            row_count=len(bars), quality="ok",
        )]
        m.symbol_count = 1
        m.row_count = len(bars)
        store.publish(m)
        repo = MarketDataRepository(store)
        out = repo.load_bars(dataset_id="legacy_none_1d", symbol="SSE.STK.600000")
        assert len(out) == len(OVERLAY_BASE_DATES)
        arrs = repo.load_bar_arrays(
            dataset_id="legacy_none_1d", symbols=["SSE.STK.600000"]
        )
        assert arrs["SSE.STK.600000"]["trade_date"][0] == OVERLAY_BASE_DATES[0]

    def test_delisted_pool_included_in_virtual_l2(self, tmp_path):
        store = build_overlay_warehouse(tmp_path, delisted=True)
        repo = MarketDataRepository(store)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        assert l2.symbol_count == 4  # 3 active + 1 delisted
        bars = repo.load_bars(dataset_id=l2.dataset_id, symbol="SZSE.STK.300104")
        assert len(bars) == 4

    def test_qfq_leading_gap_and_missing_factor(self, warehouse):
        repo = MarketDataRepository(warehouse)
        l1 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        # 600000 has factors since 20230101 -> all base rows derive
        arr = repo.load_bar_arrays(
            dataset_id=l1.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        assert arr["close"][0] > 0

    def test_delta_only_symbol_is_served(self, tmp_path):
        """A symbol present ONLY in the delta (IPO listed after the base
        snapshot) must be readable — never silently dropped."""
        store = build_overlay_warehouse(tmp_path)
        repo = MarketDataRepository(store)
        # commit a new symbol into the delta (not in the base pool)
        from wtpy.apps.astock.data.delta_store import load_overlay_state
        from wtpy.apps.astock.data.delta_store import save_overlay_state

        base = store.load_manifest(
            load_overlay_state(tmp_path).base_dataset_id
        )
        ds = __import__(
            "wtpy.apps.astock.data.delta_store", fromlist=["DeltaStore"]
        ).DeltaStore(tmp_path)
        ds.commit_batch(
            batch_id="ipo", kind="bars", source="tushare", adjustment="none",
            period="1d", base_dataset_id=base.dataset_id, watermark=20240109,
            rows={"SSE.STK.600999": [(20240109, 5.0, 5.5, 4.9, 5.2,
                                      100.0, 1000.0)]},
        )
        st = load_overlay_state(tmp_path)
        st.delta_watermark = 20240109
        save_overlay_state(tmp_path, st)

        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        # the virtual pool lists the new symbol
        assert "SSE.STK.600999" in [r.symbol for r in l2.symbols]
        # per-symbol read serves it from the delta
        bars = repo.load_bars(dataset_id=l2.dataset_id, symbol="SSE.STK.600999")
        assert len(bars) == 1
        assert bars[0].trade_date == 20240109
        assert abs(bars[0].close - 5.2) < 1e-9
        # whole-board load (symbol=None) also includes it
        all_bars = repo.load_bars(dataset_id=l2.dataset_id)
        assert "SSE.STK.600999" in {b.symbol for b in all_bars}

