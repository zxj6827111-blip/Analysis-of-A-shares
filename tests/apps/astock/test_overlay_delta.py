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

import subprocess
import sys
from pathlib import Path

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
from wtpy.apps.astock.data.overlay import OverlayView
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

    def test_database_failure_after_first_chunk_rolls_back(
        self, delta, monkeypatch
    ):
        import wtpy.apps.astock.data.delta_store as delta_store_mod

        delta.init_schema()
        real_connect = delta.connect

        class FailingConnection:
            def __init__(self, conn):
                self._conn = conn
                self._bar_inserts = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self._conn.close()
                return False

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if normalized.startswith("INSERT INTO daily_bars"):
                    self._bar_inserts += 1
                    if self._bar_inserts == 2:
                        raise RuntimeError("injected second chunk failure")
                if params is None:
                    return self._conn.execute(sql)
                return self._conn.execute(sql, params)

        def failing_connect(*, read_only=False):
            if read_only:
                return real_connect(read_only=True)
            return FailingConnection(real_connect())

        rows = _rows({
            "SSE.STK.600000": [(20240109, 10.8)],
            "SZSE.STK.000001": [(20240109, 5.4)],
        })
        with monkeypatch.context() as patch:
            patch.setattr(delta_store_mod, "_INSERT_CHUNK_ROWS", 1)
            patch.setattr(delta, "connect", failing_connect)
            with pytest.raises(DeltaWriteError, match="transaction failed"):
                delta.commit_batch(
                    batch_id="mid-transaction-failure",
                    kind=KIND_BARS,
                    source="tushare",
                    adjustment="none",
                    period="1d",
                    base_dataset_id="base",
                    watermark=20240109,
                    rows=rows,
                )

        assert delta.delta_row_count(KIND_BARS) == 0
        assert delta.batch_exists("mid-transaction-failure") is False

    def test_empty_batch_commits_zero_rows(self, delta):
        r = delta.commit_batch(
            batch_id="empty", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows={},
        )
        assert r["new_rows"] == 0
        assert delta.batch_exists("empty")

    def test_empty_symbol_lists_commit_zero_rows(self, delta):
        r = delta.commit_batch(
            batch_id="empty-lists", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows={"SSE.STK.600000": []},
        )
        assert r["new_rows"] == 0
        assert delta.batch_exists("empty-lists")

    def test_large_batch_crosses_bulk_insert_chunks(self, delta):
        rows = {
            f"SSE.STK.{600000 + i:06d}": [
                (20240109, 10.0, 10.2, 9.8, 10.1, 1000.0, 10100.0)
            ]
            for i in range(1001)
        }
        r = delta.commit_batch(
            batch_id="bulk", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id="base",
            watermark=20240109, rows=rows,
        )
        assert r["new_rows"] == 1001
        assert delta.delta_row_count(KIND_BARS) == 1001
        visible = delta.load_visible_bars(
            ["SSE.STK.600000", "SSE.STK.601000"], 20240109
        )
        assert visible["SSE.STK.600000"][20240109][3] == pytest.approx(10.1)
        assert visible["SSE.STK.601000"][20240109][3] == pytest.approx(10.1)

    def test_legacy_schema_is_readable_before_first_upgraded_write(self, tmp_path):
        import duckdb

        legacy = DeltaStore(tmp_path)
        legacy.delta_dir.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(legacy.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE sync_batches (
                    batch_id TEXT PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    period TEXT NOT NULL,
                    base_dataset_id TEXT NOT NULL,
                    watermark INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO sync_batches VALUES "
                "('legacy', 'main', 'bars', 'tushare', 'none', '1d', "
                "'base', 20240109, 0, '2024-01-09T18:30:00', 'committed')"
            )

        batches = legacy.list_batches()
        assert batches[0]["commit_seq"] == 0
        legacy.init_schema()
        assert legacy.current_commit_seq(KIND_BARS) == 1
        assert legacy.list_batches()[0]["commit_seq"] == 1
    def test_read_query_does_not_leave_cross_process_writer_lock(
        self, delta, tmp_path
    ):
        delta.commit_batch(
            batch_id="seed",
            kind=KIND_BARS,
            source="tushare",
            adjustment="none",
            period="1d",
            base_dataset_id="base",
            watermark=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
        )
        assert delta.load_all_visible_bars(20240109)
        code = (
            "from wtpy.apps.astock.data.delta_store import DeltaStore;"
            f"d=DeltaStore(r'{tmp_path}');"
            "d.commit_batch(batch_id='child',kind='bars',source='tushare',"
            "adjustment='none',period='1d',base_dataset_id='base',"
            "watermark=20240110,rows={'SSE.STK.600000':"
            "[(20240110,10,11,9,10.9,1000,100000)]})"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

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
        raw = repo.resolve_latest_ready(
            source="tushare", adjustment="none", period="1d"
        )
        assert l2.view_type == "l2_virtual_composite"
        assert l1.view_type == "l1_virtual_qfq"
        assert raw.view_type == "raw_virtual"

    def test_raw_latest_uses_delta_but_explicit_base_stays_frozen(
        self, warehouse
    ):
        repo = MarketDataRepository(warehouse)
        raw_old = repo.resolve_latest_ready(
            source="tushare", adjustment="none", period="1d"
        )
        state = load_overlay_state(warehouse.root)
        base_id = state.base_dataset_id
        commit_eod_delta(
            warehouse,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
            batch_suffix="raw_virtual",
        )
        raw_new = repo.resolve_latest_ready(
            source="tushare", adjustment="none", period="1d"
        )
        assert raw_new.dataset_id != raw_old.dataset_id
        assert repo.load_bars(
            dataset_id=raw_new.dataset_id, symbol="SSE.STK.600000"
        )[-1].trade_date == 20240109
        assert repo.load_bars(
            dataset_id=raw_old.dataset_id, symbol="SSE.STK.600000"
        )[-1].trade_date == 20240108
        assert repo.load_bars(
            dataset_id=base_id, symbol="SSE.STK.600000"
        )[-1].trade_date == 20240108

    def test_unpublished_same_watermark_batch_is_invisible_at_sequence_zero(
        self, warehouse
    ):
        from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

        repo = MarketDataRepository(warehouse)
        before = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        writer = DeltaEodWriter(warehouse)
        state = load_overlay_state(warehouse.root)
        writer.commit_bars(
            sync_run_id="unpublished",
            source="tushare",
            base_dataset_id=state.base_dataset_id,
            cutoff=20240108,
            rows=_rows({"SSE.STK.600000": [(20240108, 10.9)]}),
        )
        hidden = repo.load_bars(
            dataset_id=before.dataset_id, symbol="SSE.STK.600000"
        )
        assert hidden[-1].close == pytest.approx(10.2)

        writer.publish(delta_watermark=20240108)
        after = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        visible = repo.load_bars(
            dataset_id=after.dataset_id, symbol="SSE.STK.600000"
        )
        assert after.delta_commit_seq == 1
        assert visible[-1].close == pytest.approx(10.9)

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

    def test_same_watermark_revision_keeps_old_virtual_views(self, warehouse):
        from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

        writer = DeltaEodWriter(warehouse)
        state = load_overlay_state(warehouse.root)
        writer.commit_bars(
            sync_run_id="same_day_v1",
            source="tushare",
            base_dataset_id=state.base_dataset_id,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
        )
        writer.commit_factors(
            sync_run_id="same_day_v1",
            source="tushare",
            factor_base_dataset_id=state.factor_base_dataset_id,
            cutoff=20240109,
            rows={"SSE.STK.600000": [(20240109, 3.0)]},
        )
        writer.publish(delta_watermark=20240109, factor_watermark=20240109)

        repo = MarketDataRepository(warehouse)
        old_l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        old_factor = repo.resolve_latest_ready(
            source="tushare", adjustment="adj_factor", period="1d"
        )

        writer.commit_bars(
            sync_run_id="same_day_v2",
            source="tushare",
            base_dataset_id=state.base_dataset_id,
            cutoff=20240109,
            rows=_rows({"SSE.STK.600000": [(20240109, 11.2)]}),
        )
        writer.commit_factors(
            sync_run_id="same_day_v2",
            source="tushare",
            factor_base_dataset_id=state.factor_base_dataset_id,
            cutoff=20240109,
            rows={"SSE.STK.600000": [(20240109, 4.0)]},
        )
        writer.publish(delta_watermark=20240109, factor_watermark=20240109)

        new_l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        new_factor = repo.resolve_latest_ready(
            source="tushare", adjustment="adj_factor", period="1d"
        )
        assert new_l2.dataset_id != old_l2.dataset_id
        assert new_factor.dataset_id != old_factor.dataset_id
        assert new_l2.delta_commit_seq > old_l2.delta_commit_seq
        assert new_factor.factor_commit_seq > old_factor.factor_commit_seq

        old_bars = repo.load_bar_arrays(
            dataset_id=old_l2.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        new_bars = repo.load_bar_arrays(
            dataset_id=new_l2.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        assert old_bars["close"][-1] == pytest.approx(10.8)
        assert new_bars["close"][-1] == pytest.approx(11.2)

        old_factors = repo.load_factor_arrays(
            dataset_id=old_factor.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        new_factors = repo.load_factor_arrays(
            dataset_id=new_factor.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        assert old_factors["adj_factor"][-1] == pytest.approx(3.0)
        assert new_factors["adj_factor"][-1] == pytest.approx(4.0)
    def test_batch_factor_cache_tracks_same_watermark_commit_seq(
        self, warehouse
    ):
        from wtpy.apps.astock.data.delta_store import save_overlay_state
        from wtpy.apps.astock.data.overlay import OverlayView

        symbol = "SSE.STK.600000"
        view = OverlayView.from_root(warehouse.root)
        delta = DeltaStore(warehouse.root)
        state = load_overlay_state(warehouse.root)
        delta.commit_batch(
            batch_id="factor_cache_v1",
            kind=KIND_FACTOR,
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            base_dataset_id=state.factor_base_dataset_id,
            watermark=state.factor_watermark,
            rows={symbol: [(state.factor_watermark, 2.0)]},
        )
        state.factor_commit_seq = delta.current_commit_seq(KIND_FACTOR)
        save_overlay_state(warehouse.root, state)
        first = view.factor_arrays_batch(
            [symbol], watermark=state.factor_watermark
        )[symbol]
        assert first["adj_factor"][-1] == pytest.approx(2.0)

        delta.commit_batch(
            batch_id="factor_cache_v2",
            kind=KIND_FACTOR,
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            base_dataset_id=state.factor_base_dataset_id,
            watermark=state.factor_watermark,
            rows={symbol: [(state.factor_watermark, 4.0)]},
        )
        state.factor_commit_seq = delta.current_commit_seq(KIND_FACTOR)
        save_overlay_state(warehouse.root, state)
        revised = view.factor_arrays_batch(
            [symbol], watermark=state.factor_watermark
        )[symbol]
        assert revised["adj_factor"][-1] == pytest.approx(4.0)

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
        # 真实计数：批量加载 N 个符号只允许一次 delta 全量查询
        from wtpy.apps.astock.data.delta_store import DeltaStore

        calls = {"n": 0}
        orig = DeltaStore.load_all_visible_bars

        def _counting(self, watermark, **kw):
            calls["n"] += 1
            return orig(self, watermark, **kw)

        monkeypatch.setattr(DeltaStore, "load_all_visible_bars", _counting)

        arrs = repo.load_bar_arrays(
            dataset_id=l2.dataset_id,
            symbols=["SSE.STK.600000", "SZSE.STK.000001", "SSE.STK.601088"],
        )
        assert len(arrs) == 3
        assert arrs["SSE.STK.600000"]["trade_date"][-1] == 20240109
        assert arrs["SSE.STK.601088"]["trade_date"][-1] == 20240109
        assert calls["n"] == 1, (
            f"批量加载 3 个符号执行了 {calls['n']} 次 delta 查询"
        )

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

    def test_qfq_range_uses_snapshot_anchor_before_slicing(self, warehouse):
        from wtpy.apps.astock.data.delta_store import save_overlay_state
        from wtpy.apps.astock.data.overlay import OverlayView

        symbol = "SSE.STK.600000"
        state = load_overlay_state(warehouse.root)
        delta = DeltaStore(warehouse.root, state.delta_store_id)
        delta.commit_batch(
            batch_id="factor-anchor",
            kind=KIND_FACTOR,
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            base_dataset_id=state.factor_base_dataset_id,
            watermark=20240108,
            rows={symbol: [(20240108, 3.0)]},
        )
        state.factor_commit_seq = delta.current_commit_seq(KIND_FACTOR)
        save_overlay_state(warehouse.root, state)

        view = OverlayView.from_root(warehouse.root, required=True)
        full = view.qfq_arrays(symbol)
        ranged = view.qfq_arrays(
            symbol, start_date=20240103, end_date=20240104
        )
        assert ranged["trade_date"].tolist() == [20240103, 20240104]
        mask = (full["trade_date"] >= 20240103) & (
            full["trade_date"] <= 20240104
        )
        np.testing.assert_allclose(ranged["close"], full["close"][mask])

        batch_full = view.qfq_arrays_batch([symbol])[symbol]
        batch_ranged = view.qfq_arrays_batch(
            [symbol], start_date=20240103, end_date=20240104
        )[symbol]
        assert batch_ranged["trade_date"].tolist() == [20240103, 20240104]
        np.testing.assert_allclose(
            batch_ranged["close"], batch_full["close"][mask]
        )
        np.testing.assert_allclose(batch_ranged["close"], ranged["close"])

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
        st.delta_commit_seq = ds.current_commit_seq(KIND_BARS)
        save_overlay_state(tmp_path, st)

        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        # the virtual pool and metadata include the new symbol accurately
        records = {record.symbol: record for record in l2.symbols}
        assert "SSE.STK.600999" in records
        assert records["SSE.STK.600999"].row_count == 1
        assert records["SSE.STK.600999"].last_date == 20240109
        assert l2.data_cutoff_date == 20240109
        # per-symbol read serves it from the delta
        bars = repo.load_bars(dataset_id=l2.dataset_id, symbol="SSE.STK.600999")
        assert len(bars) == 1
        assert bars[0].trade_date == 20240109
        assert abs(bars[0].close - 5.2) < 1e-9
        # whole-board load (symbol=None) also includes it
        all_bars = repo.load_bars(dataset_id=l2.dataset_id)
        assert "SSE.STK.600999" in {b.symbol for b in all_bars}

    @pytest.mark.parametrize("alias", ["600000.SH", "sh600000"])
    def test_overlay_repository_preserves_legacy_symbol_aliases(
        self, warehouse, alias
    ):
        repo = MarketDataRepository(warehouse)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        l1 = repo.resolve_latest_ready(
            source="internal",
            adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        factor = repo.resolve_latest_ready(
            source="tushare", adjustment="adj_factor", period="1d"
        )

        l2_bars = repo.load_bars(dataset_id=l2.dataset_id, symbol=alias)
        l1_bars = repo.load_bars(dataset_id=l1.dataset_id, symbol=alias)
        l2_arrays = repo.load_bar_arrays(
            dataset_id=l2.dataset_id, symbols=[alias]
        )
        factor_arrays = repo.load_factor_arrays(
            dataset_id=factor.dataset_id, symbols=[alias]
        )

        assert l2_bars
        assert l1_bars
        assert {bar.symbol for bar in l2_bars + l1_bars} == {
            "SSE.STK.600000"
        }
        assert list(l2_arrays) == ["SSE.STK.600000"]
        assert factor_arrays[alias] is not None

    def test_delta_symbol_enumeration_failure_is_not_silenced(
        self, warehouse, monkeypatch
    ):
        view = OverlayView.from_root(warehouse.root, required=True)

        def _raise(*_args, **_kwargs):
            raise RuntimeError("delta catalog corrupt")

        monkeypatch.setattr(view.delta, "distinct_symbols", _raise)
        with pytest.raises(RuntimeError, match="delta catalog corrupt"):
            view._pool_records_with_delta()


def test_factor_dataset_reads_base_plus_delta(warehouse):
    from wtpy.apps.astock.data.adjustments import build_factor_series_from_dataset
    from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

    writer = DeltaEodWriter(warehouse)
    state = load_overlay_state(warehouse.root)
    writer.commit_bars(
        sync_run_id="factor_path",
        source="tushare",
        base_dataset_id=state.base_dataset_id,
        cutoff=20240109,
        rows=_rows({"SSE.STK.600000": [(20240109, 10.8)]}),
    )
    writer.commit_factors(
        sync_run_id="factor_path",
        source="tushare",
        factor_base_dataset_id=state.factor_base_dataset_id,
        cutoff=20240109,
        rows={"SSE.STK.600000": [(20240109, 3.0)]},
    )
    writer.publish(delta_watermark=20240109, factor_watermark=20240109)

    repo = MarketDataRepository(warehouse)
    factor_manifest = repo.resolve_latest_ready(
        source="tushare", adjustment="adj_factor", period="1d"
    )
    assert factor_manifest.view_type == "factor_virtual"
    series = build_factor_series_from_dataset(
        warehouse,
        factor_manifest,
        "SSE.STK.600000",
        [20240108, 20240109],
    )
    assert series.quality == "complete"
    assert series.factors == [1.5, 3.0]
