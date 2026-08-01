"""Integration tests: multi-source fields through real production paths."""
import pytest
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.repository import MarketDataRepository, DatasetNotReadyError
from wtpy.apps.astock.data.providers.base import MarketBar
from wtpy.apps.astock.service.backtest_request import BacktestRequest
from wtpy.apps.astock.service.db import init_db, connect, upsert_run_from_index_row


@pytest.fixture
def ready_dataset(tmp_path):
    store = DatasetStore(tmp_path / "market_data")
    bars = [
        MarketBar(
            symbol="SSE.STK.600000", trade_date=20240101 + i, period="1d",
            open=10.0 + i, high=11.0 + i, low=9.0 + i, close=10.5 + i,
            volume=1000.0, amount=10000.0, source="tdxquant", adjustment="front",
        )
        for i in range(5)
    ]
    sha = store.store_bars("SSE.STK.600000", bars)
    m = DatasetManifest(
        dataset_id="tdxquant_front_1d_20260724_test001",
        source="tdxquant", adjustment="front", period="1d",
        status="building", data_cutoff_date=20260724,
        symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=5, quality="ok")],
        symbol_count=1, row_count=5,
    )
    store.publish(m)
    return store, "tdxquant_front_1d_20260724_test001"


@pytest.fixture
def partial_dataset(tmp_path):
    store = DatasetStore(tmp_path / "market_data")
    m = DatasetManifest(
        dataset_id="tdxquant_front_1d_partial001",
        source="tdxquant", adjustment="front", period="1d",
        status="building",
        symbols=[
            SymbolRecord(symbol="SSE.STK.600000", blob_sha256="", quality="error", error="timeout"),
        ],
        symbol_count=1, row_count=0,
    )
    store.publish(m)
    return store, "tdxquant_front_1d_partial001"


class TestDatasetResolvedAndLockedAtCreation:
    def test_resolve_locks_dataset_id(self, ready_dataset):
        store, ds_id = ready_dataset
        repo = MarketDataRepository(store)
        ds = repo.resolve_latest_ready(source="tdxquant", adjustment="front", period="1d")
        assert ds.dataset_id == ds_id
        assert ds.status == "ready"

    def test_missing_dataset_raises(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        repo = MarketDataRepository(store)
        from wtpy.apps.astock.data.repository import DatasetNotFoundError
        with pytest.raises(DatasetNotFoundError):
            repo.resolve_latest_ready(source="tdxquant", adjustment="front", period="1d")

    def test_partial_dataset_rejected_by_backtest(self, partial_dataset):
        store, ds_id = partial_dataset
        repo = MarketDataRepository(store)
        with pytest.raises(DatasetNotReadyError):
            repo.load_bars(dataset_id=ds_id)

    def test_partial_allowed_with_explicit_flag(self, partial_dataset):
        store, ds_id = partial_dataset
        repo = MarketDataRepository(store)
        bars = repo.load_bars(dataset_id=ds_id, allow_partial=True)
        assert bars == []


class TestRunsUpsertPersistsMultiSourceColumns:
    def test_upsert_writes_all_fields(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)
            storage_root = str(tmp_path / "storage")

        cfg = FakeCfg()
        init_db(cfg)
        row = {
            "run_id": "test_run_ms_001",
            "title": "Multi-source test",
            "status": "ok",
            "created_at": int(time.time()),
            "period": "DAY",
            "signal_data_source": "tdxquant",
            "signal_adjustment": "front",
            "dataset_id": "tdxquant_front_1d_20260724_abc123",
            "weekly_bar_mode": "local_aggregate",
            "execution_data_source": "tdx_local",
            "execution_dataset_id": "tdxlocal_none_1d_20260724_def456",
        }
        upsert_run_from_index_row(cfg, row)
        conn = connect(cfg)
        r = conn.execute("SELECT * FROM runs WHERE run_id='test_run_ms_001'").fetchone()
        conn.close()
        assert r["signal_data_source"] == "tdxquant"
        assert r["signal_adjustment"] == "front"
        assert r["dataset_id"] == "tdxquant_front_1d_20260724_abc123"
        assert r["weekly_bar_mode"] == "local_aggregate"
        assert r["execution_data_source"] == "tdx_local"
        assert r["execution_dataset_id"] == "tdxlocal_none_1d_20260724_def456"

    def test_upsert_idempotent(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)
            storage_root = str(tmp_path / "storage")

        cfg = FakeCfg()
        init_db(cfg)
        row = {
            "run_id": "test_run_idem",
            "title": "Idem",
            "status": "ok",
            "created_at": int(time.time()),
            "signal_data_source": "tushare",
            "signal_adjustment": "qfq",
            "dataset_id": "tushare_qfq_1d_20260724_xyz",
            "weekly_bar_mode": "vendor_native",
            "execution_data_source": "tdx_local",
        }
        upsert_run_from_index_row(cfg, row)
        upsert_run_from_index_row(cfg, row)
        conn = connect(cfg)
        r = conn.execute("SELECT * FROM runs WHERE run_id='test_run_idem'").fetchone()
        conn.close()
        assert r["signal_data_source"] == "tushare"
        assert r["dataset_id"] == "tushare_qfq_1d_20260724_xyz"

    def test_legacy_run_gets_defaults(self, tmp_path):
        class FakeCfg:
            output_root = str(tmp_path)
            storage_root = str(tmp_path / "storage")

        cfg = FakeCfg()
        init_db(cfg)
        row = {
            "run_id": "test_run_legacy",
            "title": "Legacy",
            "status": "ok",
            "created_at": int(time.time()),
        }
        upsert_run_from_index_row(cfg, row)
        conn = connect(cfg)
        r = conn.execute("SELECT * FROM runs WHERE run_id='test_run_legacy'").fetchone()
        conn.close()
        assert r["signal_data_source"] is None
        assert r["dataset_id"] is None


class TestExperimentRealDualSourceExpansion:
    def test_dual_source_generates_two_variants_per_base(self):
        from wtpy.apps.astock.service.experiments import expand_param_grid_unified

        plan = expand_param_grid_unified(
            rule_ids=["rule_a"],
            period="DAY",
            codes=["SSE.STK.600000"],
            start=20200101,
            end=20240701,
        )
        base_variants = plan["variants"]
        assert len(base_variants) >= 1

        from wtpy.apps.astock.data.providers.base import DataSource, AdjustmentMode
        dual_variants = []
        for src, adj in [
            (DataSource.TDXQUANT, AdjustmentMode.FRONT),
            (DataSource.TUSHARE, AdjustmentMode.QFQ),
        ]:
            for v in base_variants:
                dv = dict(v)
                dv["signal_data_source"] = src.value
                dv["signal_adjustment"] = adj.value
                dv["dataset_id"] = None
                dual_variants.append(dv)

        assert len(dual_variants) == 2 * len(base_variants)
        assert dual_variants[0]["signal_data_source"] == "tdxquant"
        assert dual_variants[1]["signal_data_source"] == "tushare"
        assert dual_variants[0]["rule_ids"] == dual_variants[1]["rule_ids"]
        assert dual_variants[0]["codes"] == dual_variants[1]["codes"]
        assert dual_variants[0]["start"] == dual_variants[1]["start"]

    def test_create_experiment_with_dual_source(self, tmp_path):
        from wtpy.apps.astock.config import AStockConfig
        from wtpy.apps.astock.service.experiments import create_experiment_from_grid
        from wtpy.apps.astock.service import db as exp_db

        cfg = AStockConfig(
            storage_root=str(tmp_path / "storage"),
            output_root=str(tmp_path / "output"),
            tdx_root=str(tmp_path / "tdx"),
        )
        cfg.ensure_dirs()

        # Gate C D2: the dual-source template is now
        # (tdxquant/front) + (internal/tushare_factor_qfq) with a shared
        # raw execution dataset — not the retired (tushare, qfq) pair.
        md_store = DatasetStore(tmp_path / "storage" / "market_data")
        for src, adj in [
            ("tdxquant", "front"),
            ("internal", "tushare_factor_qfq"),
            ("local_vendor", "none"),
        ]:
            bars = [
                MarketBar(
                    symbol="SSE.STK.600000", trade_date=20240101, period="1d",
                    open=10.0, high=11.0, low=9.0, close=10.5,
                    volume=1000.0, amount=10000.0, source=src, adjustment=adj,
                )
            ]
            sha = md_store.store_bars("SSE.STK.600000", bars)
            m = DatasetManifest(
                dataset_id=f"{src}_{adj}_1d_test",
                source=src, adjustment=adj, period="1d",
                status="building", data_cutoff_date=20260724,
                symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=1, quality="ok", first_date=20240101, last_date=20240101)],
                symbol_count=1, row_count=1,
            )
            md_store.publish(m)

        result = create_experiment_from_grid(
            cfg,
            name="dual_test",
            rule_ids=["ma_cross"],
            codes=["SSE.STK.600000"],
            start=20200101,
            end=20240701,
            execution_data_source="local_vendor",
            dual_source_compare=True,
            force=True,
        )
        exp_id = result.get("experiment_id")
        assert exp_id is not None
        exp_row = exp_db.get_experiment(cfg, exp_id)
        config = exp_row.get("config") or {}
        assert config.get("dual_source_compare") is True
        assert (config.get("common_universe") or {}).get("common_universe_count") == 1
        variants = exp_row.get("variants") or []
        params_list = [v.get("params") for v in variants if v.get("params")]
        sources = {p.get("signal_data_source") for p in params_list}
        assert "tdxquant" in sources
        assert "internal" in sources
        ds_ids = {p.get("dataset_id") for p in params_list}
        assert None not in ds_ids
        exec_ids = {p.get("execution_dataset_id") for p in params_list}
        assert exec_ids == {"local_vendor_none_1d_test"}

    def test_dual_source_fails_without_ready_datasets(self, tmp_path):
        from wtpy.apps.astock.config import AStockConfig
        from wtpy.apps.astock.service.experiments import create_experiment_from_grid

        cfg = AStockConfig(
            storage_root=str(tmp_path / "storage"),
            output_root=str(tmp_path / "output"),
            tdx_root=str(tmp_path / "tdx"),
        )
        cfg.ensure_dirs()

        with pytest.raises(ValueError, match="ready dataset"):
            create_experiment_from_grid(
                cfg,
                name="dual_fail",
                rule_ids=["ma_cross"],
                codes=["SSE.STK.600000"],
                dual_source_compare=True,
                force=True,
            )


class TestSignalCacheFinalKeySourceIsolation:
    def test_production_key_differs_by_source(self):
        from wtpy.apps.astock.research.signal_cache import signal_cache_key

        common = dict(
            indicator_ids=["rule_a"],
            period="DAY",
            start=20200101,
            end=20240701,
            universe_hash="univ1",
            adjust_mode="asof_forward_qfq",
            factor_manifest_sha="abc",
        )
        k_tdx = signal_cache_key(**common, data_source="tdxquant", dataset_id="ds_tdx")
        k_ts = signal_cache_key(**common, data_source="tushare", dataset_id="ds_ts")
        k_legacy = signal_cache_key(**common)
        assert k_tdx != k_ts
        assert k_tdx != k_legacy
        assert k_ts != k_legacy

    def test_production_key_differs_by_dataset(self):
        from wtpy.apps.astock.research.signal_cache import signal_cache_key

        common = dict(
            indicator_ids=["rule_a"],
            period="DAY",
            start=20200101,
            end=20240701,
            universe_hash="univ1",
            adjust_mode="asof_forward_qfq",
            data_source="tdxquant",
        )
        k1 = signal_cache_key(**common, dataset_id="ds_old")
        k2 = signal_cache_key(**common, dataset_id="ds_new")
        assert k1 != k2


class TestExecutionCacheFinalKeyDatasetIsolation:
    def test_production_key_differs_by_signal_dataset(self):
        from wtpy.apps.astock.research.execution_cache import execution_cache_key

        base = {"engine": "full", "rule_ids": ["r"], "period": "DAY", "start": 1, "end": 2}
        k1 = execution_cache_key({**base, "signal_dataset_id": "ds1", "signal_data_source": "tdxquant"})
        k2 = execution_cache_key({**base, "signal_dataset_id": "ds2", "signal_data_source": "tdxquant"})
        assert k1 != k2

    def test_production_key_differs_by_source(self):
        from wtpy.apps.astock.research.execution_cache import execution_cache_key

        base = {"engine": "full", "rule_ids": ["r"], "period": "DAY", "start": 1, "end": 2}
        k1 = execution_cache_key({**base, "signal_data_source": "tdxquant", "signal_dataset_id": "ds"})
        k2 = execution_cache_key({**base, "signal_data_source": "tushare", "signal_dataset_id": "ds"})
        assert k1 != k2


class TestBacktestReadsRepositoryForTdxquant:
    def test_backtest_uses_repository_when_source_tdxquant(self, ready_dataset):
        store, ds_id = ready_dataset
        repo = MarketDataRepository(store)
        bars = repo.load_bars(dataset_id=ds_id, symbol="SSE.STK.600000")
        assert len(bars) == 5
        assert bars[0].source == "tdxquant"
        assert bars[0].adjustment == "front"

    def test_backtest_uses_repository_when_source_tushare(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="SSE.STK.600000", trade_date=20240101 + i, period="1d",
                open=10.0, high=11.0, low=9.0, close=10.5,
                volume=1000.0, amount=10000.0, source="tushare", adjustment="qfq",
            )
            for i in range(3)
        ]
        sha = store.store_bars("SSE.STK.600000", bars)
        m = DatasetManifest(
            dataset_id="tushare_qfq_1d_test001",
            source="tushare", adjustment="qfq", period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
            symbol_count=1, row_count=3,
        )
        store.publish(m)
        repo = MarketDataRepository(store)
        loaded = repo.load_bars(dataset_id="tushare_qfq_1d_test001", symbol="SSE.STK.600000")
        assert len(loaded) == 3
        assert loaded[0].source == "tushare"


class TestFullBacktestNeverCallsProvider:
    def test_repository_load_does_not_call_provider(self, ready_dataset):
        store, ds_id = ready_dataset
        repo = MarketDataRepository(store)
        from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
        from wtpy.apps.astock.data.providers.tushare import TushareProvider
        with patch.object(TdxQuantProvider, "fetch_bars", side_effect=AssertionError("PROVIDER CALLED")):
            with patch.object(TushareProvider, "fetch_bars", side_effect=AssertionError("PROVIDER CALLED")):
                bars = repo.load_bars(dataset_id=ds_id, symbol="SSE.STK.600000")
                assert len(bars) == 5


class TestMissingDatasetFailsWithoutFallback:
    def test_no_silent_fallback_on_missing(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        repo = MarketDataRepository(store)
        from wtpy.apps.astock.data.repository import DatasetNotFoundError
        with pytest.raises(DatasetNotFoundError):
            repo.resolve_latest_ready(source="tdxquant", adjustment="front", period="1d")

    def test_building_dataset_rejected(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        m = DatasetManifest(
            dataset_id="ds_building",
            source="tdxquant", adjustment="front", period="1d",
            status="building",
        )
        store.save_manifest(m)
        repo = MarketDataRepository(store)
        with pytest.raises(DatasetNotReadyError):
            repo.load_bars(dataset_id="ds_building")


class TestWeeklyBarModeEndToEnd:
    def test_request_carries_weekly_mode(self):
        req = BacktestRequest(
            rule_ids=["r"],
            signal_data_source="tdxquant",
            weekly_bar_mode="vendor_native",
        )
        assert req.weekly_bar_mode == "vendor_native"
        d = req.to_dict()
        assert d["weekly_bar_mode"] == "vendor_native"

    def test_cache_key_includes_weekly_mode(self):
        from wtpy.apps.astock.research.signal_cache import signal_cache_key
        common = dict(
            indicator_ids=["r"], period="WEEK", start=1, end=2,
            universe_hash="u", adjust_mode="asof", data_source="tdxquant",
            dataset_id="ds1",
        )
        k1 = signal_cache_key(**common, weekly_bar_mode="local_aggregate")
        k2 = signal_cache_key(**common, weekly_bar_mode="vendor_native")
        assert k1 != k2


class TestSymbolFormatResolution:
    def test_sse_stk_finds_sh_format(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="600000.SH", trade_date=20240101, period="1d",
                open=10.0, high=11.0, low=9.0, close=10.5,
                volume=1000.0, amount=10000.0, source="tdxquant", adjustment="front",
            )
        ]
        sha = store.store_bars("600000.SH", bars)
        m = DatasetManifest(
            dataset_id="ds_sym_test",
            source="tdxquant", adjustment="front", period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="600000.SH", blob_sha256=sha, row_count=1, quality="ok")],
            symbol_count=1, row_count=1,
        )
        store.publish(m)
        repo = MarketDataRepository(store)
        loaded = repo.load_bars(dataset_id="ds_sym_test", symbol="SSE.STK.600000")
        assert len(loaded) == 1
        assert loaded[0].close == 10.5

    def test_szse_stk_finds_sz_format(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="000001.SZ", trade_date=20240101, period="1d",
                open=5.0, high=6.0, low=4.0, close=5.5,
                volume=2000.0, amount=11000.0, source="tushare", adjustment="qfq",
            )
        ]
        sha = store.store_bars("000001.SZ", bars)
        m = DatasetManifest(
            dataset_id="ds_sym_test2",
            source="tushare", adjustment="qfq", period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="000001.SZ", blob_sha256=sha, row_count=1, quality="ok")],
            symbol_count=1, row_count=1,
        )
        store.publish(m)
        repo = MarketDataRepository(store)
        loaded = repo.load_bars(dataset_id="ds_sym_test2", symbol="SZSE.STK.000001")
        assert len(loaded) == 1

    def test_bare_code_finds_std_format(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="SSE.STK.601088", trade_date=20240101, period="1d",
                open=20.0, high=21.0, low=19.0, close=20.5,
                volume=3000.0, amount=60000.0, source="tdxquant", adjustment="front",
            )
        ]
        sha = store.store_bars("SSE.STK.601088", bars)
        m = DatasetManifest(
            dataset_id="ds_sym_test3",
            source="tdxquant", adjustment="front", period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.601088", blob_sha256=sha, row_count=1, quality="ok")],
            symbol_count=1, row_count=1,
        )
        store.publish(m)
        repo = MarketDataRepository(store)
        loaded = repo.load_bars(dataset_id="ds_sym_test3", symbol="601088")
        assert len(loaded) == 1

    def test_missing_symbol_still_raises(self, tmp_path):
        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="600000.SH", trade_date=20240101, period="1d",
                open=10.0, high=11.0, low=9.0, close=10.5,
                volume=1000.0, amount=10000.0,
            )
        ]
        sha = store.store_bars("600000.SH", bars)
        m = DatasetManifest(
            dataset_id="ds_sym_test4",
            source="tdxquant", adjustment="front", period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="600000.SH", blob_sha256=sha, row_count=1, quality="ok")],
            symbol_count=1, row_count=1,
        )
        store.publish(m)
        repo = MarketDataRepository(store)
        from wtpy.apps.astock.data.repository import DatasetNotFoundError
        with pytest.raises(DatasetNotFoundError):
            repo.load_bars(dataset_id="ds_sym_test4", symbol="SZSE.STK.999999")
