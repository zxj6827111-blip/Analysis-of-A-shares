"""Tests for legacy run backward compatibility."""
import pytest
import sqlite3
from pathlib import Path

from wtpy.apps.astock.service.db import init_db, connect, _SCHEMA_VERSION


@pytest.fixture
def db_cfg(tmp_path):
    class FakeCfg:
        output_root = str(tmp_path)
    return FakeCfg()


class TestLegacyRunCompatibility:
    def test_schema_version_is_2(self):
        assert _SCHEMA_VERSION == 2

    def test_fresh_db_has_new_columns(self, db_cfg):
        init_db(db_cfg)
        conn = connect(db_cfg)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        conn.close()
        assert "signal_data_source" in cols
        assert "signal_adjustment" in cols
        assert "dataset_id" in cols
        assert "weekly_bar_mode" in cols
        assert "execution_data_source" in cols
        assert "execution_dataset_id" in cols

    def test_migration_v1_to_v2(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)

        db_file = Path(tmp_path) / "astock_experiments.sqlite3"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES('schema_version', '1')")
        conn.execute("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, title TEXT, status TEXT,
                created_at INTEGER, period TEXT, period_label TEXT,
                account_mode TEXT, start INTEGER, end INTEGER,
                hold INTEGER, entry_lag INTEGER, buy_weekday INTEGER,
                exit_weekday INTEGER, buy_on TEXT, sell_on TEXT,
                signal_weekdays_json TEXT, schedule_mode TEXT,
                with_bagua INTEGER, gua_filter_json TEXT,
                indicator_ids_json TEXT, indicator_names_json TEXT,
                param_hash TEXT, experiment_id TEXT, variant_id TEXT,
                code_version TEXT, bagua_rule_version TEXT,
                selected_codes_count INTEGER, n_signals_before_bagua INTEGER,
                n_signals_after_bagua INTEGER, error TEXT, extra_json TEXT
            );
        """)
        conn.execute("""
            INSERT INTO runs (run_id, title, status) VALUES ('old_run_1', 'Legacy Run', 'done')
        """)
        conn.commit()
        conn.close()

        init_db(FakeCfg())

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id='old_run_1'").fetchone()
        assert row["signal_data_source"] == "legacy_tdx_local_asof"
        assert row["signal_adjustment"] == "asof_qfq"
        assert row["dataset_id"] is None
        assert row["weekly_bar_mode"] == "local_aggregate"
        assert row["execution_data_source"] == "tdx_local"
        conn.close()

    def test_old_run_not_marked_tdxquant(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)

        db_file = Path(tmp_path) / "astock_experiments.sqlite3"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);")
        conn.execute("INSERT INTO schema_meta VALUES('schema_version', '1');")
        conn.execute("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, title TEXT, status TEXT,
                created_at INTEGER, period TEXT, period_label TEXT,
                account_mode TEXT, start INTEGER, end INTEGER,
                hold INTEGER, entry_lag INTEGER, buy_weekday INTEGER,
                exit_weekday INTEGER, buy_on TEXT, sell_on TEXT,
                signal_weekdays_json TEXT, schedule_mode TEXT,
                with_bagua INTEGER, gua_filter_json TEXT,
                indicator_ids_json TEXT, indicator_names_json TEXT,
                param_hash TEXT, experiment_id TEXT, variant_id TEXT,
                code_version TEXT, bagua_rule_version TEXT,
                selected_codes_count INTEGER, n_signals_before_bagua INTEGER,
                n_signals_after_bagua INTEGER, error TEXT, extra_json TEXT
            );
        """)
        conn.execute("INSERT INTO runs (run_id, title) VALUES ('r1', 'Old')")
        conn.commit()
        conn.close()

        init_db(FakeCfg())

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id='r1'").fetchone()
        assert row["signal_data_source"] != "tdxquant"
        assert row["signal_data_source"] != "tushare"
        assert row["signal_data_source"] == "legacy_tdx_local_asof"
        conn.close()

    def test_new_run_can_set_tdxquant(self, db_cfg):
        init_db(db_cfg)
        conn = connect(db_cfg)
        conn.execute(
            "INSERT INTO runs (run_id, signal_data_source, signal_adjustment, dataset_id, weekly_bar_mode, execution_data_source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("new_run_1", "tdxquant", "front", "ds_123", "local_aggregate", "tdx_local"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM runs WHERE run_id='new_run_1'").fetchone()
        assert row["signal_data_source"] == "tdxquant"
        assert row["dataset_id"] == "ds_123"
        conn.close()
