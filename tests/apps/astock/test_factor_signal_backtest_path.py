# -*- coding: utf-8 -*-
"""Gate C wiring guards: derived qfq lineage flows through the backtest path.

Static source guards (no real backtest is run) plus a functional sqlite
lineage-column check and an offline read-path guard proving that reading a
derived dataset never touches any Provider.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.service.db import connect, init_db, upsert_run_from_index_row

ROOT = Path(__file__).resolve().parents[3]
SVC = ROOT / "wtpy" / "apps" / "astock" / "service"
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

SYM_A = "SSE.STK.600000"
DATES = [
    20240101, 20240102, 20240103, 20240104, 20240105,
    20240108, 20240109, 20240110, 20240111, 20240112,
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestBacktestWiringStatic:
    def test_repository_l1_path_includes_internal(self):
        src = _read(SVC / "backtest.py")
        marker = "_use_repository_l1 ="
        assert marker in src, "_use_repository_l1 assignment not found"
        window = src[src.index(marker): src.index(marker) + 240]
        assert '"internal"' in window, (
            "internal source must route through the repository L1 path"
        )

    def test_signal_cache_key_call_wires_lineage(self):
        src = _read(SVC / "backtest.py")
        marker = "return signal_cache_key("
        assert marker in src, "signal_cache_key call not found in backtest.py"
        window = src[src.index(marker): src.index(marker) + 2000]
        assert "raw_parent_dataset_id" in window
        assert "factor_parent_dataset_id" in window
        assert "formula_version" in window
        assert "anchor_policy" in window

    def test_execution_payload_wires_lineage(self):
        src = _read(SVC / "backtest_context.py")
        start = src.index("_ex_payload = {")
        end = src.index("execution_cache_key(_ex_payload)")
        block = src[start:end]
        for key in (
            '"signal_adjustment"',
            '"factor_parent_dataset_id"',
            '"formula_version"',
            '"anchor_policy"',
        ):
            assert key in block, f"{key} missing from _ex_payload"

    def test_run_meta_records_parent_dataset_ids(self):
        src = _read(SVC / "backtest_artifacts.py")
        assert '"raw_dataset_id"' in src
        assert '"factor_dataset_id"' in src


class TestDbLineageColumns:
    def test_migration_adds_lineage_columns(self):
        src = _read(SVC / "db.py")
        marker = "def _migrate_v1_to_v2("
        assert marker in src
        start = src.index(marker)
        nxt = src.find("\ndef ", start)
        fn_src = src[start: nxt if nxt != -1 else len(src)]
        assert "signal_raw_dataset_id" in fn_src
        assert "signal_factor_dataset_id" in fn_src

    def test_upsert_persists_lineage_columns(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)
            storage_root = str(tmp_path / "storage")

        cfg = FakeCfg()
        init_db(cfg)
        # KNOWN PRODUCTION GAP (not fixed here, tests must not alter prod
        # code): SCHEMA_SQL creates the runs table WITHOUT the
        # signal_raw_dataset_id / signal_factor_dataset_id columns, and a
        # fresh DB is stamped schema_version=2 directly, so
        # _migrate_v1_to_v2 never runs and upserts fail on new databases.
        # Exercise the supported v1 -> v2 upgrade path instead: downgrade the
        # stamp to 1 and re-run init_db so the migration adds the columns.
        conn = connect(cfg)
        conn.execute(
            "UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        init_db(cfg)
        row = {
            "run_id": "test_run_lineage_001",
            "title": "Derived qfq lineage",
            "status": "ok",
            "created_at": int(time.time()),
            "period": "DAY",
            "signal_data_source": "internal",
            "signal_adjustment": "tushare_factor_qfq",
            "dataset_id": "internal_tsfqfq_1d_20240112_abc123",
            "raw_dataset_id": "localvendor_none_1d_20240112_raw001",
            "factor_dataset_id": "tushare_adjfactor_1d_20240112_fac001",
        }
        upsert_run_from_index_row(cfg, row)
        conn = connect(cfg)
        r = conn.execute(
            "SELECT * FROM runs WHERE run_id='test_run_lineage_001'"
        ).fetchone()
        conn.close()
        assert r is not None
        assert r["signal_raw_dataset_id"] == "localvendor_none_1d_20240112_raw001"
        assert r["signal_factor_dataset_id"] == "tushare_adjfactor_1d_20240112_fac001"
        assert r["signal_data_source"] == "internal"
        assert r["signal_adjustment"] == "tushare_factor_qfq"


class TestReadPathZeroProviderDependency:
    @pytest.fixture(scope="class")
    def sync_mod(self):
        spec = importlib.util.spec_from_file_location(
            "sync_market_data_path_under_test", str(SYNC_SCRIPT)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _make_parents(self, store: DatasetStore):
        n = len(DATES)
        close = np.array([10.0 + i for i in range(n)])
        arrays = {
            "trade_date": np.array(DATES, dtype=np.int64),
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1000.0),
            "amount": np.full(n, 100000.0),
        }
        sha = store.store_bar_arrays(SYM_A, arrays)
        raw = DatasetManifest(
            dataset_id="localvendor_none_1d_20240112_offline",
            source="local_vendor", adjustment="none", period="1d",
            status="building",
            symbols=[SymbolRecord(
                symbol=SYM_A, blob_sha256=sha, first_date=DATES[0],
                last_date=DATES[-1], row_count=n, quality="ok")],
            symbol_count=1, row_count=n,
        )
        store.publish(raw)

        fsha = store.store_factors(SYM_A, [20240101, 20240108], [1.0, 1.25])
        fac = DatasetManifest(
            dataset_id="tushare_adjfactor_1d_20240112_offline",
            source="tushare", adjustment="adj_factor", period="1d",
            dataset_type="factor", status="building",
            symbols=[SymbolRecord(
                symbol=SYM_A, blob_sha256=fsha, first_date=20240101,
                last_date=20240108, row_count=2, quality="ok")],
            symbol_count=1,
        )
        store.publish(fac)
        return raw.dataset_id, fac.dataset_id

    def test_derive_and_load_never_touch_provider(
        self, tmp_path, monkeypatch, sync_mod
    ):
        from wtpy.apps.astock.data.providers.tushare import TushareProvider

        def _forbidden(*_a, **_kw):
            raise AssertionError(
                "Provider must never be called on the dataset read path")

        monkeypatch.setattr(TushareProvider, "fetch_bars", _forbidden)
        monkeypatch.setattr(TushareProvider, "fetch_adj_factor", _forbidden)
        monkeypatch.setattr(TushareProvider, "_ensure_initialized", _forbidden)

        store = DatasetStore(tmp_path / "market_data")
        raw_id, fac_id = self._make_parents(store)

        args = SimpleNamespace(
            raw_dataset_id=raw_id, factor_dataset_id=fac_id, cutoff=None,
            log_path=None, report_path=None,
        )
        res = sync_mod.derive_tushare_factor_qfq(args, store)
        assert res["status"] == "success"
        assert res["dataset_status"] == "ready"

        repo = MarketDataRepository(store)
        bars = repo.load_bars(dataset_id=res["dataset_id"], symbol=SYM_A)
        assert len(bars) == len(DATES)
        assert bars[0].trade_date == DATES[0]
        # anchor factor 1.25 -> first day scaled by 1.0/1.25
        assert bars[0].close == pytest.approx(round(10.0 * (1.0 / 1.25), 4))
        assert bars[-1].close == pytest.approx(round(19.0 * 1.0, 4))
        assert bars[0].source == "internal"
        assert bars[0].adjustment == "tushare_factor_qfq"
