# -*- coding: utf-8 -*-
"""Gate C phase 2: tdxquant/front full-sync CLI engine (all mocked, no client).

Covers: symbol mapping, universe partition rules, ingest quality gate,
lock exclusivity, checkpoint refusals (flag/universe/root/version), resume
skip-completed with original sync_run_id, strict ready policy (no_data
allowlist, failed -> partial), manifest provenance.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

import scripts.sync_market_data as smd
import wtpy.apps.astock.data.providers.tdxquant as tdx_provider_mod
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.providers.base import MarketBar, ProviderError
from wtpy.apps.astock.data.sync_lock import SyncTaskLock

UNI_FIELDS = ["symbol", "canonical_symbol", "exchange", "board",
              "inclusion_status", "present_in_latest_year"]


def _write_universe(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=UNI_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _urow(sym, canon, exch, present="True", status="included", board="x"):
    return {"symbol": sym, "canonical_symbol": canon, "exchange": exch,
            "board": board, "inclusion_status": status,
            "present_in_latest_year": present}


DEFAULT_ROWS = [
    _urow("000001.SZ", "SZSE.STK.000001", "SZSE"),
    _urow("600000.SH", "SSE.STK.600000", "SSE"),
    _urow("920002.BJ", "BSE.STK.920002", "BSE"),
    _urow("301107.SZ", "SZSE.STK.301107", "SZSE"),
]


def _args(universe_file, **over):
    base = dict(adjustment="front", period=None, universe_file=str(universe_file),
                batch_size=2, batch_pause=0.0, end_date=20260726,
                resume=False, fresh=False, allow_no_data_file=None,
                max_no_data_count=0, max_no_data_ratio=0.0,
                coverage_out=None, log_path=None, report_path=None,
                tdx_root="X:/none", symbol=None)
    base.update(over)
    return argparse.Namespace(**base)


def _mk_bars(tdx_sym, n=3, base_price=10.0, start=20260720):
    bars = []
    dates = [20260720, 20260721, 20260722, 20260723, 20260724]
    for i in range(n):
        p = base_price + i
        bars.append(MarketBar(symbol=tdx_sym, trade_date=dates[i], period="1d",
                              open=p, high=p + 0.5, low=p - 0.5, close=p + 0.2,
                              volume=1000.0, amount=10000.0,
                              source="tdxquant", adjustment="front"))
    return bars


class FakeProvider:
    """Stands in for TdxQuantProvider inside sync_tdxquant_front_full."""

    instances = []

    def __init__(self, tdx_root=None, batch_size=10, *, no_data=(), fail=()):
        self.no_data = set(no_data)
        self.fail = set(fail)
        self.health_called = False
        self.fetched = []
        self._tq = argparse.Namespace(get_market_data=lambda *a, **k: None)
        FakeProvider.instances.append(self)

    def health_check(self):
        self.health_called = True
        return True

    def tqcenter_version(self):
        return "1.0.3"

    def provider_version(self):
        return "tdxquant_tqcenter_1.0.3"

    def fetch_bars(self, request):
        self._tq.get_market_data()  # counted by the sync wrapper
        bars = []
        for s in request.symbols:
            self.fetched.append(s)
            if s in self.fail:
                raise ProviderError(f"boom {s}")
            if s in self.no_data:
                continue
            bars.extend(_mk_bars(s))
        return bars


@pytest.fixture
def fake_provider_cls(monkeypatch):
    FakeProvider.instances = []

    def factory(no_data=(), fail=()):
        def ctor(tdx_root=None, batch_size=10):
            return FakeProvider(tdx_root, batch_size, no_data=no_data, fail=fail)
        monkeypatch.setattr(tdx_provider_mod, "TdxQuantProvider", ctor)
        return ctor
    return factory


class TestHelpers:
    def test_to_tdx_code(self):
        assert smd._to_tdx_code("SSE.STK.600000") == "600000.SH"
        assert smd._to_tdx_code("SZSE.STK.000001") == "000001.SZ"
        assert smd._to_tdx_code("BSE.STK.920002") == "920002.BJ"
        assert smd._to_tdx_code("whatever") == "whatever"

    def test_partition_universe_rules(self):
        rows = [
            _urow("000001.SZ", "SZSE.STK.000001", "SZSE", present="True"),
            _urow("430047.BJ", "BSE.STK.430047", "BSE", present="False"),
            _urow("920680.BJ", "BSE.STK.920680", "BSE", present="False"),
            _urow("000627.SZ", "SZSE.STK.000627", "SZSE", present="False"),
            _urow("999999.SH", "SSE.STK.999999", "SSE", status="excluded"),
        ]
        eligible, excluded = smd._tdx_partition_universe(rows)
        assert [s for s, _ in eligible] == ["SZSE.STK.000001"]
        reasons = {s: r for s, _, r in excluded}
        assert reasons["BSE.STK.430047"] == "bse_legacy_code_migrated_to_920_segment"
        assert reasons["BSE.STK.920680"] == (
            "absent_latest_vendor_year_delisted_no_provider_data")
        assert reasons["SZSE.STK.000627"] == (
            "absent_latest_vendor_year_delisted_no_provider_data")
        assert "SSE.STK.999999" not in reasons

    def test_validate_symbol_bars_quality_gate(self):
        rep = dataclasses.replace
        good = _mk_bars("000001.SZ")
        assert smd._validate_symbol_bars(good) is None
        dup = _mk_bars("000001.SZ")
        dup[1] = rep(dup[1], trade_date=dup[0].trade_date)
        assert smd._validate_symbol_bars(dup) == "duplicate_trade_date"
        desc = _mk_bars("000001.SZ")
        desc[0], desc[2] = (rep(desc[0], trade_date=desc[2].trade_date),
                            rep(desc[2], trade_date=desc[0].trade_date))
        assert smd._validate_symbol_bars(desc) == "dates_not_ascending"
        neg = _mk_bars("000001.SZ", base_price=-5.0)
        assert smd._validate_symbol_bars(neg) == "nonpositive_price"
        assert smd._validate_symbol_bars(neg, require_positive=False) is None
        hl = _mk_bars("000001.SZ")
        hl[0] = rep(hl[0], high=hl[0].low - 1)
        assert smd._validate_symbol_bars(hl, require_positive=False) == "high_below_low"
        oc = _mk_bars("000001.SZ")
        oc[0] = rep(oc[0], open=oc[0].high + 5)
        assert smd._validate_symbol_bars(
            oc, require_positive=False) == "open_close_outside_range"
        nan = _mk_bars("000001.SZ")
        nan[0] = rep(nan[0], close=float("nan"))
        assert smd._validate_symbol_bars(nan, require_positive=False) == "nan_price"


class TestFullSync:
    def test_happy_path_publishes_ready(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        store = DatasetStore(tmp_path / "md")
        r = smd.sync_tdxquant_front_full(_args(uni), store)
        assert r["status"] == "success"
        assert r["dataset_status"] == "ready"
        m = store.load_manifest(r["dataset_id"])
        assert m.source == "tdxquant" and m.adjustment == "front"
        assert m.dataset_type == "bars"
        assert m.survivorship_bias is True
        assert m.imported_symbol_count == 4 and m.failed_symbol_count == 0
        assert m.universe_sha256 and m.content_hash
        assert m.provider_versions.get("tqcenter") == "1.0.3"
        assert m.provenance.get("silent_fallback") is False
        assert m.provenance.get("fill_data") is False
        assert m.provenance.get("provider_called_only_during_sync") is True
        # checkpoint removed after publish
        assert not smd._tdx_checkpoint_path(store).exists()

    def test_unexpected_no_data_blocks_ready(self, tmp_path, fake_provider_cls):
        fake_provider_cls(no_data={"920002.BJ"})
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        store = DatasetStore(tmp_path / "md")
        r = smd.sync_tdxquant_front_full(_args(uni), store)
        assert r["dataset_status"] == "partial"
        m = store.load_manifest(r["dataset_id"])
        assert m.no_data_symbol_count == 1

    def test_allowlisted_no_data_publishes_ready(self, tmp_path, fake_provider_cls):
        fake_provider_cls(no_data={"920002.BJ"})
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        allow = tmp_path / "allow.csv"
        allow.write_text("symbol,reason\nBSE.STK.920002,delisted_evidence\n",
                         encoding="utf-8-sig")
        store = DatasetStore(tmp_path / "md")
        r = smd.sync_tdxquant_front_full(
            _args(uni, allow_no_data_file=str(allow), max_no_data_count=1), store)
        assert r["dataset_status"] == "ready"
        m = store.load_manifest(r["dataset_id"])
        assert m.warning_symbol_count == 1
        assert m.no_data_allowlist[0]["symbol"] == "BSE.STK.920002"

    def test_provider_failure_classified_and_partial(self, tmp_path, fake_provider_cls):
        fake_provider_cls(fail={"600000.SH"})
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        store = DatasetStore(tmp_path / "md")
        r = smd.sync_tdxquant_front_full(_args(uni), store)
        assert r["dataset_status"] == "partial"
        m = store.load_manifest(r["dataset_id"])
        assert m.failed_symbol_count == 1
        bad = [s for s in m.symbols if s.symbol == "SSE.STK.600000"][0]
        assert bad.quality == "error" and "boom" in bad.error
        # other symbols still imported
        assert m.imported_symbol_count == 3
        assert r["stats"]["retries"] >= smd.TDX_MAX_SINGLE_RETRIES

    def test_lock_exclusive_before_provider_init(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        store = DatasetStore(tmp_path / "md")
        holder = SyncTaskLock(store.root, source="tdxquant", adjustment="front",
                              period="1d", sync_run_id="holder")
        holder.acquire()
        try:
            r = smd.sync_tdxquant_front_full(_args(uni), store)
        finally:
            holder.release()
        assert r["status"] == "failed" and r["error"] == "concurrent_lock"
        assert FakeProvider.instances == []  # zero provider construction
        assert not list((store.root / "manifests").glob("*.json"))


class TestCheckpointResume:
    def _mk_store_uni(self, tmp_path):
        uni = tmp_path / "u.csv"
        _write_universe(uni, DEFAULT_ROWS)
        store = DatasetStore(tmp_path / "md")
        return store, uni

    def _eligible_hash(self, uni):
        rows = smd._load_universe_rows(uni)
        eligible, _ = smd._tdx_partition_universe(rows)
        syms = sorted(s for s, _ in eligible)
        return hashlib.sha256(",".join(syms).encode()).hexdigest(), syms

    def test_existing_checkpoint_without_flag_refused(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        store, uni = self._mk_store_uni(tmp_path)
        smd._tdx_checkpoint_path(store).write_text(
            json.dumps({"checkpoint_version": smd.TDX_CHECKPOINT_VERSION,
                        "sync_run_id": "x", "done": {}}), encoding="utf-8")
        r = smd.sync_tdxquant_front_full(_args(uni), store)
        assert r["error"] == "checkpoint_exists_use_resume_or_fresh"

    def test_resume_universe_mismatch_refused(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        store, uni = self._mk_store_uni(tmp_path)
        ck = {"checkpoint_version": smd.TDX_CHECKPOINT_VERSION,
              "sync_run_id": "x", "universe_hash": "DIFFERENT",
              "market_data_root": str(store.root), "done": {}}
        smd._tdx_checkpoint_path(store).write_text(json.dumps(ck), encoding="utf-8")
        r = smd.sync_tdxquant_front_full(_args(uni, resume=True), store)
        assert r["error"] == "checkpoint_universe_mismatch"

    def test_resume_root_mismatch_refused(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        store, uni = self._mk_store_uni(tmp_path)
        h, _ = self._eligible_hash(uni)
        ck = {"checkpoint_version": smd.TDX_CHECKPOINT_VERSION,
              "sync_run_id": "x", "universe_hash": h,
              "market_data_root": r"Z:\other\root", "done": {}}
        smd._tdx_checkpoint_path(store).write_text(json.dumps(ck), encoding="utf-8")
        r = smd.sync_tdxquant_front_full(_args(uni, resume=True), store)
        assert r["error"] == "checkpoint_root_mismatch"

    def test_resume_version_mismatch_refused(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        store, uni = self._mk_store_uni(tmp_path)
        h, _ = self._eligible_hash(uni)
        ck = {"checkpoint_version": "tdx_ck_v0", "sync_run_id": "x",
              "universe_hash": h, "market_data_root": str(store.root), "done": {}}
        smd._tdx_checkpoint_path(store).write_text(json.dumps(ck), encoding="utf-8")
        r = smd.sync_tdxquant_front_full(_args(uni, resume=True), store)
        assert r["error"] == "checkpoint_version_mismatch"

    def test_resume_skips_completed_and_keeps_run_id(self, tmp_path, fake_provider_cls):
        fake_provider_cls()
        store, uni = self._mk_store_uni(tmp_path)
        h, syms = self._eligible_hash(uni)
        # pre-complete the first two symbols with REAL blobs
        done = {}
        for canon in syms[:2]:
            bars = _mk_bars(smd._to_tdx_code(canon))
            sha = store.store_bars(canon, bars)
            done[canon] = {"status": "ok", "blob_sha256": sha, "rows": len(bars),
                           "first_date": bars[0].trade_date,
                           "last_date": bars[-1].trade_date}
        ck = {"checkpoint_version": smd.TDX_CHECKPOINT_VERSION,
              "sync_run_id": "tdxfront_ORIG_RUN", "universe_hash": h,
              "universe_sha256": "u", "market_data_root": str(store.root),
              "batch_size": 2, "eligible_count": len(syms),
              "stats": {"provider_calls": 7, "retries": 0, "batch_fallbacks": 0},
              "done": done}
        smd._tdx_checkpoint_path(store).write_text(json.dumps(ck), encoding="utf-8")
        r = smd.sync_tdxquant_front_full(_args(uni, resume=True), store)
        assert r["status"] == "success" and r["dataset_status"] == "ready"
        assert r["sync_run_id"] == "tdxfront_ORIG_RUN"
        m = store.load_manifest(r["dataset_id"])
        assert m.sync_run_id == "tdxfront_ORIG_RUN"
        assert m.imported_symbol_count == len(syms)
        prov = FakeProvider.instances[0]
        fetched_canon = {smd._normalize_symbol(s) for s in prov.fetched}
        assert fetched_canon == set(syms[2:])  # completed symbols never refetched
