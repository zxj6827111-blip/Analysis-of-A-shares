# -*- coding: utf-8 -*-
"""P0-2 / P1-3: raw sub-dataset status aggregation and empty-window parent
retention.

P0-2: a raw surface that only published partial must never be reported as
top-level success (sync_tushare_incremental / sync_tushare_chain).
P1-3: an empty incremental window with a parent record keeps the parent blob;
symbols with no data AND no parent history beyond the threshold force the
manifest to partial instead of ready.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    MarketBar,
    ProviderCapabilities,
)

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

_MODULE = None


def _script():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "sync_status_agg_test", SYNC_SCRIPT)
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


class _RawProvider:
    """fetch_bars returns bars for symbols in ``bars``, nothing for others."""

    def __init__(self, bars: dict):
        self._bars = bars
        self._batch_size = 1

    def provider_version(self):
        return "fake_raw_v1"

    def capabilities(self):
        return ProviderCapabilities(
            source="tushare", adjustments=[AdjustmentMode.NONE],
            periods=[BarPeriod.DAY], supports_batch=False, max_batch_size=1,
            requires_client_online=False, supports_universe=True,
            supports_delisted=True, supports_bse=True,
        )

    def fetch_bars(self, request):
        out = []
        for sym in request.symbols:
            out.extend(self._bars.get(sym, []))
        return out


def _bar(symbol: str, trade_date: int) -> MarketBar:
    return MarketBar(
        symbol=symbol, trade_date=trade_date, period="1d",
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000.0, amount=10000.0, source="tushare",
        adjustment="none", data_cutoff_date=trade_date,
    )


def _publish_raw_parent(store, symbols, *, dataset_id="tushare_none_1d_parent"):
    records = []
    for sym in symbols:
        dates = _calendar_dates(20260101, 20)
        sha = store.store_factors(sym, dates, np.full(20, 1.0))
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=20, quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="none",
        period="1d", status="ready", dataset_type="bars", symbols=records,
        symbol_count=len(records), row_count=sum(r.row_count for r in records),
        data_cutoff_date=int(max(r.last_date or 0 for r in records)),
    )
    store.publish(m)
    return m


def _run_sync(store, symbols, *, parent_id=None, bars=None, ck_path=None):
    smd = _script()
    provider = _RawProvider(bars or {})
    return smd._sync_dataset(
        provider=provider,
        store=store,
        symbols=symbols,
        source="tushare",
        adjustment=AdjustmentMode.NONE,
        period=BarPeriod.DAY,
        sync_run_id="rawtest1",
        start_date=20260101,
        end_date=20260201,
        parent_dataset_id=parent_id,
        checkpoint_path=ck_path,
    )


class TestRawEmptyWindowParentRetention:
    def test_empty_window_keeps_parent_blob(self, tmp_path):
        """Empty window + parent record: the new manifest keeps the parent
        blob (quality ok) and carries the retention marker on the record."""
        store = DatasetStore(tmp_path / "md")
        symbols = ["SSE.STK.600000", "SZSE.STK.000001"]
        parent = _publish_raw_parent(store, symbols)
        result = _run_sync(store, symbols, parent_id=parent.dataset_id)
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "ready"
        for sym, prec in zip(symbols, parent.symbols):
            rec = next(r for r in m.symbols if r.symbol == sym)
            assert rec.quality == "ok"
            assert rec.blob_sha256 == prec.blob_sha256
            assert rec.row_count == prec.row_count
            assert rec.first_date == prec.first_date
            assert rec.last_date == prec.last_date
            assert rec.window_status == "no_new_rows_parent_retained"

    def test_no_parent_no_data_over_threshold_partial(self, tmp_path):
        """40 symbols with no data and no parent: 40 > min(20, 5%*40=2)
        -> manifest must be partial, not ready."""
        store = DatasetStore(tmp_path / "md")
        symbols = [f"SSE.STK.{600000 + i}" for i in range(40)]
        result = _run_sync(store, symbols)
        assert result["status"] == "partial"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "partial"
        assert result["no_data"] == 40

    def test_no_data_without_parent_under_threshold_ready(self, tmp_path):
        """39/40 symbols have data, 1 has no data and no parent:
        1 <= min(20, 5%*40=2) -> normal ready publish."""
        store = DatasetStore(tmp_path / "md")
        symbols = [f"SSE.STK.{600000 + i}" for i in range(40)]
        bars = {sym: [_bar(sym, 20260105), _bar(sym, 20260106)]
                for sym in symbols[1:]}
        result = _run_sync(store, symbols, bars=bars)
        assert result["status"] == "ready"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "ready"
        assert result["no_data"] == 1
        rec = next(r for r in m.symbols if r.symbol == symbols[0])
        assert rec.quality == "no_data"
        assert rec.blob_sha256 == ""

    def test_small_universe_two_no_data_stays_ready(self, tmp_path):
        """30-symbol universe with 2 no-data symbols (no parent): the no_data
        threshold floor (max(2, 5%*30=1)=2) keeps the surface ready instead
        of demoting a small index/ETF-like universe to partial."""
        store = DatasetStore(tmp_path / "md")
        symbols = [f"SSE.STK.{600000 + i}" for i in range(30)]
        bars = {sym: [_bar(sym, 20260105), _bar(sym, 20260106)]
                for sym in symbols[2:]}
        result = _run_sync(store, symbols, bars=bars)
        assert result["no_data"] == 2
        assert result["status"] == "ready"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "ready"

    def test_small_universe_three_no_data_partial(self, tmp_path):
        """30-symbol universe with 3 no-data symbols exceeds the floor
        (3 > max(2, 5%*30=1)=2) -> partial, not ready."""
        store = DatasetStore(tmp_path / "md")
        symbols = [f"SSE.STK.{600000 + i}" for i in range(30)]
        bars = {sym: [_bar(sym, 20260105), _bar(sym, 20260106)]
                for sym in symbols[3:]}
        result = _run_sync(store, symbols, bars=bars)
        assert result["no_data"] == 3
        assert result["status"] == "partial"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "partial"

    def test_empty_window_without_parent_keeps_no_data(self, tmp_path):
        """No parent at all: an empty window stays a plain no_data record."""
        store = DatasetStore(tmp_path / "md")
        symbols = ["SSE.STK.600000"]
        result = _run_sync(store, symbols)
        m = store.load_manifest(result["dataset_id"])
        rec = next(r for r in m.symbols if r.symbol == symbols[0])
        assert rec.quality == "no_data"
        assert rec.blob_sha256 == ""
        assert rec.window_status == ""

    def test_resume_no_data_with_parent_converted_to_retained(self, tmp_path):
        """Resume-restored no_data records are re-classified to parent
        retention when a parent blob exists (not counted as no_data)."""
        store = DatasetStore(tmp_path / "md")
        # parent covers only SYM A: B has no parent record to retain
        parent = _publish_raw_parent(store, ["SSE.STK.600000"])
        resume_records = {
            "SSE.STK.600000": {"symbol": "SSE.STK.600000", "blob_sha256": "",
                               "first_date": None, "last_date": None,
                               "row_count": 0, "quality": "no_data",
                               "error": "empty"},
            "SZSE.STK.000001": {"symbol": "SZSE.STK.000001", "blob_sha256": "",
                                "first_date": None, "last_date": None,
                                "row_count": 0, "quality": "no_data",
                                "error": "empty"},
        }
        smd = _script()
        result = smd._sync_dataset(
            provider=_RawProvider({}),
            store=store,
            symbols=["SSE.STK.600000", "SZSE.STK.000001"],
            source="tushare",
            adjustment=AdjustmentMode.NONE,
            period=BarPeriod.DAY,
            sync_run_id="rawresume1",
            start_date=20260101,
            end_date=20260201,
            parent_dataset_id=parent.dataset_id,
            resume_records=resume_records,
        )
        m = store.load_manifest(result["dataset_id"])
        rec_a = next(r for r in m.symbols if r.symbol == "SSE.STK.600000")
        assert rec_a.quality == "ok"
        assert rec_a.blob_sha256 == parent.symbols[0].blob_sha256
        assert rec_a.row_count == parent.symbols[0].row_count
        assert rec_a.window_status == "no_new_rows_parent_retained"
        rec_b = next(r for r in m.symbols if r.symbol == "SZSE.STK.000001")
        assert rec_b.quality == "no_data"
        assert rec_b.blob_sha256 == ""
        # only the un-parented symbol counts as no_data; the 2-symbol
        # universe is within the no_data floor (max(2, 5%*2)=2) -> ready
        assert result["no_data"] == 1
        assert result["success"] == 1
        assert result["status"] == "ready"


class TestRawStatusAggregation:
    def _incremental_args(self, tmp_path):
        return SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260201,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None,
        )

    def _run_incremental(self, tmp_path, monkeypatch, ds_results):
        smd = _script()
        store = DatasetStore(tmp_path / "md")

        def fake_sync_dataset(**kw):
            phase = f"{kw['adjustment'].value}/{kw['period'].value}"
            return ds_results[phase]

        monkeypatch.setattr(smd, "_sync_dataset", fake_sync_dataset)
        monkeypatch.setattr(smd, "_reconcile_after_sync",
                            lambda store, dry_run=False: {"status": "up_to_date"})

        class _HealthPro:
            def health_check(self):
                return True

            def fetch_universe(self, **kw):
                return []

        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider",
                            lambda token=None: _HealthPro())
        return smd.sync_tushare_incremental(self._incremental_args(tmp_path), store)

    def test_any_child_partial_makes_top_partial(self, tmp_path, monkeypatch):
        result = self._run_incremental(tmp_path, monkeypatch, {
            "none/1d": {"dataset_id": "d1", "success": 1, "total": 2,
                        "status": "partial"},
            "qfq/1d": {"dataset_id": "d2", "success": 2, "total": 2,
                       "status": "ready"},
        })
        assert result["status"] == "partial"
        assert "none_1d" in result["warning"]

    def test_any_child_failed_makes_top_failed(self, tmp_path, monkeypatch):
        result = self._run_incremental(tmp_path, monkeypatch, {
            "none/1d": {"dataset_id": "d1", "success": 0, "total": 2,
                        "status": "failed"},
            "qfq/1d": {"dataset_id": "d2", "success": 2, "total": 2,
                       "status": "ready"},
        })
        assert result["status"] == "failed"
        assert "none_1d" in result["error"]

    def test_all_ready_stays_success(self, tmp_path, monkeypatch):
        result = self._run_incremental(tmp_path, monkeypatch, {
            "none/1d": {"dataset_id": "d1", "success": 2, "total": 2,
                        "status": "ready"},
            "qfq/1d": {"dataset_id": "d2", "success": 2, "total": 2,
                       "status": "ready"},
        })
        assert result["status"] == "success"


class TestChainRawPartial:
    def _chain(self, tmp_path, monkeypatch, raw_status,
               raw_reconcile_status="up_to_date"):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        factor_called = []

        def fake_factor(args, store):
            factor_called.append("factor")
            return {"status": "success",
                    "dataset_status": "ready",
                    "dataset_id": "tushare_adjfactor_1d_x",
                    "reconcile": {"status": "up_to_date",
                                  "missing": [], "issues": []},
                    "datasets": {}}

        monkeypatch.setattr(
            smd, "sync_tushare_incremental",
            lambda args, store, **kw: {
                "status": raw_status, "sync_run_id": "raw_run",
                "datasets": {"none_1d": {"dataset_id": "d1"}},
                "reconcile": {"status": raw_reconcile_status,
                              "missing": [], "issues": []},
            })
        monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {"status": "published", "missing": [],
                                          "issues": [], "l1_dataset_id": "l1",
                                          "l2_dataset_id": "l2"})
        # the chain's delisted-pool step spawns a child process (network);
        # these unit tests exercise the chain orchestration only
        monkeypatch.setattr(
            smd, "_run_delisted_pool_sync",
            lambda store, args: {"status": "ok", "exit_code": 0})
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: "/tmp/uni.csv")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        return result, factor_called

    def test_raw_partial_skips_factor_and_reconcile(self, tmp_path, monkeypatch):
        """P0-1 fail-closed: a partial raw surface must stop the chain before
        the factor step — factor=skipped, no reconcile, no published product.
        A残缺 raw surface must never feed the factor step or update the
        formal L1/L2 product surfaces."""
        result, factor_called = self._chain(tmp_path, monkeypatch,
                                            raw_status="partial")
        assert result["status"] == "partial"
        assert result["factor"]["status"] == "skipped"
        assert result["factor"]["reason"] == "raw_step_not_success"
        assert result["factor"]["raw_status"] == "partial"
        assert factor_called == []
        # the raw step's own internal reconcile may be present, but the
        # chain must not claim a reconcile over the (skipped) factor step
        assert result["reconcile"]["status"] != "published"

    def test_raw_warning_skips_factor(self, tmp_path, monkeypatch):
        result, factor_called = self._chain(tmp_path, monkeypatch,
                                            raw_status="warning")
        assert result["status"] == "warning"
        assert result["factor"]["status"] == "skipped"
        assert result["factor"]["reason"] == "raw_step_not_success"
        assert result["factor"]["raw_status"] == "warning"
        assert factor_called == []

    def test_raw_internal_waiting_reconcile_not_chain_warning(
            self, tmp_path, monkeypatch):
        """Raw sub-surfaces all success but the raw step's internal reconcile
        is waiting_for_parent (same-day run: factor not yet pulled when the
        raw step's reconcile ran). The chain must not lock into warning once
        the factor step publishes ready and the final reconcile publishes."""
        result, factor_called = self._chain(tmp_path, monkeypatch,
                                            raw_status="success",
                                            raw_reconcile_status="waiting_for_parent")
        assert result["status"] == "success"
        assert result["factor"]["dataset_status"] == "ready"
        assert result["reconcile"]["status"] == "published"
        assert factor_called == ["factor"]

    def test_raw_failed_skips_factor(self, tmp_path, monkeypatch):
        result, factor_called = self._chain(tmp_path, monkeypatch,
                                            raw_status="failed")
        assert result["status"] == "failed"
        assert result["factor"]["status"] == "skipped"
        assert result["factor"]["reason"] == "raw_step_not_success"
        assert factor_called == []


class TestChainSingleReconcile:
    """The chain runs the product reconcile exactly once (after the factor
    step); the raw step's skip_reconcile_status must skip the reconcile work
    itself, not just the status demotion."""

    def _chain(self, tmp_path, monkeypatch):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        reconcile_calls = []

        def counting_reconcile(store, *, dry_run=False):
            reconcile_calls.append("reconcile")
            return {"status": "published", "missing": [], "issues": [],
                    "l1_dataset_id": "l1", "l2_dataset_id": "l2"}

        monkeypatch.setattr(smd, "_reconcile_after_sync", counting_reconcile)
        monkeypatch.setattr(
            smd, "_sync_dataset",
            lambda **kw: {"dataset_id": f"ds_{kw['adjustment'].value}",
                          "success": 1, "total": 1, "status": "ready"})
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "success",
                                 "dataset_status": "ready",
                                 "dataset_id": "tushare_adjfactor_1d_x",
                                 "reconcile": {"status": "up_to_date",
                                               "missing": [], "issues": []},
                                 "datasets": {}})
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: "/tmp/uni.csv")
        # the chain's delisted-pool step spawns a child process (network);
        # these unit tests exercise the chain orchestration only
        monkeypatch.setattr(
            smd, "_run_delisted_pool_sync",
            lambda store, args: {"status": "ok", "exit_code": 0})

        class _HealthPro:
            def health_check(self):
                return True

            def fetch_universe(self, **kw):
                return []

        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider",
                            lambda token=None: _HealthPro())
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260201,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        return result, reconcile_calls

    def test_raw_step_defers_reconcile_and_chain_runs_exactly_one(
            self, tmp_path, monkeypatch):
        """The real raw step runs with skip_reconcile_status=True: its
        reconcile entry is the deferred placeholder and _reconcile_after_sync
        is invoked exactly once — by the final chain step."""
        result, reconcile_calls = self._chain(tmp_path, monkeypatch)
        assert result["status"] == "success"
        assert result["raw"]["reconcile"]["status"] == "deferred"
        assert result["raw"]["reconcile"]["reason"] == "chain_reconcile_at_end"
        assert reconcile_calls == ["reconcile"]
        assert result["reconcile"]["status"] == "published"

    def test_standalone_incremental_still_reconciles(self, tmp_path, monkeypatch):
        """skip_reconcile_status=False (standalone run) keeps the immediate
        reconcile and the status demotion logic."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        reconcile_calls = []

        def counting_reconcile(store, *, dry_run=False):
            reconcile_calls.append("reconcile")
            return {"status": "up_to_date", "missing": [], "issues": []}

        monkeypatch.setattr(smd, "_reconcile_after_sync", counting_reconcile)
        monkeypatch.setattr(
            smd, "_sync_dataset",
            lambda **kw: {"dataset_id": f"ds_{kw['adjustment'].value}",
                          "success": 1, "total": 1, "status": "ready"})

        class _HealthPro:
            def health_check(self):
                return True

            def fetch_universe(self, **kw):
                return []

        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider",
                            lambda token=None: _HealthPro())
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260201,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None,
        )
        result = smd.sync_tushare_incremental(args, store)
        assert reconcile_calls == ["reconcile"]
        assert result["reconcile"]["status"] == "up_to_date"
        assert result["status"] == "success"
