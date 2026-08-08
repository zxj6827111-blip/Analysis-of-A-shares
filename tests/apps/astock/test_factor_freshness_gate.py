# -*- coding: utf-8 -*-
"""P1-4 / P1-5: factor per-symbol freshness gate and auto universe.

P1-4: a factor manifest whose global max date is fresh but whose per-symbol
coverage lags the raw baseline's active stocks must not publish ready.
P1-5: the zero-config chain generates a factor universe from the raw
baseline when no ready factor manifest exists yet (first migration).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
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
SYM_C = "SSE.STK.600036"

# P1-2 full-market raw baseline gates (mirror the sync script constants)
RAW_MIN_SYMBOLS = 500
RAW_MIN_MEDIAN_ROWS = 250
FULL_MARKET_MIN_SYMBOLS = 4000
# Fixture surface size: must satisfy the full-market gate (>= 4000 symbols)
# so the freshness gate still finds a raw baseline in the tests; filler
# symbols carry an OLD last date (auto-exempt from the active-freshness math).
FIXTURE_SYMBOLS = 4200

_MODULE = None


def _script():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "factor_freshness_gate_test", SYNC_SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def _padded_raw_last(symbols_last, *, filler_last=20260101):
    """Grow a small raw surface to FIXTURE_SYMBOLS symbols.

    Filler symbols carry an OLD last date: they satisfy the baseline gates
    (symbol count / median rows) but are auto-exempt from the
    active-freshness computation (suspended-style), so the per-symbol ratio
    math of the original scenarios is preserved.
    """
    out = dict(symbols_last)
    for i in range(FIXTURE_SYMBOLS - len(out)):
        out.setdefault(f"SZSE.STK.{300000 + i}", filler_last)
    return out


def _calendar_dates(start_ymd: int, n: int) -> np.ndarray:
    import datetime as _dt
    start = _dt.datetime.strptime(str(start_ymd), "%Y%m%d").date()
    return np.array(
        [int((start + _dt.timedelta(days=i)).strftime("%Y%m%d"))
         for i in range(n)],
        dtype=np.int64,
    )


def _dates_ending(end_ymd: int, n: int) -> np.ndarray:
    """n ascending calendar dates ENDING at end_ymd."""
    import datetime as _dt
    end = _dt.datetime.strptime(str(end_ymd), "%Y%m%d").date()
    start = end - _dt.timedelta(days=n - 1)
    return np.array(
        [int((start + _dt.timedelta(days=i)).strftime("%Y%m%d"))
         for i in range(n)],
        dtype=np.int64,
    )


def _publish_raw_manifest(store, symbols_last, *, dataset_id="tushare_none_1d_raw",
                          pad=True, n_rows=None):
    """tushare/none ready raw manifest; symbols_last: {symbol: last_date}.

    pad=True (default) grows the surface to a full-market size
    (FIXTURE_SYMBOLS x 260 rows) so the P1-2 raw baseline gate (>= 500
    symbols, >= 250 median rows, full-market symbol anchor) passes; filler
    symbols carry an OLD last date so they are auto-exempt from the
    active-freshness computation. pad=False / n_rows build deliberately tiny
    or orphan surfaces.
    """
    if pad:
        symbols_last = _padded_raw_last(symbols_last)
    if n_rows is None:
        n_rows = RAW_MIN_MEDIAN_ROWS + 10
    records = []
    for sym, last in symbols_last.items():
        dates = _dates_ending(int(last), n_rows)
        sha = store.store_factors(sym, dates, np.full(n_rows, 1.0))
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=n_rows, quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="none",
        period="1d", status="ready", dataset_type="bars", symbols=records,
        symbol_count=len(records), row_count=sum(r.row_count for r in records),
        data_cutoff_date=max(int(r.last_date or 0) for r in records),
    )
    store.publish(m)
    return m


def _publish_factor_manifest(store, symbols_last, *, dataset_id="tushare_adjfactor_x"):
    """tushare/adj_factor factor manifest with per-symbol last dates."""
    records = []
    for sym, last in symbols_last.items():
        dates = _dates_ending(int(last), 300)
        sha = store.store_factors(sym, dates, np.full(300, 1.0))
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=300, quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", status="ready", dataset_type="factor", symbols=records,
        symbol_count=len(records), row_count=sum(r.row_count for r in records),
        data_cutoff_date=max(int(r.last_date or 0) for r in records),
    )
    store.publish(m)
    return m


class TestFreshnessMetrics:
    def test_stale_detected(self, tmp_path):
        """1/3 factor series stale vs raw -> ratio 2/3, stale sample filled."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        fac = _publish_factor_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260701})
        m = smd._factor_freshness_metrics(store, fac, raw)
        assert m is not None
        assert m["fresh_count"] == 2
        assert m["active_count"] == 3
        assert m["fresh_symbol_ratio"] == round(2 / 3, 4)
        stale = m["stale_active_symbols"]
        assert len(stale) == 1
        assert stale[0]["symbol"] == SYM_C
        assert stale[0]["factor_last_date"] == 20260701
        assert stale[0]["raw_last_date"] == 20260804
        assert m["p50_last_date"] == 20260804
        assert m["p10_last_date"] == 20260701
        assert m["raw_dataset_id"] == raw.dataset_id
        assert m["fresh_tolerance_days"] == 3

    def test_all_fresh(self, tmp_path):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        fac = _publish_factor_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        m = smd._factor_freshness_metrics(store, fac)
        assert m["fresh_symbol_ratio"] == 1.0
        assert m["stale_active_symbols"] == []
        assert m["fresh_count"] == m["active_count"] == 3

    def test_no_raw_baseline_returns_none(self, tmp_path):
        """Only factor manifests in the store -> no raw baseline -> None."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_factor_manifest(store, {SYM_A: 20260804})
        assert smd._factor_freshness_metrics(store, None) is None
        assert smd._factor_freshness_metrics(store, None, None) is None

    def test_suspended_symbol_exempt(self, tmp_path):
        """A raw symbol far older than the cutoff (suspended/delisted) is
        not active and never demands a fresh factor series."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260101, SYM_C: 20260804})
        fac = _publish_factor_manifest(store, {
            SYM_A: 20260804, SYM_B: 20250101, SYM_C: 20260804})
        m = smd._factor_freshness_metrics(store, fac, raw)
        assert m["active_count"] == 2  # SYM_B exempt
        assert m["fresh_count"] == 2
        assert m["fresh_symbol_ratio"] == 1.0

    def test_tolerance_allows_one_day_lag(self, tmp_path):
        """adj_factor EOD lag: factor one day behind raw is still fresh."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {SYM_A: 20260804, SYM_B: 20260804})
        fac = _publish_factor_manifest(store, {SYM_A: 20260803, SYM_B: 20260803})
        m = smd._factor_freshness_metrics(store, fac)
        assert m["fresh_tolerance_days"] == 3
        assert m["fresh_symbol_ratio"] == 1.0
        assert m["stale_active_symbols"] == []

    def test_exact_tolerance_boundary_fresh(self, tmp_path):
        """Exactly 3 calendar days behind the raw last date is still fresh."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {SYM_A: 20260804})
        fac = _publish_factor_manifest(store, {SYM_A: 20260801})
        m = smd._factor_freshness_metrics(store, fac)
        assert m["fresh_symbol_ratio"] == 1.0

    def test_beyond_tolerance_stale(self, tmp_path):
        """Factor lagging more than the 3-day tolerance is stale."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {SYM_A: 20260804, SYM_B: 20260804})
        # 20260731 is 4 calendar days before 20260804 -> beyond the cap
        fac = _publish_factor_manifest(store, {SYM_A: 20260731, SYM_B: 20260804})
        m = smd._factor_freshness_metrics(store, fac)
        assert m["fresh_symbol_ratio"] == round(1 / 2, 4)
        assert m["stale_active_symbols"][0]["symbol"] == SYM_A


# ---- gate integration through sync_tushare_adj_factor_full ----

class _FakeFactorPro:
    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []
        self.stock_basic_calls = []

    def adj_factor(self, **kwargs):
        call = dict(kwargs)
        if "ts_code" not in call:
            call["ts_code"] = None
        self.calls.append(call)
        resp = self.responses.get(call["ts_code"])
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def stock_basic(self, list_status="L"):
        self.stock_basic_calls.append(list_status)
        return pd.DataFrame(columns=[
            "ts_code", "name", "list_status", "list_date", "delist_date",
        ])


class _FakeTushareProvider:
    registry = {"pro": None}

    def __init__(self, token=None):
        self._token = token
        self._pro = _FakeTushareProvider.registry["pro"]

    def _ensure_initialized(self):
        pass

    def _call_with_retry(self, fn, **kwargs):
        return fn(**kwargs)

    def fetch_adj_factor(self, ts_code=None, *, start_date=None, end_date=None,
                         trade_date=None):
        kwargs = {}
        if trade_date:
            kwargs["trade_date"] = str(trade_date)
        else:
            kwargs["ts_code"] = ts_code
            if start_date:
                kwargs["start_date"] = str(start_date)
            if end_date:
                kwargs["end_date"] = str(end_date)
        return self._call_with_retry(self._pro.adj_factor, **kwargs)

    def _to_ts_code(self, symbol):
        parts = symbol.split(".")
        exch, _, code = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exch]
        return f"{code}.{suffix}"

    def _from_ts_code(self, ts_code):
        code, suffix = ts_code.split(".")
        exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[suffix.upper()]
        return f"{exch}.STK.{code}"

    def provider_version(self):
        return "fake_factor_provider_v1"


def _factor_df(dates, factors):
    return pd.DataFrame({
        "trade_date": [int(d) for d in dates],
        "adj_factor": [float(f) for f in factors],
    })


def _write_universe(tmp_path, symbols, name="universe.csv"):
    p = tmp_path / name
    p.write_text(
        "canonical_symbol,inclusion_status\n"
        + "\n".join(f"{s},included" for s in symbols) + "\n",
        encoding="utf-8",
    )
    return p


def _factor_args(tmp_path, **over):
    base = dict(
        source="tushare", mode="incremental", adjustment="adj_factor",
        token=None, universe_file=None, start_date=None, end_date=20260804,
        factor_raw_root=str(tmp_path / "factor_raw"),
        rate_per_min=100000, resume=False, fresh=True,
        log_path=None, report_path=None, coverage_out=None,
        include_bse=True, include_delisted=False, anchor_date=None,
        symbol=None, asset_class="stocks", skip_freshness_gate=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestGateIntegration:
    def _run(self, tmp_path, monkeypatch, *, raw_last, parent_last,
             window_rows, symbols, args_overrides=None):
        smd = _script()
        pro = _FakeFactorPro(window_rows)
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        store = DatasetStore(tmp_path / "market_data")
        _publish_raw_manifest(store, raw_last)
        _publish_factor_manifest(store, parent_last)
        uni = _write_universe(tmp_path, symbols)
        args = _factor_args(tmp_path, universe_file=str(uni),
                            **(args_overrides or {}))
        result = smd.sync_tushare_adj_factor_full(args, store)
        return smd, pro, store, args, result

    def test_gate_marks_partial_on_stale(self, tmp_path, monkeypatch):
        """1/3 symbols keep an old parent factor date while raw is fresh ->
        the gate demotes the manifest to partial."""
        symbols = [SYM_A, SYM_B, SYM_C]
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            raw_last={SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            parent_last={SYM_A: 20260731, SYM_B: 20260731, SYM_C: 20260701},
            window_rows={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
                "600036.SH": _factor_df([], []),  # stale: parent retained
            },
            symbols=symbols,
        )
        assert result["dataset_status"] == "partial"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "partial"
        freshness = (m.provenance or {}).get("freshness")
        assert freshness is not None
        assert freshness["fresh_count"] == 2
        assert freshness["active_count"] == 3
        assert freshness["fresh_symbol_ratio"] == round(2 / 3, 4)
        assert freshness["stale_active_symbols"][0]["symbol"] == SYM_C
        assert result["freshness"]["fresh_symbol_ratio"] == round(2 / 3, 4)
        # P0-2 fail-closed: a gate-demoted partial must NOT run the product
        # reconcile (the formal L1/L2 surfaces stay untouched).
        assert result["reconcile"]["status"] == "skipped"
        assert result["reconcile"]["reason"] == "factor_not_ready"
        assert result["reconcile"]["dataset_status"] == "partial"

    def test_gate_allows_fresh(self, tmp_path, monkeypatch):
        """All active symbols fresh -> ready, freshness metrics attached."""
        symbols = [SYM_A, SYM_B, SYM_C]
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            raw_last={SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            parent_last={SYM_A: 20260731, SYM_B: 20260731, SYM_C: 20260731},
            window_rows={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
                "600036.SH": _factor_df([20260804], [3.0]),
            },
            symbols=symbols,
        )
        assert result["dataset_status"] == "ready"
        m = store.load_manifest(result["dataset_id"])
        freshness = (m.provenance or {}).get("freshness")
        assert freshness is not None
        assert freshness["fresh_symbol_ratio"] == 1.0
        assert result["freshness"]["fresh_count"] == 3
        # ready manifest: the product reconcile did run (not skipped)
        assert result["reconcile"]["status"] != "skipped"

    def test_gate_skipped_without_raw_baseline(self, tmp_path, monkeypatch):
        """No raw baseline in the store -> gate skipped, ready publish."""
        smd = _script()
        pro = _FakeFactorPro({
            "600000.SH": _factor_df([20260804], [3.0]),
        })
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        store = DatasetStore(tmp_path / "market_data")
        _publish_factor_manifest(store, {SYM_A: 20260731})
        uni = _write_universe(tmp_path, [SYM_A])
        args = _factor_args(tmp_path, universe_file=str(uni))
        result = smd.sync_tushare_adj_factor_full(args, store)
        assert result["dataset_status"] == "ready"
        assert "freshness" not in result
        m = store.load_manifest(result["dataset_id"])
        assert "freshness" not in (m.provenance or {})

    def test_skip_freshness_gate_keeps_ready(self, tmp_path, monkeypatch):
        """--skip-freshness-gate: the gate does not demote a stale manifest
        and the decision is recorded in the provenance."""
        symbols = [SYM_A, SYM_B, SYM_C]
        smd, pro, store, args, result = self._run(
            tmp_path, monkeypatch,
            raw_last={SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            parent_last={SYM_A: 20260731, SYM_B: 20260731, SYM_C: 20260701},
            window_rows={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
                "600036.SH": _factor_df([], []),  # stale vs raw baseline
            },
            symbols=symbols,
            args_overrides={"skip_freshness_gate": True},
        )
        # same scenario demotes without the flag (test_gate_marks_partial_on_stale)
        assert result["dataset_status"] == "ready"
        m = store.load_manifest(result["dataset_id"])
        assert m.status == "ready"
        assert (m.provenance or {}).get("freshness_gate") == "skipped_by_flag"
        assert "freshness" not in (m.provenance or {})


class TestGateBlockedPartialAsParent:
    """A partial manifest demoted by the freshness gate must stay usable as
    the next run's incremental parent (window continuation), otherwise the
    factor sync degrades to a full-history refetch every round."""

    def _publish_partial(self, store, dataset_id, provenance):
        _publish_factor_manifest(
            store, {SYM_A: 20260701, SYM_B: 20260701},
            dataset_id=dataset_id)
        m = store.load_manifest(dataset_id)
        m.status = "partial"
        m.provenance = dict(provenance)
        store.save_manifest(m)
        return m

    def test_gate_blocked_partial_selected(self, tmp_path):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        self._publish_partial(
            store, "tushare_adjfactor_gate_blocked",
            {"freshness": {"gate": "blocked"}})
        parent = smd._select_factor_incremental_parent(store)
        assert parent is not None
        assert parent.dataset_id == "tushare_adjfactor_gate_blocked"

    def test_plain_partial_rejected(self, tmp_path):
        """A partial NOT demoted by the gate (e.g. provider failures) must
        never become the incremental parent (broken records must not merge)."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        self._publish_partial(
            store, "tushare_adjfactor_plain_partial",
            {"freshness": {"fresh_count": 0, "active_count": 3}})
        assert smd._select_factor_incremental_parent(store) is None

    def test_ready_still_wins_over_gate_blocked_partial(self, tmp_path):
        """A ready full-history set always beats a gate-blocked partial."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_factor_manifest(
            store, {SYM_A: 20260804, SYM_B: 20260804},
            dataset_id="tushare_adjfactor_ready")
        self._publish_partial(
            store, "tushare_adjfactor_gate_blocked",
            {"freshness": {"gate": "blocked"}})
        parent = smd._select_factor_incremental_parent(store)
        assert parent.dataset_id == "tushare_adjfactor_ready"


class TestAutoFactorUniverse:
    def test_generated_from_raw_ok_symbols(self, tmp_path):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        # add one no_data symbol: must be excluded from the universe
        rec = SymbolRecord(symbol="SZSE.STK.000002", blob_sha256="",
                           quality="no_data", error="no_factor")
        raw.symbols = list(raw.symbols) + [rec]
        raw.symbol_count = len(raw.symbols)
        store.save_manifest(raw)

        path = smd._auto_generate_factor_universe(store)
        assert path is not None
        p = Path(path)
        assert p.exists()
        assert p.name.startswith(f"auto_factor_universe_{raw.data_cutoff_date}_")
        assert p.parent == store.root / "universes"
        loaded = smd._load_universe_file(p)
        expected = sorted(
            r.symbol for r in raw.symbols if r.quality == "ok" and r.blob_sha256)
        assert loaded == expected
        assert len(loaded) == FIXTURE_SYMBOLS
        # idempotent: second call returns the same file
        assert smd._auto_generate_factor_universe(store) == path
        # P1-2: the CSV is accompanied by a .meta.json with full provenance
        meta_path = p.with_suffix(p.suffix + ".meta.json")
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["source_dataset_id"] == raw.dataset_id
        assert meta["data_cutoff_date"] == raw.data_cutoff_date
        assert meta["generated_at"]
        assert meta["symbol_count"] == len(loaded)
        assert len(meta["universe_sha256"]) == 64
        assert meta["file"] == str(p)

    def test_tiny_surface_not_selected(self, tmp_path):
        """P1-2: a 20-row / 3-symbol raw surface is not a full-market
        baseline — _select_factor_raw_baseline returns None and no universe
        is generated from it."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            pad=False, n_rows=20)
        assert smd._select_factor_raw_baseline(store) is None
        assert smd._auto_generate_factor_universe(store) is None

    def test_16_row_orphan_loses_to_full_surface(self, tmp_path):
        """P1-2: a 16-row orphan window must never become the baseline while
        a full-market surface exists; the generated universe (and its meta)
        come from the full surface."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        orphan_syms = {f"SZSE.STK.{300000 + i}": 20260716 for i in range(500)}
        _publish_raw_manifest(store, orphan_syms,
                              dataset_id="tushare_none_1d_raw_orphan",
                              pad=False, n_rows=16)
        full = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        base = smd._select_factor_raw_baseline(store)
        assert base is not None
        assert base.dataset_id == full.dataset_id
        path = smd._auto_generate_factor_universe(store)
        assert path is not None
        meta = json.loads(
            Path(path).with_suffix(".csv.meta.json").read_text(encoding="utf-8"))
        assert meta["source_dataset_id"] == full.dataset_id
        assert meta["data_cutoff_date"] == full.data_cutoff_date
        assert meta["symbol_count"] == FIXTURE_SYMBOLS

    def test_none_without_raw_baseline(self, tmp_path):
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        assert smd._auto_generate_factor_universe(store) is None

    def test_chain_fallback_uses_generated_universe(self, tmp_path, monkeypatch):
        """No ready factor manifest -> chain auto-generates the universe
        from the raw baseline and passes it to the factor step."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        captured = {}

        def fake_raw(args, store, **kw):
            return {"status": "success", "sync_run_id": "raw_run",
                    "datasets": {}, "reconcile": {"status": "up_to_date"}}

        def fake_factor(args, store):
            captured["universe_file"] = args.universe_file
            return {"status": "success", "dataset_status": "ready",
                    "dataset_id": "tushare_adjfactor_1d_new",
                    "reconcile": {"status": "up_to_date", "missing": [],
                                  "issues": []},
                    "datasets": {}}

        monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
        monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {"status": "published", "missing": [],
                                          "issues": [], "l1_dataset_id": "l1",
                                          "l2_dataset_id": "l2"})
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: None)
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "success"
        assert captured["universe_file"] is not None
        assert Path(captured["universe_file"]).exists()
        expected = sorted(
            r.symbol for r in raw.symbols if r.quality == "ok" and r.blob_sha256)
        assert smd._load_universe_file(
            Path(captured["universe_file"])) == expected


class TestFullMarketBaselineGate:
    """P1-1: the factor raw baseline must be a real full-market surface —
    a 500-symbol pool can never drive factor coverage / universe generation."""

    def test_500_symbol_pool_is_not_full_market(self, tmp_path):
        """500 symbols x 260 rows passes the old 500/250 gates but is far
        below the A-share full-market anchor -> no baseline, no universe."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        pool = {f"SZSE.STK.{300000 + i}": 20260804 for i in range(500)}
        _publish_raw_manifest(store, pool, pad=False, n_rows=260)
        assert smd._select_factor_raw_baseline(store) is None
        assert smd._auto_generate_factor_universe(store) is None

    def test_full_market_wins_over_small_pool(self, tmp_path):
        """A full-market surface next to a 500-symbol subset pool: the
        full-market surface is the baseline, the subset pool is rejected."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        pool = {f"SZSE.STK.{300000 + i}": 20260804 for i in range(500)}
        _publish_raw_manifest(store, pool, pad=False, n_rows=260,
                              dataset_id="tushare_none_1d_small_pool")
        full = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        base = smd._select_factor_raw_baseline(store)
        assert base is not None
        assert base.dataset_id == full.dataset_id

    def test_relative_coverage_rejects_bigger_peer(self, tmp_path):
        """A 4200-symbol surface standing next to a LARGER 5000-symbol
        surface is not full-market (relative coverage 0.84 < 0.9)."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        bigger = {
            f"SSE.STK.{600000 + i}": 20260804 for i in range(5000)}
        _publish_raw_manifest(store, bigger, pad=False, n_rows=260,
                              dataset_id="tushare_none_1d_bigger")
        smaller = {
            f"SZSE.STK.{300000 + i}": 20260804 for i in range(4200)}
        _publish_raw_manifest(store, smaller, pad=False, n_rows=260,
                              dataset_id="tushare_none_1d_smaller")
        assert smd._is_full_market_manifest(
            store, store.load_manifest("tushare_none_1d_smaller")) is False
        assert smd._is_full_market_manifest(
            store, store.load_manifest("tushare_none_1d_bigger")) is True


def _publish_factor_manifest_with_universe(store, symbols, uni_path, *,
                                           dataset_id="tushare_adjfactor_universe",
                                           n_rows=300):
    """tushare/adj_factor ready factor manifest referencing a universe file."""
    records = []
    for sym in symbols:
        dates = _dates_ending(20260804, n_rows)
        sha = store.store_factors(sym, dates, np.full(n_rows, 1.0))
        records.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
            last_date=int(dates[-1]), row_count=n_rows, quality="ok"))
    m = DatasetManifest(
        dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
        period="1d", status="ready", dataset_type="factor",
        universe_file=str(uni_path), symbols=records,
        symbol_count=len(records), row_count=sum(r.row_count for r in records),
        data_cutoff_date=max(int(r.last_date or 0) for r in records),
    )
    store.publish(m)
    return m


class TestLatestFactorUniverseFile:
    """P1-1: the zero-config chain must only reuse a factor manifest's
    universe when it actually spans the full market."""

    def test_1_symbol_universe_rejected_without_baseline(self, tmp_path):
        """External repro: a 1-symbol 16-row ready factor manifest whose
        universe is small.csv must never be reused when no full-market raw
        baseline exists."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        uni = _write_universe(tmp_path, [SYM_A])
        _publish_factor_manifest_with_universe(
            store, [SYM_A], uni, dataset_id="tushare_adjfactor_small",
            n_rows=16)
        assert smd._latest_factor_universe_file_path(store) is None

    def test_small_universe_skipped_even_with_baseline(self, tmp_path):
        """A small universe (< 4000 rows and < 90% of the baseline ok
        symbols) is skipped even when a full-market raw baseline exists."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        uni = _write_universe(tmp_path, [SYM_A])
        _publish_factor_manifest_with_universe(
            store, [SYM_A], uni, dataset_id="tushare_adjfactor_small")
        assert smd._latest_factor_universe_file_path(store) is None

    def test_full_market_universe_reused_with_baseline(self, tmp_path):
        """A full-market raw baseline + a factor manifest universe covering
        >= 90% of its ok symbols -> the universe is reused."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        baseline = smd._select_factor_raw_baseline(store)
        assert baseline is not None
        assert baseline.dataset_id == raw.dataset_id
        ok_syms = sorted(
            r.symbol for r in baseline.symbols
            if r.quality == "ok" and r.blob_sha256)
        uni = _write_universe(tmp_path, ok_syms)
        _publish_factor_manifest_with_universe(
            store, ok_syms[:100], uni, dataset_id="tushare_adjfactor_full")
        assert smd._latest_factor_universe_file_path(store) == str(uni)

    def test_latest_candidate_wins_when_all_valid(self, tmp_path):
        """Both candidates' universes are full-market -> the newest (highest
        cutoff) factor manifest's universe wins."""
        smd = _script()
        store = DatasetStore(tmp_path / "md")
        raw = _publish_raw_manifest(store, {
            SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804})
        baseline = smd._select_factor_raw_baseline(store)
        ok_syms = sorted(
            r.symbol for r in baseline.symbols
            if r.quality == "ok" and r.blob_sha256)
        uni_old = _write_universe(tmp_path, ok_syms, name="universe_old.csv")
        uni_new = _write_universe(tmp_path, ok_syms, name="universe_new.csv")
        _publish_factor_manifest_with_universe(
            store, ok_syms[:10], uni_old, dataset_id="tushare_adjfactor_old")
        newer = store.load_manifest("tushare_adjfactor_old")
        newer.data_cutoff_date = 20260701
        store.save_manifest(newer)
        _publish_factor_manifest_with_universe(
            store, ok_syms[:10], uni_new, dataset_id="tushare_adjfactor_new")
        assert smd._latest_factor_universe_file_path(store) == str(uni_new)


class TestSyncLogFreshness:
    """P2-1a: the persisted factor sync log must carry the freshness metrics
    and gate status/reason of the real sync run."""

    def _run(self, tmp_path, monkeypatch, *, raw_last, parent_last,
             window_rows, symbols):
        smd = _script()
        pro = _FakeFactorPro(window_rows)
        _FakeTushareProvider.registry["pro"] = pro
        from wtpy.apps.astock.data.providers import tushare as tushare_mod
        monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeTushareProvider)
        store = DatasetStore(tmp_path / "market_data")
        _publish_raw_manifest(store, raw_last)
        _publish_factor_manifest(store, parent_last)
        uni = _write_universe(tmp_path, symbols)
        args = _factor_args(tmp_path, universe_file=str(uni))
        result = smd.sync_tushare_adj_factor_full(args, store)
        log_path = store.sync_logs_dir / f"{result['sync_run_id']}.json"
        return result, json.loads(log_path.read_text(encoding="utf-8"))

    def test_gate_blocked_partial_log_has_freshness(self, tmp_path, monkeypatch):
        """A gate-demoted partial persists freshness details + the blocked
        gate status and reason in the sync log JSON."""
        symbols = [SYM_A, SYM_B, SYM_C]
        result, log = self._run(
            tmp_path, monkeypatch,
            raw_last={SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            parent_last={SYM_A: 20260731, SYM_B: 20260731, SYM_C: 20260701},
            window_rows={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
                "600036.SH": _factor_df([], []),  # stale: parent retained
            },
            symbols=symbols,
        )
        assert result["dataset_status"] == "partial"
        assert log["freshness"] is not None
        assert log["freshness"]["gate"] == "blocked"
        assert log["freshness"]["reason"] == "freshness_below_threshold"
        assert log["freshness"]["fresh_count"] == 2
        assert log["freshness"]["active_count"] == 3
        assert log["freshness"]["fresh_symbol_ratio"] == round(2 / 3, 4)
        assert log["freshness"]["raw_dataset_id"]
        assert log["freshness"]["fresh_tolerance_days"] == 3
        assert log["freshness_gate"] == "blocked"
        assert log["result"]["status"] == "partial"

    def test_gate_passed_log_has_freshness(self, tmp_path, monkeypatch):
        """A ready publish records a passed gate + ratio in the sync log."""
        symbols = [SYM_A, SYM_B, SYM_C]
        result, log = self._run(
            tmp_path, monkeypatch,
            raw_last={SYM_A: 20260804, SYM_B: 20260804, SYM_C: 20260804},
            parent_last={SYM_A: 20260731, SYM_B: 20260731, SYM_C: 20260731},
            window_rows={
                "600000.SH": _factor_df([20260804], [3.0]),
                "000001.SZ": _factor_df([20260804], [3.0]),
                "600036.SH": _factor_df([20260804], [3.0]),
            },
            symbols=symbols,
        )
        assert result["dataset_status"] == "ready"
        assert log["freshness"]["gate"] == "passed"
        assert log["freshness"]["fresh_symbol_ratio"] == 1.0
        assert log["freshness_gate"] == "passed"
