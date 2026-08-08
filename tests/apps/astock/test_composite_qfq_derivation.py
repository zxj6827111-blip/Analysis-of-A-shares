# -*- coding: utf-8 -*-
"""Gate B6: derive_composite_tushare_factor_qfq — composite raw × factor
parents (main + supplement) with BSE pre-migration alias resolution.

Offline-only: synthetic datasets in a tmp DatasetStore, no providers, no
network, no E:\\AStockData.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.pit_universe import InstrumentWindow, PointInTimeUniverse

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

SYM_MAIN = "SSE.STK.600000"       # factor in main dataset
SYM_SUPP = "SZSE.STK.300104"      # delisted, factor only in supplement
SYM_OLD = "BSE.STK.430017"        # pre-migration code, factor via 920 alias
SYM_NEW = "BSE.STK.920017"        # post-migration twin (in main factor ds)
SYM_NOFAC = "SZSE.STK.000999"     # no factor anywhere

DATES = [
    20240101, 20240102, 20240103, 20240104, 20240105,
    20240108, 20240109, 20240110, 20240111, 20240112,
]
CUTOFF = 20240112


@pytest.fixture(scope="module")
def sync_mod():
    spec = importlib.util.spec_from_file_location(
        "sync_market_data_cqfq_under_test", str(SYNC_SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


def _price_arrays(dates, base):
    n = len(dates)
    close = np.array([base + i for i in range(n)], dtype=np.float64)
    return {
        "trade_date": np.array(dates, dtype=np.int64),
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": np.array([1000.0 + 10 * i for i in range(n)]),
        "amount": np.array([100000.0 + 100 * i for i in range(n)]),
    }


def _make_bars_ds(store, sym_arrays, *, dataset_id, source, adjustment):
    records, total = [], 0
    for sym, arrays in sym_arrays.items():
        sha = store.store_bar_arrays(sym, arrays)
        d = arrays["trade_date"]
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(d[0]),
            last_date=int(d[-1]), row_count=len(d), quality="ok",
        ))
        total += len(d)
    m = DatasetManifest(
        dataset_id=dataset_id, source=source, adjustment=adjustment,
        period="1d", status="building", symbols=records,
        symbol_count=len(records), row_count=total,
    )
    m.status = "ready"
    store.save_manifest(m)
    return m


def _make_factor_ds(store, sym_factors, *, dataset_id):
    records = []
    for sym, (dates, factors) in sym_factors.items():
        sha = store.store_factors(sym, dates, factors)
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=len(dates), quality="ok",
        ))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", dataset_type="factor", status="building",
        symbols=records, symbol_count=len(records),
        row_count=sum(r.row_count for r in records),
    )
    m.status = "ready"
    store.save_manifest(m)
    return m


def _make_universe(store, *, cutoff=CUTOFF):
    windows = [
        InstrumentWindow(
            canonical_symbol=SYM_NEW, ts_code="920017.BJ", exchange="BSE",
            board="bse", name="bse-migrated", list_status="L",
            list_date=20200101, delist_date=None, last_trade_date=None,
            aliases=[SYM_OLD],
        ),
    ]
    pit = PointInTimeUniverse.build(windows, cutoff=cutoff, built_from={"test": True})
    pit.save(store.root)
    return pit


def _args(store, raw_id, fac_id, sup_id=None, uni_id=None, cutoff=CUTOFF):
    return SimpleNamespace(
        raw_dataset_id=raw_id,
        factor_dataset_id=fac_id,
        supplement_factor_dataset_id=sup_id,
        universe_dataset_id_arg=uni_id,
        cutoff=cutoff,
        log_path=None,
        report_path=None,
    )


def _standard_setup(store):
    raw = _make_bars_ds(
        store,
        {
            SYM_MAIN: _price_arrays(DATES, 10.0),
            SYM_SUPP: _price_arrays(DATES[:6], 20.0),
            SYM_OLD: _price_arrays(DATES, 30.0),
            SYM_NOFAC: _price_arrays(DATES, 40.0),
        },
        dataset_id="internal_composite_none_1d_20240112_raw001",
        source="internal", adjustment="composite_none",
    )
    fac = _make_factor_ds(
        store,
        {
            SYM_MAIN: (DATES, [1.0] * 5 + [2.0] * 5),
            SYM_NEW: (DATES, [1.0] * 8 + [4.0] * 2),
        },
        dataset_id="tushare_adjfactor_1d_20240112_fac001",
    )
    sup = _make_factor_ds(
        store,
        {SYM_SUPP: (DATES[:6], [1.0] * 6)},
        dataset_id="tushare_adjfactor_1d_20240112_sup001",
    )
    pit = _make_universe(store)
    return raw, fac, sup, pit


class TestCompositeQfqDerivation:
    def test_full_resolution_and_status_partial_on_missing(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        assert r["status"] == "success"
        # SYM_NOFAC has no factor -> recorded, dataset partial (strict policy)
        assert r["result"]["missing_factor"] == 1
        assert r["dataset_status"] == "partial"
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        assert rec[SYM_NOFAC].quality == "no_data"
        assert rec[SYM_NOFAC].error == "missing_factor"
        assert rec[SYM_NOFAC].blob_sha256 == ""  # raw NEVER substituted
        assert SYM_NOFAC in m.provenance["missing_factor_symbols"]

    def test_factor_source_resolution(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        counts = r["result"]["factor_source_counts"]
        assert counts["main"] == 1          # SYM_MAIN
        assert counts["supplement"] == 1    # SYM_SUPP
        assert counts["alias_main"] == 1    # SYM_OLD via SYM_NEW
        m = store.load_manifest(r["dataset_id"])
        assert m.provenance["alias_factor_symbols"] == [SYM_OLD]
        assert m.provenance["supplement_factor_symbols"] == [SYM_SUPP]

    def test_qfq_math_matches_tsqfq_v1(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        arr = store.load_bars(rec[SYM_MAIN].blob_sha256)
        # anchor = last factor <= cutoff = 2.0; ratio = 1.0/2.0 then 2.0/2.0
        raw_close = np.array([10.0 + i for i in range(10)])
        expect = np.round(
            raw_close * np.array([0.5] * 5 + [1.0] * 5), 4)
        assert np.allclose(arr["close"], expect)
        # volume / amount copied unchanged
        assert np.allclose(arr["volume"], [1000.0 + 10 * i for i in range(10)])
        assert np.allclose(arr["amount"], [100000.0 + 100 * i for i in range(10)])

    def test_alias_uses_920_factor_series(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        arr = store.load_bars(rec[SYM_OLD].blob_sha256)
        # 920 factor: 1.0×8, 4.0×2 -> anchor 4.0; ratios 0.25×8 then 1.0×2
        raw_close = np.array([30.0 + i for i in range(10)])
        expect = np.round(raw_close * np.array([0.25] * 8 + [1.0] * 2), 4)
        assert np.allclose(arr["close"], expect)

    def test_no_future_factor_leading_gap_dropped(self, sync_mod, store):
        # factor series starts mid-way: earlier bars have no asof factor
        raw = _make_bars_ds(
            store, {SYM_MAIN: _price_arrays(DATES, 10.0)},
            dataset_id="internal_composite_none_1d_20240112_raw002",
            source="internal", adjustment="composite_none",
        )
        fac = _make_factor_ds(
            store, {SYM_MAIN: (DATES[3:], [1.0] * 7)},
            dataset_id="tushare_adjfactor_1d_20240112_fac002",
        )
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id), store)
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        assert rec[SYM_MAIN].first_date == DATES[3]
        assert rec[SYM_MAIN].row_count == 7
        assert any(i["issue"] == "leading_gap_rows_dropped" for i in r["issues"])

    def test_anchor_ignores_factors_after_cutoff(self, sync_mod, store):
        # factors continue past cutoff: anchor must stay at cutoff factor
        dates_ext = DATES + [20240115]
        raw = _make_bars_ds(
            store, {SYM_MAIN: _price_arrays(DATES, 10.0)},
            dataset_id="internal_composite_none_1d_20240112_raw003",
            source="internal", adjustment="composite_none",
        )
        fac = _make_factor_ds(
            store, {SYM_MAIN: (dates_ext, [1.0] * 10 + [99.0])},
            dataset_id="tushare_adjfactor_1d_20240115_fac003",
        )
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, cutoff=CUTOFF), store)
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        arr = store.load_bars(rec[SYM_MAIN].blob_sha256)
        # anchor = 1.0 (last factor <= 20240112), NOT 99.0
        raw_close = np.array([10.0 + i for i in range(10)])
        assert np.allclose(arr["close"], np.round(raw_close, 4))

    def test_rejects_wrong_raw_parent(self, sync_mod, store):
        raw = _make_bars_ds(
            store, {SYM_MAIN: _price_arrays(DATES, 10.0)},
            dataset_id="localvendor_none_1d_20240112_raw004",
            source="local_vendor", adjustment="none",
        )
        fac = _make_factor_ds(
            store, {SYM_MAIN: (DATES, [1.0] * 10)},
            dataset_id="tushare_adjfactor_1d_20240112_fac004",
        )
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id), store)
        assert r["status"] == "failed"
        assert r["error"] == "raw_parent_wrong_source"

    def test_rejects_non_ready_factor(self, sync_mod, store):
        raw = _make_bars_ds(
            store, {SYM_MAIN: _price_arrays(DATES, 10.0)},
            dataset_id="internal_composite_none_1d_20240112_raw005",
            source="internal", adjustment="composite_none",
        )
        fac = _make_factor_ds(
            store, {SYM_MAIN: (DATES, [1.0] * 10)},
            dataset_id="tushare_adjfactor_1d_20240112_fac005",
        )
        fac.status = "building"
        store.save_manifest(fac)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id), store)
        assert r["status"] == "failed"
        assert "not_ready" in r["error"]

    def test_idempotent_rerun_reports_real_stats(self, sync_mod, store):
        """Second derive (idempotent branch) must reconstruct the stats from
        the on-disk manifest instead of returning a zeroed summary — the CLI
        prints "derived: imported/eligible ok, rows=..." from these fields."""
        from wtpy.apps.astock.data.tushare_product import (
            derive_composite_tushare_factor_qfq,
        )

        raw, fac, sup, pit = _standard_setup(store)
        kw = dict(
            raw_dataset_id=raw.dataset_id, factor_dataset_id=fac.dataset_id,
            supplement_factor_dataset_id=sup.dataset_id,
            universe_dataset_id=pit.universe_dataset_id,
        )
        r1 = derive_composite_tushare_factor_qfq(store, **kw)
        assert r1["result"]["imported"] == 3
        assert r1["result"]["rows"] == 26
        r2 = derive_composite_tushare_factor_qfq(store, **kw)
        assert r2["status"] == "success"
        assert r2["idempotent"] is True
        res = r2["result"]
        assert res["imported"] == 3
        assert res["eligible"] == 3
        assert res["rows"] == 26
        assert res["missing_factor"] == 1
        assert res["failed"] == 0
        assert res["factor_source_counts"] == {
            "main": 1, "supplement": 1, "alias_main": 1, "alias_supplement": 0,
        }
        assert res["missing_factor_symbols"] == [SYM_NOFAC]
        assert res["status"] == r1["dataset_status"]

    def test_manifest_lineage_and_versions(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        m = store.load_manifest(r["dataset_id"])
        assert m.adjustment == "composite_tushare_factor_qfq"
        assert m.formula_version == "ctsfqfq_v1"
        assert m.raw_dataset_id == raw.dataset_id
        assert m.factor_dataset_id == fac.dataset_id
        assert m.provenance["supplement_factor_dataset_id"] == sup.dataset_id
        assert len(m.raw_dataset_sha256) == 64
        assert len(m.provenance["supplement_factor_dataset_sha256"]) == 64
        assert m.provenance["universe_dataset_id"] == pit.universe_dataset_id
        assert "factor_resolution_v1" in m.provenance["factor_resolution_rule"]
        assert m.dataset_id.startswith(
            "internal_composite_tushare_factor_qfq_1d_")
        # not claiming native qfq
        assert any("Tushare-native" in u for u in m.prohibited_or_discouraged_use)

    def test_delisted_supplement_produces_signal_bars(self, sync_mod, store):
        raw, fac, sup, pit = _standard_setup(store)
        r = sync_mod.derive_composite_tushare_factor_qfq(
            _args(store, raw.dataset_id, fac.dataset_id, sup.dataset_id,
                  pit.universe_dataset_id), store)
        m = store.load_manifest(r["dataset_id"])
        rec = {s.symbol: s for s in m.symbols}
        # delisted stock (bars end early) still yields QFQ bars through its end
        assert rec[SYM_SUPP].quality == "ok"
        assert rec[SYM_SUPP].row_count == 6
        assert rec[SYM_SUPP].last_date == DATES[5]
