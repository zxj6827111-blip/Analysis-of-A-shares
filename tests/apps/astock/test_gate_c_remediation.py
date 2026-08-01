# -*- coding: utf-8 -*-
"""Gate C remediation tests: D5 (SQLite migration/write reliability),
D1 (dataset binding), D2 (configurable dual-source variants + common
universe/cutoff), D6 (dataset-derived trading calendar), D7 (dataset factor
gate, BSE), D3/D4 (coverage & error semantics)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.providers.base import MarketBar
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.data.dataset_binding import (
    DatasetBindingError,
    classify_symbol_coverage,
    manifest_symbol_index,
    validate_execution_dataset_binding,
    validate_signal_dataset_binding,
)
from wtpy.apps.astock.service import db as dbm
from wtpy.apps.astock.service.backtest import resolve_market_data_bindings
from wtpy.apps.astock.service.backtest_request import BacktestRequest


# ---------------------------------------------------------------- fixtures

DATES = [20000104, 20080115, 20101012, 20151231, 20160104, 20200608, 20260717]


def _bars(symbol, dates, source, adjustment, base=10.0):
    return [
        MarketBar(
            symbol=symbol, trade_date=d, period="1d",
            open=base + i, high=base + i + 1, low=base + i - 1, close=base + i + 0.5,
            volume=1000.0, amount=10000.0, source=source, adjustment=adjustment,
        )
        for i, d in enumerate(dates)
    ]


def _publish(store, dataset_id, source, adjustment, symbol_specs, *, dataset_type="bars", **extra):
    """symbol_specs: list of (symbol, dates|None, quality)."""
    recs = []
    total = 0
    for sym, dates, quality in symbol_specs:
        if dates:
            sha = store.store_bars(sym, _bars(sym, dates, source, adjustment))
            recs.append(SymbolRecord(
                symbol=sym, blob_sha256=sha, row_count=len(dates),
                first_date=dates[0], last_date=dates[-1], quality="ok"))
            total += len(dates)
        else:
            recs.append(SymbolRecord(symbol=sym, blob_sha256="", quality=quality, error=quality))
    m = DatasetManifest(
        dataset_id=dataset_id, source=source, adjustment=adjustment, period="1d",
        status="building", dataset_type=dataset_type,
        symbols=recs, symbol_count=len(recs), row_count=total, **extra,
    )
    store.publish(m)
    return m


@pytest.fixture
def sandbox(tmp_path):
    cfg = AStockConfig(
        storage_root=str(tmp_path / "storage"),
        output_root=str(tmp_path / "output"),
        tdx_root=str(tmp_path / "tdx"),
    )
    cfg.ensure_dirs()
    store = DatasetStore(cfg.market_data_root)
    # execution: local_vendor/none — covers 600000/000001/920001 (+600001 exec-only)
    _publish(store, "localvendor_none_1d_t1", "local_vendor", "none", [
        ("SSE.STK.600000", DATES, "ok"),
        ("SZSE.STK.000001", DATES, "ok"),
        ("BSE.STK.920001", DATES, "ok"),
        ("SSE.STK.600001", DATES, "ok"),
    ])
    # signal A: tdxquant/front — covers 600000/000001/920001 + allowlisted no_data
    _publish(store, "tdxquant_front_1d_t1", "tdxquant", "front", [
        ("SSE.STK.600000", DATES, "ok"),
        ("SZSE.STK.000001", DATES, "ok"),
        ("BSE.STK.920001", DATES, "ok"),
        ("SSE.STK.600193", None, "no_data"),
    ])
    # signal B: internal/tushare_factor_qfq — misses 000001 (coverage case)
    _publish(store, "internal_tsfqfq_1d_t1", "internal", "tushare_factor_qfq", [
        ("SSE.STK.600000", DATES, "ok"),
        ("BSE.STK.920001", DATES, "ok"),
    ], raw_dataset_id="localvendor_none_1d_t1")
    # factor dataset: tushare/adj_factor with one factor change at 20160104
    fsha_600000 = store.store_factors("SSE.STK.600000", [20000104, 20160104], [1.0, 1.25])
    fsha_000001 = store.store_factors("SZSE.STK.000001", [20000104], [1.0])
    fsha_920001 = store.store_factors("BSE.STK.920001", [20000104], [1.0])
    fm = DatasetManifest(
        dataset_id="tushare_adjfactor_1d_t1", source="tushare",
        adjustment="adj_factor", period="1d", status="building",
        dataset_type="factor",
        symbols=[
            SymbolRecord(symbol="SSE.STK.600000", blob_sha256=fsha_600000, row_count=2, quality="ok"),
            SymbolRecord(symbol="SZSE.STK.000001", blob_sha256=fsha_000001, row_count=1, quality="ok"),
            SymbolRecord(symbol="BSE.STK.920001", blob_sha256=fsha_920001, row_count=1, quality="ok"),
        ],
        symbol_count=3, row_count=4,
    )
    store.publish(fm)
    # a partial dataset for status probes
    pm = DatasetManifest(
        dataset_id="tdxquant_front_1d_partial", source="tdxquant",
        adjustment="front", period="1d", status="partial",
        symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256="", quality="error", error="x")],
        symbol_count=1, row_count=0,
    )
    store.save_manifest(pm)
    return cfg, store


# ---------------------------------------------------------------- D5


def _runs_cols(cfg):
    conn = sqlite3.connect(str(dbm.db_path(cfg)))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    finally:
        conn.close()


class TestD5SchemaMigration:
    def test_fresh_db_is_latest(self, tmp_path):
        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()
        dbm.init_db(cfg)
        # Gate B7 advanced the latest schema to v4 (survivorship-safe columns)
        assert dbm.get_schema_version(cfg) == dbm._SCHEMA_VERSION == 4
        assert dbm._V3_COLUMNS.issubset(_runs_cols(cfg))
        assert dbm._V4_COLUMNS.issubset(_runs_cols(cfg))

    def _make_v2_db(self, out_root: Path):
        out_root.mkdir(parents=True, exist_ok=True)
        p = out_root / "astock_experiments.sqlite3"
        conn = sqlite3.connect(str(p))
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, title TEXT, status TEXT,
              created_at INTEGER, period TEXT, period_label TEXT, account_mode TEXT,
              start INTEGER, end INTEGER, hold INTEGER, entry_lag INTEGER,
              buy_weekday INTEGER, exit_weekday INTEGER, buy_on TEXT, sell_on TEXT,
              signal_weekdays_json TEXT, schedule_mode TEXT, with_bagua INTEGER,
              gua_filter_json TEXT, indicator_ids_json TEXT, indicator_names_json TEXT,
              param_hash TEXT, experiment_id TEXT, variant_id TEXT, code_version TEXT,
              bagua_rule_version TEXT, selected_codes_count INTEGER,
              n_signals_before_bagua INTEGER, n_signals_after_bagua INTEGER,
              error TEXT, extra_json TEXT, signal_data_source TEXT,
              signal_adjustment TEXT, dataset_id TEXT, weekly_bar_mode TEXT,
              execution_data_source TEXT, execution_dataset_id TEXT);
            INSERT INTO schema_meta VALUES('schema_version','2');
            INSERT INTO runs(run_id,status,created_at,param_hash)
              VALUES('bt_old_1','ok',1700000000,'ph1');
            """
        )
        conn.commit()
        conn.close()
        return p

    def test_existing_v2_db_migrates_and_keeps_data(self, tmp_path):
        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()
        self._make_v2_db(Path(cfg.output_root))
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4
        assert dbm._V3_COLUMNS.issubset(_runs_cols(cfg))
        assert dbm._V4_COLUMNS.issubset(_runs_cols(cfg))
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        conn.close()
        # idempotent re-run
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4

    def test_partial_schema_migrates_idempotently(self, tmp_path):
        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()
        p = self._make_v2_db(Path(cfg.output_root))
        conn = sqlite3.connect(str(p))
        conn.execute("ALTER TABLE runs ADD COLUMN signal_raw_dataset_id TEXT")
        conn.execute("UPDATE runs SET signal_raw_dataset_id='keepme'")
        conn.commit()
        conn.close()
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4
        assert dbm._V3_COLUMNS.issubset(_runs_cols(cfg))
        assert dbm._V4_COLUMNS.issubset(_runs_cols(cfg))
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        val = conn.execute(
            "SELECT signal_raw_dataset_id FROM runs WHERE run_id='bt_old_1'"
        ).fetchone()[0]
        conn.close()
        assert val == "keepme"

    def test_upsert_writes_v3_lineage_and_history_sees_it(self, tmp_path):
        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()
        dbm.init_db(cfg)
        dbm.upsert_run_from_index_row(cfg, {
            "run_id": "bt_v3_1",
            "status": "ok",
            "created_at": 1753500000,
            "signal_data_source": "internal",
            "signal_adjustment": "tushare_factor_qfq",
            "dataset_id": "internal_tsfqfq_1d_t1",
            "signal_raw_dataset_id": "localvendor_none_1d_t1",
            "signal_factor_dataset_id": "tushare_adjfactor_1d_t1",
            "signal_formula_version": "tsqfq_v1",
            "execution_data_source": "local_vendor",
            "execution_adjustment": "none",
            "execution_dataset_id": "localvendor_none_1d_t1",
            "metrics": {"total_return": 0.1},
        })
        rows = dbm.list_runs_db(cfg, limit=10)
        r = [x for x in rows if x["run_id"] == "bt_v3_1"][0]
        assert r["signal_raw_dataset_id"] == "localvendor_none_1d_t1"
        assert r["signal_factor_dataset_id"] == "tushare_adjfactor_1d_t1"
        assert r["signal_formula_version"] == "tsqfq_v1"
        assert r["execution_adjustment"] == "none"
        from wtpy.apps.astock.service.runs import list_runs
        hist = list_runs(cfg, limit=10)
        h = [x for x in hist if x.get("run_id") == "bt_v3_1"][0]
        assert h.get("signal_factor_dataset_id") == "tushare_adjfactor_1d_t1"

    def test_upsert_failure_not_swallowed(self, tmp_path, monkeypatch):
        from wtpy.apps.astock.service import runs as runs_mod

        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()

        def _boom(*a, **k):
            raise sqlite3.OperationalError("no such column: whatever")

        monkeypatch.setattr(dbm, "upsert_run_from_index_row", _boom)
        with pytest.raises(runs_mod.RunPersistenceError, match="sqlite_persist_failed"):
            runs_mod.append_run_index(cfg, {"run_id": "bt_fail_1", "status": "ok"})
        rows = json.loads((Path(cfg.output_root) / "runs_index.json").read_text(encoding="utf-8"))
        row = [r for r in rows if r["run_id"] == "bt_fail_1"][0]
        assert row["status"] == "failed"
        assert "sqlite_persist_failed" in row["error"]

    def test_reconcile_backfills_idempotently(self, tmp_path):
        cfg = AStockConfig(storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o"))
        cfg.ensure_dirs()
        dbm.init_db(cfg)
        out = Path(cfg.output_root)
        (out / "bt_orphan_1").mkdir(parents=True)
        (out / "bt_orphan_1" / "run_meta.json").write_text(json.dumps({
            "run_id": "bt_orphan_1", "status": "ok",
            "metrics": {"total_return": 0.05},
            "repro": {"request": {
                "signal_data_source": "tdxquant",
                "signal_adjustment": "front",
                "dataset_id": "tdxquant_front_1d_t1",
                "execution_dataset_id": "localvendor_none_1d_t1",
                "execution_adjustment": "none",
            }},
        }), encoding="utf-8")
        (out / "runs_index.json").write_text(json.dumps([
            {"run_id": "bt_orphan_1", "status": "ok", "created_at": 1753500001},
        ]), encoding="utf-8")
        rep1 = dbm.reconcile_runs_from_disk(cfg)
        assert rep1["ok"] and rep1["inserted"] == 1
        rep2 = dbm.reconcile_runs_from_disk(cfg)
        assert rep2["inserted"] == 0 and rep2["already_present"] >= 1
        rows = dbm.list_runs_db(cfg, limit=10)
        r = [x for x in rows if x["run_id"] == "bt_orphan_1"][0]
        assert r["signal_data_source"] == "tdxquant"
        assert r["dataset_id"] == "tdxquant_front_1d_t1"
        assert r["execution_adjustment"] == "none"


# ---------------------------------------------------------------- D1


class TestD1DatasetBinding:
    def _repo(self, sandbox):
        cfg, store = sandbox
        return MarketDataRepository(store)

    def test_source_mismatch_rejected(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "internal_tsfqfq_1d_t1",
                source="tdxquant", adjustment="front", period="1d")
        assert ei.value.code == "DATASET_BINDING_MISMATCH"
        assert ei.value.http_status == 400
        p = ei.value.to_payload()
        assert p["requested_source"] == "tdxquant"
        assert p["manifest_source"] == "internal"
        assert p["remediation"]

    def test_adjustment_mismatch_rejected(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "internal_tsfqfq_1d_t1",
                source="internal", adjustment="front", period="1d")
        assert ei.value.code == "DATASET_BINDING_MISMATCH"

    def test_period_mismatch_rejected(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "tdxquant_front_1d_t1",
                source="tdxquant", adjustment="front", period="1w")
        assert ei.value.code == "DATASET_BINDING_MISMATCH"

    def test_status_not_ready_rejected(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "tdxquant_front_1d_partial",
                source="tdxquant", adjustment="front", period="1d")
        assert ei.value.code == "DATASET_NOT_READY"

    def test_not_found_is_404(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "does_not_exist_1d", source="tdxquant",
                adjustment="front", period="1d")
        assert ei.value.code == "DATASET_NOT_FOUND"
        assert ei.value.http_status == 404

    def test_factor_dataset_cannot_be_signal(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "tushare_adjfactor_1d_t1",
                source="tushare", adjustment="adj_factor", period="1d")
        assert ei.value.code == "DATASET_ROLE_MISMATCH"

    def test_execution_must_be_raw(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_execution_dataset_binding(
                repo, "tdxquant_front_1d_t1", source="tdxquant", period="1d")
        assert ei.value.code == "DATASET_ROLE_MISMATCH"

    def test_raw_dataset_cannot_be_signal(self, sandbox):
        repo = self._repo(sandbox)
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, "localvendor_none_1d_t1",
                source="local_vendor", adjustment="none", period="1d")
        assert ei.value.code == "DATASET_ROLE_MISMATCH"

    def test_backtest_entry_rejects_mismatch(self, sandbox, monkeypatch):
        cfg, store = sandbox
        req = BacktestRequest(
            rule_ids=["r"],
            signal_data_source="tdxquant", signal_adjustment="front",
            dataset_id="internal_tsfqfq_1d_t1",
            execution_data_source="local_vendor",
            execution_dataset_id="localvendor_none_1d_t1",
        )
        with pytest.raises(DatasetBindingError) as ei:
            resolve_market_data_bindings(cfg, req, ["SSE.STK.600000"])
        assert ei.value.code == "DATASET_BINDING_MISMATCH"

    def test_backtest_entry_ok_sets_lineage(self, sandbox):
        cfg, store = sandbox
        req = BacktestRequest(
            rule_ids=["r"],
            signal_data_source="internal",
            signal_adjustment="tushare_factor_qfq",
            dataset_id="internal_tsfqfq_1d_t1",
            execution_data_source="local_vendor",
            execution_dataset_id="localvendor_none_1d_t1",
        )
        b = resolve_market_data_bindings(cfg, req, ["SSE.STK.600000"])
        assert req.execution_adjustment == "none"
        assert req.signal_raw_parent_dataset_id == "localvendor_none_1d_t1"
        assert b["factor_dataset_id"] == "tushare_adjfactor_1d_t1"
        assert req.ca_factor_dataset_id == "tushare_adjfactor_1d_t1"


# ---------------------------------------------------------------- D3


class TestD3CoverageSemantics:
    def test_classify_symbol_coverage(self, sandbox):
        cfg, store = sandbox
        repo = MarketDataRepository(store)
        m = repo.get_dataset("tdxquant_front_1d_t1")
        idx = manifest_symbol_index(m)
        assert classify_symbol_coverage(idx, "SSE.STK.600000") == "ok"
        assert classify_symbol_coverage(idx, "SSE.STK.999999") == "not_in_dataset"
        assert classify_symbol_coverage(idx, "SSE.STK.600193") == "no_data_allowlisted"

    def test_uncovered_symbol_soft_dropped_not_silent(self, sandbox):
        """Product mode: drop uncovered symbols with explicit list; keep runnable pool."""
        cfg, store = sandbox
        req = BacktestRequest(
            rule_ids=["r"],
            signal_data_source="tdxquant", signal_adjustment="front",
            dataset_id="tdxquant_front_1d_t1",
            execution_data_source="local_vendor",
            execution_dataset_id="localvendor_none_1d_t1",
        )
        b = resolve_market_data_bindings(
            cfg, req, ["SSE.STK.600000", "SSE.STK.600193", "SZSE.STK.300999"])
        reasons = {x["symbol"]: x["reason"] for x in b["coverage_excluded"]}
        assert reasons["SSE.STK.600193"] == "signal_no_data_allowlisted"
        assert reasons["SZSE.STK.300999"] == "signal_not_in_dataset"
        assert b["codes_kept"] == ["SSE.STK.600000"]
        assert b["codes_requested_count"] == 3
        assert "SSE.STK.600193" not in b["codes_kept"]

    def test_all_uncovered_still_hard_fails(self, sandbox):
        cfg, store = sandbox
        req = BacktestRequest(
            rule_ids=["r"],
            signal_data_source="tdxquant", signal_adjustment="front",
            dataset_id="tdxquant_front_1d_t1",
            execution_data_source="local_vendor",
            execution_dataset_id="localvendor_none_1d_t1",
        )
        with pytest.raises(DatasetBindingError) as ei:
            resolve_market_data_bindings(
                cfg, req, ["SSE.STK.600193", "SZSE.STK.300999"])
        assert ei.value.code == "SYMBOL_NOT_COVERED"
        assert ei.value.extra.get("kept_count") == 0


# ---------------------------------------------------------------- D2


class TestD2SignalVariants:
    def _create(self, cfg, **kw):
        from wtpy.apps.astock.service.experiments import create_experiment_from_grid

        base = dict(
            name="dual",
            rule_ids=["ma_cross"],
            codes=["SSE.STK.600000", "SZSE.STK.000001", "BSE.STK.920001"],
            start=20000101,
            end=20270101,
            execution_data_source="local_vendor",
            force=True,
        )
        base.update(kw)
        return create_experiment_from_grid(cfg, **base)

    def test_single_experiment_two_variants(self, sandbox):
        cfg, store = sandbox
        from wtpy.apps.astock.service import db as exp_db

        exp = self._create(cfg, signal_variants=[
            {"signal_data_source": "tdxquant", "signal_adjustment": "front",
             "dataset_id": "tdxquant_front_1d_t1"},
            {"signal_data_source": "internal", "signal_adjustment": "tushare_factor_qfq",
             "dataset_id": "internal_tsfqfq_1d_t1"},
        ])
        exp_id = exp["experiment_id"]
        row = exp_db.get_experiment(cfg, exp_id)
        variants = row["variants"]
        assert len(variants) == 2
        srcs = {v["params"]["signal_data_source"] for v in variants}
        assert srcs == {"tdxquant", "internal"}
        ds = {v["params"]["dataset_id"] for v in variants}
        assert ds == {"tdxquant_front_1d_t1", "internal_tsfqfq_1d_t1"}
        # identical pool / dates / execution across variants
        p0, p1 = variants[0]["params"], variants[1]["params"]
        assert p0["codes"] == p1["codes"]
        assert p0["end"] == p1["end"]
        assert p0["execution_dataset_id"] == p1["execution_dataset_id"] == "localvendor_none_1d_t1"

    def test_common_universe_and_cutoff(self, sandbox):
        cfg, store = sandbox
        exp = self._create(cfg, signal_variants=[
            {"signal_data_source": "tdxquant", "signal_adjustment": "front"},
            {"signal_data_source": "internal", "signal_adjustment": "tushare_factor_qfq"},
        ])
        cu = exp["config"]["common_universe"] if "config" in exp else None
        if cu is None:
            from wtpy.apps.astock.service import db as exp_db
            cu = exp_db.get_experiment(cfg, exp["experiment_id"])["config"]["common_universe"]
        assert cu["requested_universe_count"] == 3
        # 000001 missing from internal signal set -> excluded
        assert cu["common_universe_count"] == 2
        assert cu["excluded_by_signal_counts"]["internal/tushare_factor_qfq"] == 1
        assert cu["effective_end_date"] == 20260717  # dataset cutoff < requested end
        assert cu["requested_end_date"] == 20270101
        reasons = {x["symbol"]: x["reason"] for x in cu["exclusions"]}
        assert "SZSE.STK.000001" in reasons

    def test_dual_source_compare_template(self, sandbox):
        cfg, store = sandbox
        from wtpy.apps.astock.service import db as exp_db

        exp = self._create(cfg, dual_source_compare=True)
        row = exp_db.get_experiment(cfg, exp["experiment_id"])
        srcs = {v["params"]["signal_data_source"] for v in row["variants"]}
        assert srcs == {"tdxquant", "internal"}
        adjs = {v["params"]["signal_adjustment"] for v in row["variants"]}
        assert "front" in adjs
        assert adjs & {"tushare_factor_qfq", "composite_tushare_factor_qfq"}

    def test_legacy_not_allowed_as_variant(self, sandbox):
        cfg, store = sandbox
        with pytest.raises(ValueError, match="legacy"):
            self._create(cfg, signal_variants=[
                {"signal_data_source": "legacy_tdx_local_asof"},
                {"signal_data_source": "tdxquant", "signal_adjustment": "front"},
            ])

    def test_variant_failure_fails_experiment(self, sandbox, monkeypatch):
        cfg, store = sandbox
        from wtpy.apps.astock.service import db as exp_db
        from wtpy.apps.astock.service.experiments import ExperimentRunner

        exp = self._create(cfg, signal_variants=[
            {"signal_data_source": "tdxquant", "signal_adjustment": "front"},
            {"signal_data_source": "internal", "signal_adjustment": "tushare_factor_qfq"},
        ])
        exp_id = exp["experiment_id"]
        runner = ExperimentRunner(cfg)

        calls = {"n": 0}

        def _fake_run_one(self_, eid, variant):
            calls["n"] += 1
            vid = variant["variant_id"]
            if calls["n"] == 1:
                exp_db.update_variant(cfg, vid, status="succeeded", run_id="bt_fake_ok")
                return vid, "succeeded", "bt_fake_ok", None
            exp_db.update_variant(cfg, vid, status="failed", error="boom")
            return vid, "failed", None, "boom"

        monkeypatch.setattr(ExperimentRunner, "_run_one", _fake_run_one)
        runner._run_experiment(exp_id)
        row = exp_db.get_experiment(cfg, exp_id)
        assert row["status"] == "failed"
        assert row["failed_variants"] == 1


# ---------------------------------------------------------------- D6


class TestD6DatasetCalendar:
    def test_calendar_from_dataset_covers_full_range(self, sandbox, tmp_path):
        cfg, store = sandbox
        from wtpy.apps.astock.data.calendar import build_calendar_from_dataset

        cal, meta = build_calendar_from_dataset(
            store, "localvendor_none_1d_t1", cache_dir=tmp_path / "calcache")
        assert cal.dates[0] == 20000104
        assert cal.dates[-1] == 20260717
        assert meta["calendar_source"] == "execution_dataset"
        assert meta["calendar_sha256"]
        # 2000-2015 usable
        assert cal.is_trading_day(20080115)
        assert cal.next_trading_day(20000105) == 20080115
        # 2016 boundary has a predecessor (not the floor anymore)
        assert cal.prev_trading_day(20160104) == 20151231
        # no trading day after the last -> None (explicit refusal)
        assert cal.next_trading_day(20260717) is None
        # cache round-trip: same sha
        cal2, meta2 = build_calendar_from_dataset(
            store, "localvendor_none_1d_t1", cache_dir=tmp_path / "calcache")
        assert meta2["calendar_sha256"] == meta["calendar_sha256"]
        assert cal2.dates == cal.dates

    def test_calendar_hash_isolation_between_datasets(self, sandbox, tmp_path):
        cfg, store = sandbox
        from wtpy.apps.astock.data.calendar import build_calendar_from_dataset

        _publish(store, "localvendor_none_1d_t2", "local_vendor", "none", [
            ("SSE.STK.600000", [20100104, 20100105], "ok"),
        ])
        cal1, meta1 = build_calendar_from_dataset(store, "localvendor_none_1d_t1")
        cal2, meta2 = build_calendar_from_dataset(store, "localvendor_none_1d_t2")
        assert meta1["calendar_sha256"] != meta2["calendar_sha256"]


# ---------------------------------------------------------------- D7


class TestD7DatasetFactorGate:
    def test_factor_series_from_dataset_complete(self, sandbox):
        cfg, store = sandbox
        from wtpy.apps.astock.data.adjustments import (
            build_factor_series_from_dataset,
            formal_adjustment_ready,
        )

        repo = MarketDataRepository(store)
        fm = repo.get_dataset("tushare_adjfactor_1d_t1")
        s = build_factor_series_from_dataset(store, fm, "SSE.STK.600000", DATES)
        assert s.quality == "complete"
        assert s.source == "dataset"
        assert s.event_dates == [20000104, 20160104]
        # asof semantics: factor jumps exactly at 20160104
        fmap = dict(zip(s.dates, s.factors))
        assert fmap[20151231] == 1.0
        assert fmap[20160104] == 1.25
        ok, msg = formal_adjustment_ready([s])
        assert ok, msg

    def test_bse_symbol_covered_no_baostock(self, sandbox, monkeypatch):
        cfg, store = sandbox
        from wtpy.apps.astock.data import adjustments as adj_mod
        from wtpy.apps.astock.data.adjustments import (
            build_factor_series_from_dataset,
            formal_adjustment_ready,
        )

        def _no_bs(*a, **k):
            raise AssertionError("baostock must not be called in repo mode")

        monkeypatch.setattr(adj_mod, "fetch_baostock_factor_events", _no_bs)
        repo = MarketDataRepository(store)
        fm = repo.get_dataset("tushare_adjfactor_1d_t1")
        s = build_factor_series_from_dataset(store, fm, "BSE.STK.920001", DATES)
        assert s.quality == "complete"
        ok, _ = formal_adjustment_ready([s])
        assert ok

    def test_missing_symbol_is_explicit_incomplete(self, sandbox):
        cfg, store = sandbox
        from wtpy.apps.astock.data.adjustments import (
            build_factor_series_from_dataset,
            formal_adjustment_ready,
        )

        repo = MarketDataRepository(store)
        fm = repo.get_dataset("tushare_adjfactor_1d_t1")
        s = build_factor_series_from_dataset(store, fm, "SSE.STK.601999", DATES)
        assert s.quality == "incomplete"
        assert s.source == "dataset_missing"
        ok, msg = formal_adjustment_ready([s])
        assert not ok
        assert "601999" in msg

    def test_bse_code_mapping(self):
        from wtpy.apps.astock.data.universe import is_bse_code, to_std_code
        from wtpy.apps.astock.service.backtest_universe import select_universe

        assert to_std_code("920001") == "BSE.STK.920001"
        assert to_std_code("bj430047") == "BSE.STK.430047"
        assert to_std_code("830799") == "BSE.STK.830799"
        assert to_std_code("870436") == "BSE.STK.870436"
        assert to_std_code("BSE.STK.920001") == "BSE.STK.920001"
        assert to_std_code("900901") == "SSE.STK.900901"  # SSE B-share stays SSE
        assert is_bse_code("920001") and is_bse_code("bj430047")
        assert not is_bse_code("900901")
        cfg = AStockConfig()
        out = select_universe(cfg, ["BSE.STK.920001", "bj830799", "920002"])
        assert out == ["BSE.STK.920001", "BSE.STK.830799", "BSE.STK.920002"]

    def test_bse_symbol_variants_in_repository(self):
        v = MarketDataRepository._symbol_variants("BSE.STK.920001")
        assert "920001.BJ" in v and "bj920001" in v
        v2 = MarketDataRepository._symbol_variants("920001")
        assert "BSE.STK.920001" in v2


# ---------------------------------------------------------------- D4


class TestD4ErrorModel:
    def test_payload_shape(self, sandbox):
        cfg, store = sandbox
        repo = MarketDataRepository(store)
        try:
            validate_signal_dataset_binding(
                repo, "internal_tsfqfq_1d_t1",
                source="tdxquant", adjustment="front", period="1d")
            assert False, "should raise"
        except DatasetBindingError as e:
            p = e.to_payload()
            for k in ("code", "message", "dataset_id", "requested_source",
                      "requested_adjustment", "manifest_source",
                      "manifest_adjustment", "remediation"):
                assert k in p, k

    def test_api_maps_binding_error_to_4xx(self, sandbox, monkeypatch):
        cfg, store = sandbox
        from fastapi.testclient import TestClient
        from wtpy.apps.astock.api import create_app

        app = create_app(cfg)
        client = TestClient(app)
        # async pre-validation: mismatch -> 400 structured, no job created
        r = client.post("/api/v1/backtests", json={
            "rule_ids": ["tn6_whatever"],
            "codes": ["SSE.STK.600000"],
            "async_mode": True,
            "signal_data_source": "tdxquant",
            "signal_adjustment": "front",
            "dataset_id": "internal_tsfqfq_1d_t1",
            "execution_data_source": "local_vendor",
            "execution_dataset_id": "localvendor_none_1d_t1",
        })
        assert r.status_code == 400
        d = r.json()["detail"]
        assert d["code"] == "DATASET_BINDING_MISMATCH"
        assert d["manifest_source"] == "internal"
        # nonexistent dataset -> 404 (was 500 before remediation)
        r2 = client.post("/api/v1/backtests", json={
            "rule_ids": ["tn6_whatever"],
            "codes": ["SSE.STK.600000"],
            "async_mode": True,
            "signal_data_source": "tdxquant",
            "signal_adjustment": "front",
            "dataset_id": "no_such_dataset_1d",
        })
        assert r2.status_code == 404
        assert r2.json()["detail"]["code"] == "DATASET_NOT_FOUND"
        # no run rows were created by either rejected request
        assert not any(Path(cfg.output_root).glob("bt_*"))
