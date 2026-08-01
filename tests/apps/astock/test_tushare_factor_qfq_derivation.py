# -*- coding: utf-8 -*-
"""derive_tushare_factor_qfq: raw local_vendor bars x tushare adj_factor.

Offline-only: synthetic raw/factor datasets in a tmp DatasetStore, no
providers, no network, no E:\\AStockData.
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

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

SYM_A = "SSE.STK.600000"
SYM_B = "SZSE.STK.000001"

# 10 consecutive workdays (2024-01-01 Mon .. 2024-01-12 Fri)
DATES = [
    20240101, 20240102, 20240103, 20240104, 20240105,
    20240108, 20240109, 20240110, 20240111, 20240112,
]


@pytest.fixture(scope="module")
def sync_mod():
    spec = importlib.util.spec_from_file_location(
        "sync_market_data_qfq_under_test", str(SYNC_SCRIPT)
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


def make_raw(store, sym_arrays, dataset_id="localvendor_none_1d_20240112_raw001",
             source="local_vendor", adjustment="none"):
    records = []
    total = 0
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
        period="1d", status="building",
        universe_file="vendor_universe_v1.csv", universe_sha256="u" * 64,
        symbols=records, symbol_count=len(records), row_count=total,
    )
    store.publish(m)
    return dataset_id


def make_factor(store, sym_factors, dataset_id="tushare_adjfactor_1d_20240112_fac001",
                status=None, dataset_type="factor"):
    records = []
    for sym, (dates, factors) in sym_factors.items():
        sha = store.store_factors(sym, dates, factors)
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=len(dates), quality="ok",
        ))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", status="building", dataset_type=dataset_type,
        universe_file="vendor_universe_v1.csv", universe_sha256="u" * 64,
        symbols=records, symbol_count=len(records),
    )
    if status:
        m.status = status
    store.publish(m)
    return dataset_id


def derive(sync_mod, store, raw_id, fac_id, cutoff=None):
    args = SimpleNamespace(
        raw_dataset_id=raw_id, factor_dataset_id=fac_id, cutoff=cutoff,
        log_path=None, report_path=None,
    )
    return sync_mod.derive_tushare_factor_qfq(args, store)


def _sym_record(manifest, sym):
    rec = next((r for r in manifest.symbols if r.symbol == sym), None)
    assert rec is not None, f"{sym} not in derived manifest"
    return rec


def _std_env(store):
    """Raw for A+B over DATES; A has a split-style factor step, B constant."""
    raw_id = make_raw(store, {
        SYM_A: _price_arrays(DATES, 10.0),
        SYM_B: _price_arrays(DATES, 20.0),
    })
    fac_id = make_factor(store, {
        SYM_A: ([20240101, 20240108], [1.0, 1.25]),
        SYM_B: ([20240101], [1.1]),
    })
    return raw_id, fac_id


class TestNormalDerivation:
    def test_qfq_prices_and_unadjusted_volume(self, sync_mod, store):
        raw_id, fac_id = _std_env(store)
        res = derive(sync_mod, store, raw_id, fac_id)
        assert res["status"] == "success"
        assert res["dataset_status"] == "ready"

        m = store.load_manifest(res["dataset_id"])
        assert m is not None and m.status == "ready"

        raw_a = _price_arrays(DATES, 10.0)
        rec_a = _sym_record(m, SYM_A)
        got_a = store.load_bars(rec_a.blob_sha256)
        np.testing.assert_array_equal(got_a["trade_date"], raw_a["trade_date"])

        # mirror production math exactly (asof factor / anchor)
        fd = np.array([20240101, 20240108], dtype=np.int64)
        fv = np.array([1.0, 1.25])
        cutoff = 20240112  # defaults to max raw last_date
        anchor = fv[int(np.searchsorted(fd, cutoff, side="right")) - 1]
        pos = np.searchsorted(fd, raw_a["trade_date"], side="right") - 1
        ratio = fv[pos] / anchor
        for field in ("open", "high", "low", "close"):
            np.testing.assert_allclose(
                got_a[field], np.round(raw_a[field] * ratio, 4), atol=1e-9,
            )
        # spot checks per spec: pre-step day scaled by 1.0/1.25, post-step 1:1
        assert got_a["close"][0] == pytest.approx(
            round(raw_a["close"][0] * (1.0 / 1.25), 4))
        post = raw_a["trade_date"] >= 20240108
        np.testing.assert_allclose(
            got_a["close"][post], np.round(raw_a["close"][post] * 1.0, 4),
            atol=1e-9,
        )
        # volume / amount copied unchanged (never adjusted)
        np.testing.assert_array_equal(got_a["volume"], raw_a["volume"])
        np.testing.assert_array_equal(got_a["amount"], raw_a["amount"])

        # B: constant factor -> anchor == asof -> ratio 1 -> prices == raw
        raw_b = _price_arrays(DATES, 20.0)
        rec_b = _sym_record(m, SYM_B)
        got_b = store.load_bars(rec_b.blob_sha256)
        for field in ("open", "high", "low", "close"):
            np.testing.assert_allclose(
                got_b[field], np.round(raw_b[field], 4), atol=1e-9)
        np.testing.assert_array_equal(got_b["volume"], raw_b["volume"])
        np.testing.assert_array_equal(got_b["amount"], raw_b["amount"])

    def test_manifest_lineage(self, sync_mod, store):
        raw_id, fac_id = _std_env(store)
        res = derive(sync_mod, store, raw_id, fac_id)
        m = store.load_manifest(res["dataset_id"])
        assert m.source == "internal"
        assert m.adjustment == "tushare_factor_qfq"
        assert m.raw_dataset_id == raw_id
        assert m.factor_dataset_id == fac_id
        assert len(m.raw_dataset_sha256) == 64
        assert len(m.factor_dataset_sha256) == 64
        assert m.raw_source == "local_vendor"
        assert m.factor_source == "tushare"
        assert m.formula_version == "tsqfq_v1"
        assert m.formula_version == sync_mod.QFQ_FORMULA_VERSION
        assert m.anchor_policy == "last_factor_on_or_before_cutoff"
        assert m.anchor_policy == sync_mod.QFQ_ANCHOR_POLICY
        assert m.survivorship_bias is True
        assert m.universe_sha256 == "u" * 64


class TestParentValidation:
    def test_partial_factor_rejected(self, sync_mod, store):
        raw_id = make_raw(store, {SYM_A: _price_arrays(DATES, 10.0)})
        fac_id = make_factor(
            store, {SYM_A: ([20240101], [1.0])}, status="partial")
        res = derive(sync_mod, store, raw_id, fac_id)
        assert res["status"] == "failed"
        assert res["error"] == "factor_not_ready"

    def test_raw_wrong_source_rejected(self, sync_mod, store):
        raw_id = make_raw(
            store, {SYM_A: _price_arrays(DATES, 10.0)},
            dataset_id="tdxquant_front_1d_20240112_bad001",
            source="tdxquant", adjustment="front",
        )
        fac_id = make_factor(store, {SYM_A: ([20240101], [1.0])})
        res = derive(sync_mod, store, raw_id, fac_id)
        assert res["status"] == "failed"
        assert res["error"] == "raw_parent_wrong_source"

    def test_non_factor_dataset_type_rejected(self, sync_mod, store):
        raw_id = make_raw(store, {SYM_A: _price_arrays(DATES, 10.0)})
        fac_id = make_factor(
            store, {SYM_A: ([20240101], [1.0])},
            dataset_id="tushare_adjfactor_1d_20240112_notfac",
            dataset_type="bars",
        )
        res = derive(sync_mod, store, raw_id, fac_id)
        assert res["status"] == "failed"
        assert res["error"] == "not_a_factor_dataset"


class TestAnchorAndGaps:
    def test_no_anchor_symbol_fails_dataset_partial(self, sync_mod, store):
        raw_id = make_raw(store, {
            SYM_A: _price_arrays(DATES, 10.0),
            SYM_B: _price_arrays(DATES, 20.0),
        })
        # B's only factor date is after cutoff -> no anchor for B
        fac_id = make_factor(store, {
            SYM_A: ([20240101], [1.0]),
            SYM_B: ([20240108], [1.25]),
        })
        res = derive(sync_mod, store, raw_id, fac_id, cutoff=20240105)
        assert res["status"] == "success"
        assert res["dataset_status"] == "partial"
        m = store.load_manifest(res["dataset_id"])
        assert m.status == "partial"
        rec_b = _sym_record(m, SYM_B)
        assert rec_b.quality == "error"
        assert rec_b.error == "no_anchor_factor"
        assert _sym_record(m, SYM_A).quality == "ok"
        assert any(
            i.get("symbol") == SYM_B and i.get("issue") == "no_anchor_factor"
            for i in res["issues"]
        )

    def test_leading_gap_rows_dropped_and_recorded(self, sync_mod, store):
        raw_id = make_raw(store, {SYM_A: _price_arrays(DATES, 10.0)})
        # first factor date 20240103 -> raw 20240101/20240102 have no asof factor
        fac_id = make_factor(store, {SYM_A: ([20240103], [1.0])})
        res = derive(sync_mod, store, raw_id, fac_id)
        assert res["status"] == "success"
        assert res["dataset_status"] == "ready"
        m = store.load_manifest(res["dataset_id"])
        rec = _sym_record(m, SYM_A)
        assert rec.quality == "ok"
        assert rec.first_date == 20240103
        assert rec.row_count == len(DATES) - 2
        gap = [i for i in res["issues"]
               if i.get("issue") == "leading_gap_rows_dropped"]
        assert gap and gap[0]["symbol"] == SYM_A and gap[0]["detail"] == 2

    def test_never_backfills_future_factor(self, sync_mod, store):
        raw_id = make_raw(store, {SYM_A: _price_arrays(DATES, 10.0)})
        # single factor 1.2 at 20240105; rows before it must be DROPPED,
        # never scaled by the (future) 1.2 value.
        fac_id = make_factor(store, {SYM_A: ([20240105], [1.2])})
        res = derive(sync_mod, store, raw_id, fac_id, cutoff=20240112)
        assert res["dataset_status"] == "ready"
        m = store.load_manifest(res["dataset_id"])
        rec = _sym_record(m, SYM_A)
        assert rec.first_date == 20240105
        assert rec.row_count == len(DATES) - 4
        gap = [i for i in res["issues"]
               if i.get("issue") == "leading_gap_rows_dropped"]
        assert gap and gap[0]["detail"] == 4
        # surviving rows: asof == anchor == 1.2 -> ratio 1 -> raw prices
        raw = _price_arrays(DATES, 10.0)
        got = store.load_bars(rec.blob_sha256)
        keep = raw["trade_date"] >= 20240105
        np.testing.assert_array_equal(got["trade_date"], raw["trade_date"][keep])
        np.testing.assert_allclose(
            got["close"], np.round(raw["close"][keep], 4), atol=1e-9)

    def test_weekend_cutoff_uses_last_factor_on_or_before(self, sync_mod, store):
        raw_id, fac_id = _std_env(store)
        # 20240113 is a Saturday (not a raw trade date)
        res = derive(sync_mod, store, raw_id, fac_id, cutoff=20240113)
        assert res["status"] == "success"
        assert res["dataset_status"] == "ready"
        m = store.load_manifest(res["dataset_id"])
        assert m.data_cutoff_date == 20240113
        rec = _sym_record(m, SYM_A)
        got = store.load_bars(rec.blob_sha256)
        # anchor is still the 20240108 factor (1.25)
        assert got["close"][0] == pytest.approx(round(10.0 * (1.0 / 1.25), 4))


class TestFactorRevision:
    def test_revised_factor_produces_new_dataset_and_prices(self, sync_mod, store):
        raw_id = make_raw(store, {SYM_A: _price_arrays(DATES, 10.0)})
        fac_v1 = make_factor(
            store, {SYM_A: ([20240101, 20240108], [1.0, 1.25])},
            dataset_id="tushare_adjfactor_1d_20240112_facv1")
        fac_v2 = make_factor(
            store, {SYM_A: ([20240101, 20240108], [1.0, 1.30])},
            dataset_id="tushare_adjfactor_1d_20240112_facv2")

        res1 = derive(sync_mod, store, raw_id, fac_v1)
        res2 = derive(sync_mod, store, raw_id, fac_v2)
        assert res1["dataset_status"] == "ready"
        assert res2["dataset_status"] == "ready"
        assert res1["dataset_id"] != res2["dataset_id"]

        m1 = store.load_manifest(res1["dataset_id"])
        m2 = store.load_manifest(res2["dataset_id"])
        assert m1.factor_dataset_id == fac_v1
        assert m2.factor_dataset_id == fac_v2
        assert m1.factor_dataset_sha256 != m2.factor_dataset_sha256

        c1 = store.load_bars(_sym_record(m1, SYM_A).blob_sha256)["close"]
        c2 = store.load_bars(_sym_record(m2, SYM_A).blob_sha256)["close"]
        assert c1[0] == pytest.approx(round(10.0 * (1.0 / 1.25), 4))
        assert c2[0] == pytest.approx(round(10.0 * (1.0 / 1.30), 4))
        assert c1[0] != c2[0]
