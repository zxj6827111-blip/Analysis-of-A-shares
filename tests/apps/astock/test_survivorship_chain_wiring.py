# -*- coding: utf-8 -*-
"""Gate B7: survivorship-safe product chain wiring.

Covers: SQLite schema v4 migration paths, v4 field round-trip, execution
binding acceptance of composite_none, signal binding rejection of raw sets,
and the fail-closed survivorship-safe baseline resolver. Offline-only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.dataset_binding import (
    DatasetBindingError,
    validate_execution_dataset_binding,
    validate_signal_dataset_binding,
)
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.pit_universe import InstrumentWindow, PointInTimeUniverse
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.service import db as dbm
from wtpy.apps.astock.service.baseline import (
    BaselineUnavailableError,
    resolve_survivorship_safe_baseline,
)

SYM = "SSE.STK.600000"
DATES = [20240101, 20240102, 20240103]


def _cfg(tmp_path):
    cfg = AStockConfig(
        storage_root=str(tmp_path / "s"), output_root=str(tmp_path / "o")
    )
    cfg.ensure_dirs()
    return cfg


def _runs_cols(cfg):
    conn = sqlite3.connect(str(dbm.db_path(cfg)))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    finally:
        conn.close()


def _bars_arrays():
    close = np.array([10.0, 11.0, 12.0])
    return {
        "trade_date": np.array(DATES, dtype=np.int64),
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": np.array([100.0] * 3),
        "amount": np.array([1000.0] * 3),
    }


def _make_bars_ds(store, *, dataset_id, source, adjustment, provenance=None,
                  raw_dataset_id="", data_cutoff_date=20240103):
    sha = store.store_bar_arrays(SYM, _bars_arrays())
    m = DatasetManifest(
        dataset_id=dataset_id, source=source, adjustment=adjustment,
        period="1d", status="building", data_cutoff_date=data_cutoff_date,
        raw_dataset_id=raw_dataset_id,
        provenance=dict(provenance or {}),
        symbols=[SymbolRecord(symbol=SYM, blob_sha256=sha, first_date=DATES[0],
                              last_date=DATES[-1], row_count=3, quality="ok")],
        symbol_count=1, row_count=3,
    )
    m.status = "ready"
    store.save_manifest(m)
    return m


def _make_universe(store_root):
    pit = PointInTimeUniverse.build(
        [InstrumentWindow(
            canonical_symbol=SYM, ts_code="600000.SH", exchange="SSE",
            board="sse_main", name="t", list_status="L", list_date=20000101,
            delist_date=None, last_trade_date=None,
        )],
        cutoff=20240103,
    )
    pit.save(store_root)
    return pit


class TestSchemaV4:
    def test_fresh_db_is_v4(self, tmp_path):
        cfg = _cfg(tmp_path)
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4
        assert dbm._V4_COLUMNS.issubset(_runs_cols(cfg))

    _V3_RUNS_SQL = """
        DROP TABLE runs;
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
          execution_data_source TEXT, execution_dataset_id TEXT,
          signal_raw_dataset_id TEXT, signal_factor_dataset_id TEXT,
          signal_formula_version TEXT, execution_adjustment TEXT);
        UPDATE schema_meta SET value='3' WHERE key='schema_version';
    """

    def test_v3_db_migrates_to_v4(self, tmp_path):
        cfg = _cfg(tmp_path)
        dbm.init_db(cfg)
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        conn.executescript(
            self._V3_RUNS_SQL
            + "INSERT INTO runs(run_id,status,created_at) VALUES('r1','ok',1);"
        )
        conn.commit()
        conn.close()
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4
        assert dbm._V4_COLUMNS.issubset(_runs_cols(cfg))
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        conn.close()

    def test_partial_v4_schema_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path)
        dbm.init_db(cfg)
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        conn.executescript(
            self._V3_RUNS_SQL
            + "ALTER TABLE runs ADD COLUMN universe_dataset_id TEXT;"
            + "INSERT INTO runs(run_id,status,created_at,universe_dataset_id)"
            + "  VALUES('r1','ok',1,'keepme');"
        )
        conn.commit()
        conn.close()
        dbm.init_db(cfg)
        assert dbm.get_schema_version(cfg) == 4
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        assert conn.execute(
            "SELECT universe_dataset_id FROM runs WHERE run_id='r1'"
        ).fetchone()[0] == "keepme"
        conn.close()

    def test_newer_schema_refused(self, tmp_path):
        cfg = _cfg(tmp_path)
        dbm.init_db(cfg)
        conn = sqlite3.connect(str(dbm.db_path(cfg)))
        conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(dbm.SchemaMigrationError):
            dbm.init_db(cfg)

    def test_v4_field_round_trip(self, tmp_path):
        cfg = _cfg(tmp_path)
        dbm.init_db(cfg)
        dbm.upsert_run_from_index_row(cfg, {
            "run_id": "r_v4", "status": "ok", "created_at": 1753500000,
            "universe_dataset_id": "pit_universe_1d_x",
            "universe_rule_version": "pit_universe_rule_v1",
            "delist_exit_rule_version": "delist_exit_v1",
            "delist_exit_scenario": "zero_recovery",
            "delist_recovery_discount": 0.0,
            "signal_supplement_factor_dataset_id": "tushare_adjfactor_1d_sup",
            "baseline_generation": "survivorship_safe",
            "data_cutoff_date": 20260717,
        }, out_dir=None)
        got = [r for r in dbm.list_runs_db(cfg, limit=5) if r["run_id"] == "r_v4"][0]
        assert got["universe_dataset_id"] == "pit_universe_1d_x"
        assert got["delist_exit_scenario"] == "zero_recovery"
        assert got["baseline_generation"] == "survivorship_safe"
        assert got["data_cutoff_date"] == 20260717
        assert got["signal_supplement_factor_dataset_id"] == "tushare_adjfactor_1d_sup"


class TestCompositeBinding:
    def test_execution_binding_accepts_composite_none(self, tmp_path):
        store = DatasetStore(tmp_path / "md")
        repo = MarketDataRepository(store)
        m = _make_bars_ds(store, dataset_id="internal_composite_none_1d_t1",
                          source="internal", adjustment="composite_none")
        got = validate_execution_dataset_binding(
            repo, m.dataset_id, source="internal")
        assert got.adjustment == "composite_none"

    def test_execution_binding_still_rejects_adjusted(self, tmp_path):
        store = DatasetStore(tmp_path / "md")
        repo = MarketDataRepository(store)
        m = _make_bars_ds(store, dataset_id="internal_ctsfqfq_1d_t1",
                          source="internal",
                          adjustment="composite_tushare_factor_qfq")
        with pytest.raises(DatasetBindingError) as ei:
            validate_execution_dataset_binding(repo, m.dataset_id, source="internal")
        assert ei.value.code == "DATASET_ROLE_MISMATCH"

    def test_signal_binding_rejects_composite_none(self, tmp_path):
        store = DatasetStore(tmp_path / "md")
        repo = MarketDataRepository(store)
        m = _make_bars_ds(store, dataset_id="internal_composite_none_1d_t2",
                          source="internal", adjustment="composite_none")
        with pytest.raises(DatasetBindingError) as ei:
            validate_signal_dataset_binding(
                repo, m.dataset_id, source="internal",
                adjustment="composite_none")
        assert ei.value.code == "DATASET_ROLE_MISMATCH"


class TestBaselineResolver:
    def _cfg_with_baseline(self, tmp_path, *, with_signal=True, with_exec=True,
                           raw_parent_matches=True, with_universe=True):
        cfg = _cfg(tmp_path)
        store = DatasetStore(cfg.market_data_root)
        exe = None
        if with_exec:
            exe = _make_bars_ds(
                store, dataset_id="internal_composite_none_1d_t9",
                source="internal", adjustment="composite_none")
        pit = _make_universe(Path(cfg.market_data_root)) if with_universe else None
        if with_signal:
            _make_bars_ds(
                store,
                dataset_id="internal_composite_tushare_factor_qfq_1d_t9",
                source="internal", adjustment="composite_tushare_factor_qfq",
                raw_dataset_id=(
                    exe.dataset_id if (exe and raw_parent_matches)
                    else "internal_composite_none_1d_other"),
                provenance={
                    "universe_dataset_id": pit.universe_dataset_id if pit else "",
                    "supplement_factor_dataset_id": "tushare_adjfactor_1d_sup",
                },
            )
        return cfg

    def test_resolves_full_combo(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path)
        bl = resolve_survivorship_safe_baseline(cfg)
        assert bl["signal_adjustment"] == "composite_tushare_factor_qfq"
        assert bl["execution_dataset_id"] == "internal_composite_none_1d_t9"
        assert bl["universe_dataset_id"].startswith("pit_universe_1d_")
        assert bl["delist_exit_scenario"] == "last_tradable_price"
        assert bl["baseline_generation"] == "survivorship_safe"

    def test_missing_signal_fails_closed(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path, with_signal=False)
        with pytest.raises(BaselineUnavailableError):
            resolve_survivorship_safe_baseline(cfg)

    def test_missing_execution_fails_closed(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path, with_exec=False)
        with pytest.raises(BaselineUnavailableError):
            resolve_survivorship_safe_baseline(cfg)

    def test_missing_universe_fails_closed(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path, with_universe=False)
        with pytest.raises(BaselineUnavailableError):
            resolve_survivorship_safe_baseline(cfg)

    def test_generation_mix_fails_closed(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path, raw_parent_matches=False)
        with pytest.raises(BaselineUnavailableError) as ei:
            resolve_survivorship_safe_baseline(cfg)
        assert "refusing to mix generations" in str(ei.value)

    def test_scenario_override_passes_through(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path)
        bl = resolve_survivorship_safe_baseline(
            cfg, delist_exit_scenario="zero_recovery")
        assert bl["delist_exit_scenario"] == "zero_recovery"

    def test_api_baseline_overrides_product_default_local_vendor(self, tmp_path):
        """Product default execution_data_source=local_vendor must not stick
        when baseline=survivorship_safe pins internal/composite_none."""
        cfg = self._cfg_with_baseline(tmp_path)
        bl = resolve_survivorship_safe_baseline(cfg)
        # Mirror api.py create-backtest baseline merge
        _exec_src = "local_vendor"  # product default after `or "local_vendor"`
        _exec_ds = None
        if not _exec_ds and _exec_src in (None, "", "tdx_local", "local_vendor"):
            _exec_src = bl["execution_data_source"]
        _exec_ds = _exec_ds or bl["execution_dataset_id"]
        assert _exec_src == bl["execution_data_source"]
        assert _exec_src == "internal"
        assert _exec_ds == "internal_composite_none_1d_t9"

    def test_api_baseline_keeps_explicit_exec_pin(self, tmp_path):
        cfg = self._cfg_with_baseline(tmp_path)
        bl = resolve_survivorship_safe_baseline(cfg)
        _exec_src = "local_vendor"
        _exec_ds = "localvendor_none_explicit_pin"
        # Explicit dataset id wins — do not rewrite source solely from baseline
        if not _exec_ds and _exec_src in (None, "", "tdx_local", "local_vendor"):
            _exec_src = bl["execution_data_source"]
        _exec_ds = _exec_ds or bl["execution_dataset_id"]
        assert _exec_ds == "localvendor_none_explicit_pin"
        assert _exec_src == "local_vendor"
