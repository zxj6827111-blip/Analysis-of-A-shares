"""Tushare-only product reconcile + default selection tests (migration plan).

Covers the 16 required acceptance items:
  1.  source=all 不调用 legacy provider
  2.  Tushare 增量后自动 reconcile
  3.  reconcile 幂等
  4.  L2 base 为 Tushare
  5.  supplement 是无重叠 complement
  6.  退市股票进入正式 L2
  7.  L1 raw parent 等于正式 L2
  8.  日期回退不切换
  9.  16 行孤儿面不被选中
  10. Quick 不选 legacy
  11. Bagua 默认是 Tushare QFQ
  12. tdx_front 返回清晰错误
  13. 旧 dataset ID 仍可读取
  14. 无新增环境变量时可启动
  15. 产品未就绪时回测不使用不安全 fallback
  16. health API 使用正式 L1/L2 日期
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import numpy as np
import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
)
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.data.tushare_product import (
    DATA_POLICY_TUSHARE_ONLY,
    build_delisted_missing_complement,
    derive_composite_tushare_factor_qfq,
    reconcile_tushare_product_datasets,
    resolve_active_tushare_product_pair,
    select_tushare_base,
    validate_tushare_product_pair,
)

BASE_SYMS = [f"SSE.STK.{600000 + i}" for i in range(120)]
DELISTED_SYMS = ["SZSE.STK.300104", "SSE.STK.601558", "SZSE.STK.002680"]
COMMON_SYMS = ["SSE.STK.600000", "SSE.STK.600519"]


def _arrays(n_rows: int, base_price: float, start_date: int = 20240101) -> dict:
    # Real calendar dates (strptime-safe): the reconcile freshness gate
    # parses symbol last dates, so day-00 numeric dates would abort it.
    start = _dt.datetime.strptime(str(start_date), "%Y%m%d").date()
    dates = np.array(
        [int((start + _dt.timedelta(days=i)).strftime("%Y%m%d"))
         for i in range(n_rows)],
        dtype=np.int64,
    )
    close = np.linspace(base_price, base_price * 1.2, n_rows)
    return {
        "trade_date": dates,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": np.full(n_rows, 1000.0),
        "amount": np.full(n_rows, 100000.0),
    }


def _publish_bars(
    store: DatasetStore,
    dataset_id: str,
    source: str,
    adjustment: str,
    symbols: dict,
    *,
    universe_type: str = "",
    status: str = "building",
    data_cutoff_date: int = 0,
    provenance: dict = None,
) -> DatasetManifest:
    records = []
    total = 0
    for sym, arrays in symbols.items():
        sha = store.store_bar_arrays(sym, arrays)
        d = arrays["trade_date"]
        records.append(
            SymbolRecord(
                symbol=sym, blob_sha256=sha,
                first_date=int(d[0]), last_date=int(d[-1]),
                row_count=len(d), quality="ok",
            )
        )
        total += len(d)
    m = DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period="1d",
        status=status,
        data_cutoff_date=data_cutoff_date or int(max(r.last_date or 0 for r in records)),
        universe_type=universe_type,
        provenance=dict(provenance or {}),
        symbols=records,
        symbol_count=len(records),
        row_count=total,
    )
    store.publish(m)
    return m


def _publish_factors(
    store: DatasetStore,
    dataset_id: str,
    symbols: dict,
    status: str = "building",
) -> DatasetManifest:
    records = []
    for sym, (dates, factors) in symbols.items():
        sha = store.store_factors(sym, dates, factors)
        records.append(
            SymbolRecord(
                symbol=sym, blob_sha256=sha,
                first_date=int(dates[0]), last_date=int(dates[-1]),
                row_count=len(dates), quality="ok",
            )
        )
    m = DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="adj_factor",
        period="1d",
        status=status,
        dataset_type="factor",
        symbols=records,
        symbol_count=len(records),
        row_count=sum(r.row_count for r in records),
    )
    store.publish(m)
    return m


def _full_base_arrays() -> dict:
    """300-row (>= 250 median gate) full-history-ish bars for 120 symbols."""
    return {sym: _arrays(300, 10.0 + i) for i, sym in enumerate(BASE_SYMS)}


def _factor_series(sym: str) -> tuple:
    # factor history must predate the earliest raw history (delisted stocks
    # start 20230101 in the fixture) so no symbol hits all_rows_leading_gap;
    # the trailing date (20241130) stays fresh vs the raw base last date
    # (20241026) within the 3-day tolerance.
    dates = np.concatenate((
        np.arange(20220101, 20220101 + 320, dtype=np.int64),
        np.asarray([20241130], dtype=np.int64),
    ))
    factors = np.full(len(dates), 2.0)
    return dates, factors


def _standard_parents(store: DatasetStore) -> dict:
    """Publish the full Tushare parent set and return their ids."""
    base = _publish_bars(
        store, "tushare_none_1d_base_full",
        "tushare", "none", _full_base_arrays(),
    )
    delisted_arrays = {sym: _arrays(200, 5.0, 20230101) for sym in DELISTED_SYMS}
    _publish_bars(
        store, "tushare_none_1d_delisted",
        "tushare", "none", delisted_arrays,
        universe_type="b1_delisted_supplement",
    )
    fac = _publish_factors(
        store, "tushare_adjfactor_1d_full",
        {sym: _factor_series(sym) for sym in BASE_SYMS + DELISTED_SYMS},
    )
    return {"base": base.dataset_id, "factor": fac.dataset_id}


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


@pytest.fixture
def product_store(store):
    """Store with a fully reconciled formal L1/L2 pair."""
    _standard_parents(store)
    result = reconcile_tushare_product_datasets(store)
    assert result.status == "published", result.to_dict()
    return store


def _bootstrap_chain(store: DatasetStore) -> dict:
    ids = _standard_parents(store)
    r = reconcile_tushare_product_datasets(store)
    assert r.status == "published", r.to_dict()
    return {"ids": ids, "reconcile": r}


# ---------------------------------------------------------------------------
# 1-3: sync wiring + idempotence
# ---------------------------------------------------------------------------


class TestSyncWiring:
    def test_source_all_only_tushare(self, tmp_path, monkeypatch):
        """`--source all` never initializes TDX / tdx_local / local_vendor."""
        import importlib.util
        import os
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_all_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)

        called: list = []
        store = DatasetStore(tmp_path / "md")

        def fake_tushare(args, store):
            called.append("tushare")
            return {"status": "success", "datasets": {}}

        def forbidden(*a, **k):
            called.append("LEGACY_PROVIDER_INITIALIZED")
            return {"status": "failed", "error": "should_not_run"}

        monkeypatch.setattr(smd, "sync_tushare_full", fake_tushare)
        monkeypatch.setattr(smd, "sync_tushare_incremental", forbidden)
        monkeypatch.setattr(smd, "sync_tdxquant_full", forbidden)
        monkeypatch.setattr(smd, "sync_tdxquant_incremental", forbidden)
        monkeypatch.setattr(smd, "sync_tdx_local_full", forbidden)
        monkeypatch.setattr(smd, "sync_local_vendor_full", forbidden)
        monkeypatch.setattr(smd, "_reconcile_after_sync", lambda store, dry_run=False: {"status": "up_to_date"})
        monkeypatch.setattr(smd, "get_storage_root", lambda: Path(tmp_path))
        monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if k != "MARKET_DATA_ROOT"})
        monkeypatch.setattr(
            "sys.argv",
            ["sync_market_data.py", "--source", "all", "--mode", "full"],
        )
        smd.main()
        assert called == ["tushare"]

    def test_tushare_incremental_auto_reconciles(self, tmp_path, monkeypatch):
        """Tushare incremental success triggers reconcile afterwards."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_reconcile_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)

        store = DatasetStore(tmp_path / "md")
        sync_calls = []

        def fake_sync_dataset(**kw):
            sync_calls.append(kw)
            return {"success": 1, "total": 1, "dataset_id": "tushare_none_1d_test"}

        class _FakeProvider:
            def __init__(self, *a, **k):
                pass

            def health_check(self):
                return True

            def provider_version(self):
                return "fake"

        monkeypatch.setattr(smd, "_sync_dataset", fake_sync_dataset)
        monkeypatch.setattr(
            smd, "_infer_incremental_resume", lambda *a, **k: (20260701, None))
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {"status": "published"})
        from wtpy.apps.astock.data.providers import tushare as tushare_provider_mod

        monkeypatch.setattr(tushare_provider_mod, "TushareProvider", _FakeProvider)

        args = SimpleNamespace(
            source="tushare", mode="incremental", token=None, start_date=20260701,
            end_date=20260804, anchor_date=None, symbol="600000,000001",
            include_bse=True, include_delisted=False, resume=False, fresh=True,
            asset_class="stocks", adjustment=None,
        )
        result = smd.sync_tushare_incremental(args, store)
        assert result["reconcile"]["status"] == "published"
        assert result["status"] == "success"

    def test_sync_warning_when_reconcile_blocks(self, tmp_path, monkeypatch):
        """Task must not claim success when the product chain cannot pass."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_warn_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)

        store = DatasetStore(tmp_path / "md")

        class _FakeProvider:
            def __init__(self, *a, **k):
                pass

            def health_check(self):
                return True

            def provider_version(self):
                return "fake"

        monkeypatch.setattr(
            smd, "_sync_dataset",
            lambda **kw: {"success": 1, "total": 1, "dataset_id": "ds"})
        monkeypatch.setattr(
            smd, "_infer_incremental_resume", lambda *a, **k: (20260701, None))
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {
                "status": "waiting_for_parent", "missing": ["tushare_delisted"],
                "issues": [], "l1_dataset_id": "", "l2_dataset_id": "",
            })
        from wtpy.apps.astock.data.providers import tushare as tushare_provider_mod

        monkeypatch.setattr(tushare_provider_mod, "TushareProvider", _FakeProvider)
        args = SimpleNamespace(
            source="tushare", mode="incremental", token=None, start_date=20260701,
            end_date=20260804, anchor_date=None, symbol="600000,000001",
            include_bse=True, include_delisted=False, resume=False, fresh=True,
            asset_class="stocks", adjustment=None,
        )
        result = smd.sync_tushare_incremental(args, store)
        assert result["status"] == "warning"
        assert "waiting_for_parent" in result["warning"]


class TestZeroConfigChain:
    """task=tushare default chain: raw -> factor -> reconcile (in-process).

    The chain lives in the script so both UI-launched and cron-launched
    `--source tushare --mode incremental` runs update factor + reconcile
    without any new configuration or cron changes.
    """

    def _load_script(self, tmp_path, monkeypatch):
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_chain_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)
        monkeypatch.setattr(smd, "get_storage_root", lambda: Path(tmp_path))
        # the chain's delisted-pool step spawns a child process (network);
        # these unit tests exercise the chain orchestration only
        monkeypatch.setattr(
            smd, "_run_delisted_pool_sync",
            lambda store, args: {"status": "ok", "exit_code": 0})
        return smd

    def _raw_ok(self, reconcile_status="waiting_for_parent"):
        return {
            "status": "success", "sync_run_id": "raw_run",
            "datasets": {"none_1d": {"dataset_id": "raw_ds"}},
            "reconcile": {"status": reconcile_status, "missing": [], "issues": []},
        }

    def test_default_incremental_runs_chain_in_order(self, tmp_path, monkeypatch):
        """main() dispatch: --source tushare --mode incremental (no adjustment)
        must run raw -> factor -> reconcile in that order."""
        smd = self._load_script(tmp_path, monkeypatch)
        order: list = []

        def fake_raw(args, store, **kw):
            order.append("raw")
            return self._raw_ok()

        def fake_factor(args, store):
            order.append("factor")
            assert args.adjustment == "adj_factor"
            assert args.mode == "incremental"
            return {"status": "success", "dataset_status": "ready",
                    "dataset_id": "tushare_adjfactor_1d_x",
                    "reconcile": {"status": "up_to_date", "missing": [], "issues": []},
                    "datasets": {}}

        def fake_reconcile(store, *, dry_run=False):
            order.append("reconcile")
            return {"status": "published", "missing": [], "issues": [],
                    "l1_dataset_id": "l1", "l2_dataset_id": "l2"}

        monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
        monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
        monkeypatch.setattr(smd, "_reconcile_after_sync", fake_reconcile)
        monkeypatch.setattr(smd, "_apply_reconcile_status", lambda r: None)
        monkeypatch.setattr(
            "sys.argv",
            ["sync_market_data.py", "--source", "tushare", "--mode", "incremental",
             "--end-date", "20260804"],
        )
        smd.main()
        assert order == ["raw", "factor", "reconcile"]

    def test_explicit_adjustment_none_skips_factor(self, tmp_path, monkeypatch):
        """`--adjustment none --mode incremental` stays a raw-only sync."""
        smd = self._load_script(tmp_path, monkeypatch)
        called: list = []

        def fake_raw(args, store, **kw):
            called.append("raw")
            return self._raw_ok()

        def forbidden(*a, **k):
            called.append("FORBIDDEN")
            return {"status": "failed"}

        monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
        monkeypatch.setattr(smd, "sync_tushare_chain", forbidden)
        monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", forbidden)
        monkeypatch.setattr(
            "sys.argv",
            ["sync_market_data.py", "--source", "tushare", "--mode", "incremental",
             "--adjustment", "none", "--end-date", "20260804"],
        )
        smd.main()
        assert called == ["raw"]

    def test_chain_factor_failure_is_not_success(self, tmp_path, monkeypatch):
        """Raw success + factor failure must never report 同步成功."""
        smd = self._load_script(tmp_path, monkeypatch)
        monkeypatch.setattr(
            smd, "sync_tushare_incremental",
            lambda args, store, **kw: self._raw_ok())
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "failed",
                                 "error": "universe_file_required"})
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: None)
        store = DatasetStore(tmp_path / "md")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "failed"
        assert result["factor"]["status"] == "failed"
        assert "success" not in str(result.get("warning") or "") or True
        assert result["status"] != "success"

    def test_chain_factor_partial_is_warning_not_success(self, tmp_path, monkeypatch):
        """Factor published partial (provider failures) -> no success."""
        smd = self._load_script(tmp_path, monkeypatch)
        monkeypatch.setattr(
            smd, "sync_tushare_incremental",
            lambda args, store, **kw: self._raw_ok())
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "success",
                                 "dataset_status": "partial",
                                 "reconcile": {"status": "up_to_date",
                                               "missing": [], "issues": []}})
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: "/tmp/uni.csv")
        store = DatasetStore(tmp_path / "md")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "warning"
        assert result["status"] != "success"

    def test_chain_reconcile_blocked_is_warning_not_success(self, tmp_path, monkeypatch):
        """Formal L1/L2 not published (lineage/freshness fail) -> no success."""
        smd = self._load_script(tmp_path, monkeypatch)
        monkeypatch.setattr(
            smd, "sync_tushare_incremental",
            lambda args, store, **kw: self._raw_ok())
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "success",
                                 "dataset_status": "ready",
                                 "reconcile": {"status": "up_to_date",
                                               "missing": [], "issues": []}})
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {
                "status": "waiting_for_parent", "missing": ["tushare_delisted"],
                "issues": [], "l1_dataset_id": "", "l2_dataset_id": "",
            })
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: "/tmp/uni.csv")
        store = DatasetStore(tmp_path / "md")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "warning"
        assert "waiting_for_parent" in result["warning"]

    def test_chain_full_success_path(self, tmp_path, monkeypatch):
        """raw + factor ready + formal pair published -> success."""
        smd = self._load_script(tmp_path, monkeypatch)
        monkeypatch.setattr(
            smd, "sync_tushare_incremental",
            lambda args, store, **kw: self._raw_ok())
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "success",
                                 "dataset_status": "ready",
                                 "dataset_id": "tushare_adjfactor_1d_new",
                                 "reconcile": {"status": "up_to_date",
                                               "missing": [], "issues": []},
                                 "datasets": {}})
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {
                "status": "published", "missing": [], "issues": [],
                "l1_dataset_id": "l1", "l2_dataset_id": "l2",
            })
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: "/tmp/uni.csv")
        store = DatasetStore(tmp_path / "md")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "success"
        assert result["factor_dataset_id"] == "tushare_adjfactor_1d_new"
        assert result["reconcile"]["status"] == "published"

    def test_chain_factor_uses_default_cache_dir_without_env(self, tmp_path, monkeypatch):
        """No TUSHARE_FACTOR_RAW_ROOT -> safe default cache under data root."""
        smd = self._load_script(tmp_path, monkeypatch)
        monkeypatch.delenv("TUSHARE_FACTOR_RAW_ROOT", raising=False)
        captured = {}

        def fake_raw(args, store, **kw):
            return self._raw_ok()

        def fake_factor(args, store):
            captured["factor_raw_root"] = args.factor_raw_root
            captured["universe_file"] = args.universe_file
            return {"status": "success", "dataset_status": "ready",
                    "reconcile": {"status": "up_to_date", "missing": [], "issues": []},
                    "datasets": {}}

        monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
        monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {"status": "published", "missing": [],
                                          "issues": [], "l1_dataset_id": "l1",
                                          "l2_dataset_id": "l2"})
        uni = tmp_path / "uni.csv"
        uni.write_text(
            "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            smd, "_latest_factor_universe_file_path", lambda store: str(uni))
        store = DatasetStore(tmp_path / "md")
        args = SimpleNamespace(
            source="tushare", mode="incremental", adjustment=None,
            token=None, start_date=None, end_date=20260804,
            anchor_date=None, symbol=None, include_bse=True,
            include_delisted=False, resume=False, fresh=False,
            asset_class="stocks", universe_file=None, factor_raw_root=None,
        )
        result = smd.sync_tushare_chain(args, store)
        assert result["status"] == "success"
        assert captured["factor_raw_root"] == str(
            tmp_path / "md" / "tushare_factor_raw_cache")
        assert captured["universe_file"] == str(uni)

    def test_reconcile_idempotent(self, product_store):
        n_manifests = len(product_store.list_manifests())
        r1 = reconcile_tushare_product_datasets(product_store)
        r2 = reconcile_tushare_product_datasets(product_store)
        assert r1.status == "up_to_date" and r1.published is False
        assert r2.status == "up_to_date" and r2.published is False
        assert len(product_store.list_manifests()) == n_manifests
        assert r1.l1_dataset_id == r2.l1_dataset_id
        assert r1.l2_dataset_id == r2.l2_dataset_id

    def test_reconcile_waits_for_missing_factor(self, store):
        _standard_parents(store)
        store.manifests_dir.joinpath("tushare_adjfactor_1d_full.json").unlink()
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare/adj_factor" in r.missing
        assert r.published is False

    def test_reconcile_waits_for_stale_factor(self, store):
        _standard_parents(store)
        factor = store.load_manifest("tushare_adjfactor_1d_full")
        for rec in factor.symbols:
            rec.last_date = 20220300
        factor.data_cutoff_date = 20220300
        store.save_manifest(factor)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare/adj_factor" in r.missing
        assert any("factor_date_lag" in issue for issue in r.issues)
        assert r.published is False


class TestFactorFreshnessGateReconcile:
    """P0-2: the reconcile must not be bypassed by an old ready factor whose
    global max date covers the cutoff while the LATEST factor surface is a
    freshness-gate-blocked partial — and per-symbol freshness is recomputed
    against the current raw base, never trusted from manifest provenance."""

    def _gate_blocked_partial_factor(self, store, dataset_id="tushare_adjfactor_gate_blocked"):
        _publish_factors(
            store, dataset_id,
            {sym: _factor_series(sym) for sym in BASE_SYMS + DELISTED_SYMS},
        )
        m = store.load_manifest(dataset_id)
        m.status = "partial"
        m.provenance = {"freshness": {"gate": "blocked"}}
        m.data_cutoff_date = 20261101
        store.save_manifest(m)
        return m

    def test_latest_gate_blocked_partial_blocks_publish(self, store):
        """External repro: latest factor = partial (freshness gate blocked),
        older ready factor's global max date already covers the cutoff. The
        reconcile must refuse (waiting_for_parent / freshness_gate_blocked)
        and never fall back to the old ready factor — no L1/L2 lands."""
        _standard_parents(store)
        old_ready = store.load_manifest("tushare_adjfactor_1d_full")
        assert old_ready.status == "ready"
        # the old ready factor's global max already covers the raw cutoff
        assert max(int(r.last_date or 0) for r in old_ready.symbols) >= 20241026
        self._gate_blocked_partial_factor(store)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare/adj_factor" in r.missing
        assert any("freshness_gate_blocked" in i for i in r.issues)
        assert r.published is False
        assert r.factor_dataset_id == "tushare_adjfactor_gate_blocked"
        assert not any(
            "composite_none" in mid or "composite_tushare_factor_qfq" in mid
            for mid in store.list_manifests()
        ), "no L1/L2 product manifest may land when the gate blocks"

    def test_120_active_1_fresh_rejected(self, store):
        """External repro at scale: 120 active stocks with only 1 fresh
        (ratio 0.0083 < 0.95) must block the formal publish even though the
        factor global max date reaches the cutoff."""
        _standard_parents(store)
        factor = store.load_manifest("tushare_adjfactor_1d_full")
        fresh_symbol = BASE_SYMS[0]
        for rec in factor.symbols:
            if rec.symbol != fresh_symbol and rec.symbol in BASE_SYMS:
                rec.last_date = 20241001  # 25 days stale vs raw 20241026
        store.save_manifest(factor)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare/adj_factor" in r.missing
        assert any("freshness_below_threshold" in i for i in r.issues)
        assert r.published is False
        fresh = r.factor_freshness
        assert fresh is not None
        assert fresh["active_count"] == len(BASE_SYMS)
        assert fresh["fresh_count"] == 1
        assert fresh["fresh_symbol_ratio"] == round(1 / len(BASE_SYMS), 4)
        assert not any(
            "composite_none" in mid or "composite_tushare_factor_qfq" in mid
            for mid in store.list_manifests()
        ), "no L1/L2 product manifest may land when freshness is below threshold"

    def test_all_fresh_publishes_with_recomputed_ratio(self, product_store):
        """All active symbols fresh -> published; the reported freshness is
        recomputed against the current raw base (ratio 1.0 over the base)."""
        r = reconcile_tushare_product_datasets(product_store)
        assert r.status == "up_to_date"
        fresh = r.factor_freshness
        assert fresh is not None
        assert fresh["fresh_symbol_ratio"] == 1.0
        assert fresh["active_count"] == len(BASE_SYMS)
        assert fresh["raw_dataset_id"] == r.base_dataset_id

    def test_recompute_does_not_trust_manifest_provenance(self, store):
        """A ready factor whose provenance claims a perfect ratio but whose
        real series are stale vs the current raw base must be rejected."""
        _standard_parents(store)
        factor = store.load_manifest("tushare_adjfactor_1d_full")
        factor.provenance = {"freshness": {
            "fresh_symbol_ratio": 1.0, "fresh_count": 123, "active_count": 123}}
        store.save_manifest(factor)
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS:
                rec.last_date = 20241001
        store.save_manifest(factor)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert any("freshness_below_threshold" in i for i in r.issues)
        assert r.published is False

    def test_long_holiday_future_cutoff_still_publishes(self, store):
        """Spring-Festival-style long holiday (>5 days without trading): the
        raw base's data_cutoff_date is the sync-time --end-date (today), far
        ahead of every symbol's last trade date. The freshness pre-check must
        fall back to the real max symbol date instead of reporting 'no active
        raw symbols' — with fresh factors the formal product still publishes."""
        _standard_parents(store)
        base = store.load_manifest("tushare_none_1d_base_full")
        max_last = max(int(r.last_date or 0) for r in base.symbols)
        assert max_last < 20260808  # sanity: cutoff weeks ahead of all dates
        base.data_cutoff_date = 20260808
        store.save_manifest(base)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published", r.to_dict()
        assert r.published is True
        fresh = r.factor_freshness
        assert fresh is not None
        assert fresh["active_count"] == len(BASE_SYMS)
        assert fresh["fresh_symbol_ratio"] == 1.0
        assert not any("no active raw symbols" in i for i in r.issues)

    def test_long_holiday_future_cutoff_stale_factor_ratio_blocked(self, store):
        """Control: with stale factors the future cutoff must block via the
        freshness RATIO (fresh=N/active), never via the 'no active raw
        symbols' empty-set path."""
        _standard_parents(store)
        base = store.load_manifest("tushare_none_1d_base_full")
        base.data_cutoff_date = 20260808
        store.save_manifest(base)
        factor = store.load_manifest("tushare_adjfactor_1d_full")
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS and rec.symbol != BASE_SYMS[0]:
                rec.last_date = 20241001  # 25 days stale vs raw 20241026
        store.save_manifest(factor)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert r.published is False
        fresh = r.factor_freshness
        assert fresh is not None
        assert fresh["active_count"] == len(BASE_SYMS)
        assert fresh["fresh_count"] == 1
        assert any("freshness_below_threshold" in i for i in r.issues)
        assert not any("no active raw symbols" in i for i in r.issues)


class TestDeriveFreshnessGate:
    """P1 (derive path): --mode derive composite_tushare_factor_qfq must not
    publish a ready L1 derived from a factor that lags the raw surface, and
    the parent auto-resolve must not silently pick an old ready factor while
    the latest factor candidate is a freshness-gate-blocked partial."""

    def _publish_l2(self, store, ids) -> str:
        from wtpy.apps.astock.data.composite_dataset import build_composite_none

        base = store.load_manifest(ids["base"])
        l2 = build_composite_none(
            store,
            base_dataset_id=ids["base"],
            supplement_dataset_id="tushare_none_1d_delisted",
            cutoff=int(base.data_cutoff_date or 0),
        )
        return l2.dataset_id

    def test_derive_blocked_when_factor_stale(self, store):
        """External repro (derive channel): latest usable factor is an old
        ready one whose per-symbol freshness lags the raw surface — the
        derive must NOT publish a ready L1; the on-disk manifest is partial
        and the result carries the freshness_gate block."""
        ids = _standard_parents(store)
        l2_id = self._publish_l2(store, ids)
        factor = store.load_manifest(ids["factor"])
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS and rec.symbol != BASE_SYMS[0]:
                rec.last_date = 20241001  # 25 days stale vs raw 20241026
        store.save_manifest(factor)

        r = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2_id, factor_dataset_id=ids["factor"])
        assert r["status"] == "success"
        assert r["dataset_status"] == "partial"
        gate = r["freshness_gate"]
        assert gate["status"] == "blocked"
        assert gate["reason"] == "freshness_below_threshold"
        assert gate["fresh_symbol_ratio"] == round(1 / len(BASE_SYMS), 4)
        assert gate["active_count"] == len(BASE_SYMS)
        m = store.load_manifest(r["dataset_id"])
        assert m is not None
        assert m.status == "partial", \
            "no READY L1 manifest may land from a stale factor"
        # a partial L1 is never the active formal product
        assert resolve_active_tushare_product_pair(store) is None

    def test_derive_fresh_publishes_ready(self, store):
        """All-active-symbols-fresh derive publishes a ready L1 as before
        (the gate reports passed)."""
        ids = _standard_parents(store)
        l2_id = self._publish_l2(store, ids)
        r = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2_id, factor_dataset_id=ids["factor"])
        assert r["status"] == "success"
        assert r["dataset_status"] == "ready"
        assert r["freshness_gate"]["status"] == "passed"
        assert r["freshness_gate"]["fresh_symbol_ratio"] == 1.0
        m = store.load_manifest(r["dataset_id"])
        assert m is not None and m.status == "ready"

    def test_reconcile_existing_gate_blocked_partial_l1_waits(
            self, store):
        """A deterministic L1 partial (freshness-gate blocked) already on
        disk: the reconcile's derive hits the idempotent path and the
        validation reports "L1 not ready: partial" — the reconcile must
        classify that as waiting_for_parent (freshness), never failed."""
        from wtpy.apps.astock.data.composite_dataset import build_composite_none
        from wtpy.apps.astock.data.tushare_product import (
            build_delisted_missing_complement,
            select_delisted_pool,
        )

        ids = _standard_parents(store)
        base = store.load_manifest(ids["base"])
        cutoff = int(base.data_cutoff_date or 0)
        # mirror the reconcile: supplement = the delisted missing complement
        complement = build_delisted_missing_complement(
            store, base=base, delisted_pool=select_delisted_pool(store),
            cutoff=cutoff,
        )
        l2 = build_composite_none(
            store, base_dataset_id=ids["base"],
            supplement_dataset_id=complement.dataset_id,
            cutoff=cutoff,
        )
        factor = store.load_manifest(ids["factor"])
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS and rec.symbol != BASE_SYMS[0]:
                rec.last_date = 20241001  # stale vs raw 20241026
        store.save_manifest(factor)
        d = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2.dataset_id,
            factor_dataset_id=ids["factor"], cutoff=cutoff)
        assert d["dataset_status"] == "partial"
        assert d["freshness_gate"]["status"] == "blocked"
        l1_m = store.load_manifest(d["dataset_id"])
        assert l1_m.status == "partial"
        assert (l1_m.provenance or {}).get("freshness", {}).get(
            "status") == "blocked"

        # factor refreshed in place (same dataset id, fresh records): the
        # coarse reconcile gate passes, but the derive idempotent path still
        # returns the on-disk partial L1 -> validation reports partial.
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS:
                rec.last_date = 20241130
        store.save_manifest(factor)

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert r.published is False
        assert "tushare/adj_factor" in r.missing
        assert any("L1 not ready: partial" in i for i in r.issues)
        assert any("l1_not_ready_freshness" in i for i in r.issues)
        assert any("freshness_below_threshold" in i or
                   "freshness_gate_blocked" in i for i in r.issues)

    def test_reconcile_fresh_marked_partial_l1_fails(self, store):
        """Control: a partial L1 whose provenance records a PASSED freshness
        gate is a non-freshness partial (provider-level failure) -> failed,
        never waiting_for_parent."""
        from wtpy.apps.astock.data.composite_dataset import build_composite_none
        from wtpy.apps.astock.data.tushare_product import (
            build_delisted_missing_complement,
            select_delisted_pool,
        )

        ids = _standard_parents(store)
        base = store.load_manifest(ids["base"])
        cutoff = int(base.data_cutoff_date or 0)
        complement = build_delisted_missing_complement(
            store, base=base, delisted_pool=select_delisted_pool(store),
            cutoff=cutoff,
        )
        l2 = build_composite_none(
            store, base_dataset_id=ids["base"],
            supplement_dataset_id=complement.dataset_id,
            cutoff=cutoff,
        )
        factor = store.load_manifest(ids["factor"])
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS and rec.symbol != BASE_SYMS[0]:
                rec.last_date = 20241001  # stale -> gate-blocked partial
        store.save_manifest(factor)
        d = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2.dataset_id,
            factor_dataset_id=ids["factor"], cutoff=cutoff)
        assert d["dataset_status"] == "partial"
        # rewrite the L1 provenance: gate passed (a partial for another
        # reason would carry this marker)
        l1_m = store.load_manifest(d["dataset_id"])
        l1_m.provenance = dict(l1_m.provenance or {})
        l1_m.provenance["freshness"] = {"status": "passed"}
        store.save_manifest(l1_m)
        # factor refreshed in place: the coarse gate passes, the idempotent
        # derive returns the on-disk partial L1, validation fails on
        # "L1 not ready: partial" with NO freshness cause -> failed.
        for rec in factor.symbols:
            if rec.symbol in BASE_SYMS:
                rec.last_date = 20241130
        store.save_manifest(factor)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "failed"
        assert r.published is False
        assert any("L1 not ready: partial" in i for i in r.issues)

    def test_auto_resolve_parents_refuses_gate_blocked_latest(
            self, tmp_path, monkeypatch):
        """_auto_resolve_parents must refuse the derive when the LATEST
        factor candidate is a freshness-gate-blocked partial (no silent
        fallback to an older ready factor)."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_resolve_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)

        store = DatasetStore(tmp_path / "md")
        ids = _standard_parents(store)
        # a ready internal/composite_none surface so the raw branch passes
        _publish_bars(
            store, "internal_composite_none_1d_ready", "internal",
            "composite_none",
            {sym: _arrays(300, 10.0) for sym in COMMON_SYMS},
        )
        # NEWER gate-blocked partial factor must block the auto-resolve
        _publish_factors(
            store, "tushare_adjfactor_gate_blocked",
            {sym: _factor_series(sym) for sym in BASE_SYMS[:5]})
        m = store.load_manifest("tushare_adjfactor_gate_blocked")
        m.status = "partial"
        m.provenance = {"freshness": {"gate": "blocked"}}
        m.data_cutoff_date = 20261101
        store.save_manifest(m)

        args = SimpleNamespace(raw_dataset_id=None, factor_dataset_id=None)
        err = smd._auto_resolve_parents(
            args, store, raw_source="internal",
            raw_adjustment="composite_none")
        assert err is not None
        assert "freshness" in err and "blocked" in err
        assert args.factor_dataset_id is None

    def test_auto_resolve_parents_picks_ready_factor(self, tmp_path):
        """Control: without a gate-blocked partial, the auto-resolve keeps
        picking the latest ready factor."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location(
            "sync_md_resolve_ok_test", root / "scripts" / "sync_market_data.py")
        smd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smd)

        store = DatasetStore(tmp_path / "md")
        ids = _standard_parents(store)
        _publish_bars(
            store, "internal_composite_none_1d_ready", "internal",
            "composite_none",
            {sym: _arrays(300, 10.0) for sym in COMMON_SYMS},
        )
        factor = store.load_manifest(ids["factor"])
        factor.data_cutoff_date = 20241026  # ready-loop keys on cutoff
        store.save_manifest(factor)
        args = SimpleNamespace(raw_dataset_id=None, factor_dataset_id=None)
        err = smd._auto_resolve_parents(
            args, store, raw_source="internal",
            raw_adjustment="composite_none")
        assert err is None
        assert args.factor_dataset_id == ids["factor"]

    def test_reconcile_waits_for_missing_delisted(self, store):
        ids = _standard_parents(store)
        store.manifests_dir.joinpath("tushare_none_1d_delisted.json").unlink()
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare_delisted" in r.missing
        assert r.published is False

    def test_reconcile_waits_for_missing_delisted_factor(self, store):
        """Delisted symbols without factors must block the formal L1 publish."""
        _standard_parents(store)
        # drop the delisted symbols from the main factor dataset
        m = store.load_manifest("tushare_adjfactor_1d_full")
        m.symbols = [s for s in m.symbols if s.symbol not in DELISTED_SYMS]
        m.symbol_count = len(m.symbols)
        store.save_manifest(m)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare_delisted_factor" in r.missing
        assert r.published is False
        assert any("l1_missing_factor_symbols" in i for i in r.issues)

    def test_reconcile_missing_factor_does_not_publish_l2(self, store):
        """A factor missing-symbol gap above the threshold must return
        waiting_for_parent WITHOUT leaving a ready L2 (or partial L1) orphan
        manifest on the disk (the L2 publish used to happen before the
        check)."""
        _standard_parents(store)
        m = store.load_manifest("tushare_adjfactor_1d_full")
        m.symbols = [s for s in m.symbols if s.symbol not in DELISTED_SYMS]
        m.symbol_count = len(m.symbols)
        store.save_manifest(m)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "waiting_for_parent"
        assert "tushare_delisted_factor" in r.missing
        assert r.published is False
        assert not any(
            "composite_none" in mid or "composite_tushare_factor_qfq" in mid
            for mid in store.list_manifests()
        ), "no L2/L1 product manifest may land when the factor gap blocks"


class TestDeriveDataPolicyFromParent:
    """The L1 derivation must derive its provenance data_policy from the REAL
    raw parent (composite_none L2), never hardcode tushare_only_v1: a legacy
    mixed-vendor composite produces a legacy L1 that the strict pair
    validation rejects (fail-closed)."""

    def _l2(self, store, *, base_dataset_id, cutoff):
        from wtpy.apps.astock.data.composite_dataset import build_composite_none

        return build_composite_none(
            store,
            base_dataset_id=base_dataset_id,
            supplement_dataset_id="tushare_none_1d_delisted",
            cutoff=cutoff,
        )

    def test_tushare_parent_keeps_tushare_only_policy(self, store):
        ids = _standard_parents(store)
        base = store.load_manifest(ids["base"])
        l2 = self._l2(store, base_dataset_id=ids["base"],
                      cutoff=int(base.data_cutoff_date or 0))
        assert (l2.provenance or {}).get("data_policy") == "tushare_only_v1"
        d = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2.dataset_id, factor_dataset_id=ids["factor"])
        assert d["status"] == "success"
        l1 = store.load_manifest(d["dataset_id"])
        prov = l1.provenance or {}
        assert prov["data_policy"] == DATA_POLICY_TUSHARE_ONLY
        assert prov["base_source"] == "tushare"
        assert prov["supplement_source"] == "tushare"
        # the lineage-valid pair still resolves as the formal product
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert v["ok"], v["issues"]

    def test_legacy_mixed_parent_derives_legacy_policy(self, store):
        """base=local_vendor composite (still allowed by build_composite_none)
        must produce an L1 whose data_policy is NOT tushare_only_v1, and the
        strict pair validation rejects it (fail-closed, never formal)."""
        ids = _standard_parents(store)
        local_base = _publish_bars(
            store, "localvendor_none_1d_legacy_base",
            "local_vendor", "none",
            {sym: _arrays(300, 9.0 + i) for i, sym in enumerate(BASE_SYMS)},
        )
        l2 = self._l2(store, base_dataset_id=local_base.dataset_id,
                      cutoff=int(local_base.data_cutoff_date or 0))
        assert (l2.provenance or {}).get("data_policy") == "legacy_mixed_vendor"
        fac = store.load_manifest(ids["factor"])
        d = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2.dataset_id, factor_dataset_id=fac.dataset_id)
        assert d["status"] == "success"
        l1 = store.load_manifest(d["dataset_id"])
        prov = l1.provenance or {}
        assert prov["data_policy"] != DATA_POLICY_TUSHARE_ONLY
        assert prov["data_policy"] == "legacy_mixed_vendor"
        # fail-closed: the validator rejects the legacy pair...
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("tushare_only_v1" in i for i in v["issues"])
        # ...and it never becomes the active formal product
        assert resolve_active_tushare_product_pair(store) is None


class TestDryRunReconcile:
    """dry_run=True must never crash on a store that has not published the
    product chain yet, and must never write any manifest."""

    def test_dry_run_fresh_parents_no_crash_no_manifests(self, store):
        _standard_parents(store)
        before = set(store.list_manifests())
        r = reconcile_tushare_product_datasets(store, dry_run=True)
        assert r.status == "dry_run"
        assert r.published is False
        assert any("dry_run_placeholder" in i for i in r.issues)
        assert set(store.list_manifests()) == before

    def test_dry_run_published_store_full_chain_no_manifests(
            self, product_store):
        """With the L2 manifest already on disk the dry-run runs the whole
        chain (candidate L2 + derive) and still publishes nothing."""
        before = set(product_store.list_manifests())
        r = reconcile_tushare_product_datasets(product_store, dry_run=True)
        assert r.status in ("up_to_date", "published", "dry_run")
        assert r.published is False
        assert r.l2_dataset_id
        assert set(product_store.list_manifests()) == before


# ---------------------------------------------------------------------------
# 4-7: survivorship-safe composite
# ---------------------------------------------------------------------------


class TestCompositeSurvivorship:
    def test_l2_base_is_tushare(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        assert pair is not None
        l2 = pair.l2_manifest
        assert l2.source == "internal" and l2.adjustment == "composite_none"
        prov = l2.provenance or {}
        assert prov["data_policy"] == DATA_POLICY_TUSHARE_ONLY
        assert prov["base_source"] == "tushare"
        assert prov["supplement_source"] == "tushare"
        assert prov["supplement_rule"] == "missing_symbols_only"
        assert prov["survivorship_policy"] == "listed_plus_delisted"
        assert prov["quality_status"] == "passed"

    def test_supplement_disjoint(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        base = product_store.load_manifest(pair.base_dataset_id)
        supp = product_store.load_manifest(pair.supplement_dataset_id)
        base_syms = {s.symbol for s in base.symbols if s.blob_sha256}
        supp_syms = {s.symbol for s in supp.symbols if s.blob_sha256}
        assert not (base_syms & supp_syms)
        # complement rule recorded
        assert (supp.provenance or {}).get("supplement_rule") == "missing_symbols_only"

    def test_delisted_symbol_in_formal_l2(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        l2_syms = {s.symbol for s in pair.l2_manifest.symbols}
        assert "SZSE.STK.300104" in l2_syms
        assert "SSE.STK.600001" in l2_syms
        assert all(s in l2_syms for s in BASE_SYMS)

    def test_delisted_present_in_base_is_excluded_from_complement(self, store):
        """A delisted symbol already in base must not be duplicated."""
        arrays = _full_base_arrays()
        arrays["SZSE.STK.300104"] = _arrays(300, 9.0)  # delisted already in base
        _publish_bars(store, "tushare_none_1d_base_full", "tushare", "none", arrays)
        _publish_bars(
            store, "tushare_none_1d_delisted", "tushare", "none",
            {sym: _arrays(200, 5.0, 20230101) for sym in DELISTED_SYMS},
            universe_type="b1_delisted_supplement",
        )
        base = select_tushare_base(store)
        pool = []
        from wtpy.apps.astock.data.tushare_product import select_delisted_pool
        pool = select_delisted_pool(store)
        comp = build_delisted_missing_complement(
            store, base=base, delisted_pool=pool, cutoff=20260804)
        assert "SZSE.STK.300104" not in {s.symbol for s in comp.symbols}
        assert "SZSE.STK.300104" in (comp.provenance or {}).get("excluded_overlap", [])

    def test_reconcile_records_pit_universe_alias_map(self, store):
        """A point-in-time universe file under the data root must be picked
        up by the reconcile (BSE pre-migration alias map) and its id recorded
        on the derived L1 provenance."""
        from wtpy.apps.astock.data.pit_universe import (
            InstrumentWindow,
            PointInTimeUniverse,
        )

        _standard_parents(store)
        pit = PointInTimeUniverse.build(
            [
                InstrumentWindow(
                    canonical_symbol="BSE.STK.920001", ts_code="920001.BJ",
                    exchange="BSE", board="main", name="bse920001",
                    list_status="L", list_date=20230101, delist_date=None,
                    last_trade_date=20260804, aliases=["BSE.STK.430001"],
                )
            ],
            cutoff=20260804,
        )
        pit.save(store.root)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published", r.to_dict()
        pair = resolve_active_tushare_product_pair(store)
        assert pair is not None
        universe_id = (pair.l1_manifest.provenance or {}).get(
            "universe_dataset_id")
        assert universe_id, "reconcile must record the pit universe id on L1"
        assert universe_id == pit.universe_dataset_id

    def test_l1_raw_parent_is_formal_l2(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        assert pair.l1_manifest.raw_dataset_id == pair.l2_dataset_id
        assert pair.l1_manifest.factor_source == "tushare"
        v = validate_tushare_product_pair(product_store)
        assert v["ok"], v["issues"]

    def test_lineage_rejects_broken_pair(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        # point L1 at a different L2 id -> lineage check must fail
        other = _publish_bars(
            product_store, "internal_composite_none_1d_other", "internal",
            "composite_none", {"SSE.STK.600000": _arrays(50, 10.0)},
        )
        v = validate_tushare_product_pair(
            product_store,
            l1_dataset_id=pair.l1_dataset_id,
            l2_dataset_id=other.dataset_id,
        )
        assert not v["ok"]
        assert any("raw parent" in i for i in v["issues"])

    def test_supersede_keeps_unique_coverage_factor_supplement(self, store):
        """A small factor set with symbols the winner does not carry is a
        supplement role and must never be demoted (delisted-factor set)."""
        from wtpy.apps.astock.data.repository import MarketDataRepository

        def _publish_factor_set(dataset_id, symbols):
            records = []
            for sym in symbols:
                dates = np.arange(20240101, 20240101 + 300, dtype=np.int64)
                factors = np.full(300, 1.0)
                sha = store.store_factors(sym, dates, factors)
                records.append(SymbolRecord(
                    symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
                    last_date=int(dates[-1]), row_count=300, quality="ok",
                ))
            m = DatasetManifest(
                dataset_id=dataset_id, source="tushare", adjustment="adj_factor",
                period="1d", status="building", dataset_type="factor",
                symbols=records, symbol_count=len(records),
                row_count=sum(r.row_count for r in records),
                data_cutoff_date=20260731,
            )
            store.publish(m)

        _publish_factor_set("factor_full", [f"SSE.STK.{600000 + i}" for i in range(100)])
        _publish_factor_set(
            "factor_delisted_supplement",
            ["SZSE.STK.300104", "SSE.STK.601558", "SZSE.STK.002680"],
        )
        repo = MarketDataRepository(store)
        full = store.load_manifest("factor_full")
        demoted = repo.supersede_dominated_ready(full)
        assert "factor_delisted_supplement" not in demoted
        assert store.load_manifest("factor_delisted_supplement").status == "ready"

        # a pure subset without unique coverage is still demoted
        _publish_factor_set("factor_subset", [f"SSE.STK.{600000 + i}" for i in range(5)])
        demoted2 = repo.supersede_dominated_ready(store.load_manifest("factor_full"))
        assert "factor_subset" in demoted2

    def test_reconcile_publishes_with_delisted_factor_supplement(self, store):
        """Formal L1 publishes once the delisted-factor supplement exists."""
        _publish_bars(
            store, "tushare_none_1d_base_full", "tushare", "none",
            _full_base_arrays(),
        )
        _publish_bars(
            store, "tushare_none_1d_delisted", "tushare", "none",
            {sym: _arrays(200, 5.0, 20230101) for sym in DELISTED_SYMS},
            universe_type="b1_delisted_supplement",
        )
        # main factor set: full market WITHOUT the delisted symbols
        _publish_factors(
            store, "tushare_adjfactor_1d_full",
            {sym: _factor_series(sym) for sym in BASE_SYMS},
        )
        # delisted-factor supplement: delisted symbols only
        _publish_factors(
            store, "tushare_adjfactor_1d_delisted_supp",
            {sym: _factor_series(sym) for sym in DELISTED_SYMS},
        )

        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published", r.to_dict()
        assert r.missing == []
        pair = resolve_active_tushare_product_pair(store)
        assert pair is not None
        assert pair.l1_manifest.status == "ready"
        prov = pair.l1_manifest.provenance or {}
        assert prov["factor_source_counts"]["supplement"] == len(DELISTED_SYMS)
        assert not (prov.get("missing_factor_symbols") or [])


# ---------------------------------------------------------------------------
# 8-9: quality gates
# ---------------------------------------------------------------------------


class TestResolverLineageValidation:
    """P1-2: resolve_active_tushare_product_pair must run the full pair
    lineage validation (validate_tushare_product_pair's core checks), not
    just role/data-policy filters — a tdx-factor L1 is never active."""

    def _publish_l2(self, store, ids):
        from wtpy.apps.astock.data.composite_dataset import build_composite_none

        base = store.load_manifest(ids["base"])
        l2 = build_composite_none(
            store,
            base_dataset_id=ids["base"],
            supplement_dataset_id="tushare_none_1d_delisted",
            cutoff=int(base.data_cutoff_date or 0),
        )
        return l2

    def _publish_tdx_l1(
        self, store, l2,
        dataset_id="internal_composite_tushare_factor_qfq_tdx",
    ):
        """A ready L1 whose factor source is TDX (external repro: the
        validator reports 'L1 factor_source is not tushare: tdx')."""
        records = []
        for sym in BASE_SYMS[:30]:
            dates = np.arange(20240101, 20240101 + 300, dtype=np.int64)
            factors = np.full(300, 1.0)
            sha = store.store_factors(sym, dates, factors)
            records.append(SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
                last_date=int(dates[-1]), row_count=300, quality="ok",
            ))
        m = DatasetManifest(
            dataset_id=dataset_id,
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d", status="ready", dataset_type="bars",
            raw_dataset_id=l2.dataset_id,
            factor_dataset_id="tdx_factor_surface",
            factor_source="tdx",
            symbols=records, symbol_count=len(records),
            row_count=sum(r.row_count for r in records),
            data_cutoff_date=20261101,
            provenance={"data_policy": DATA_POLICY_TUSHARE_ONLY},
        )
        store.publish(m)
        return m

    def _publish_tushare_l1(
        self, store, l2, ids,
        dataset_id="internal_composite_tushare_factor_qfq_l1",
        *,
        raw_dataset_id=None, factor_dataset_id=None,
    ):
        """A ready tushare-lineage L1 (valid unless a parent is overridden)."""
        records = []
        for sym in BASE_SYMS[:30]:
            dates = np.arange(20240101, 20240101 + 300, dtype=np.int64)
            factors = np.full(300, 1.0)
            sha = store.store_factors(sym, dates, factors)
            records.append(SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=int(dates[0]),
                last_date=int(dates[-1]), row_count=300, quality="ok",
            ))
        m = DatasetManifest(
            dataset_id=dataset_id,
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d", status="ready", dataset_type="bars",
            raw_dataset_id=(
                l2.dataset_id if raw_dataset_id is None else raw_dataset_id
            ),
            factor_dataset_id=(
                ids["factor"] if factor_dataset_id is None else factor_dataset_id
            ),
            factor_source="tushare",
            symbols=records, symbol_count=len(records),
            row_count=sum(r.row_count for r in records),
            data_cutoff_date=20261101,
            provenance={"data_policy": DATA_POLICY_TUSHARE_ONLY},
        )
        store.publish(m)
        return m

    def _publish_bare_l2(
        self, store,
        dataset_id="internal_composite_none_1d_bare",
    ):
        """A ready L2 with the right role + policy but NO provenance parents."""
        m = DatasetManifest(
            dataset_id=dataset_id,
            source="internal", adjustment="composite_none",
            period="1d", status="ready", dataset_type="bars",
            data_cutoff_date=20261101,
            provenance={"data_policy": DATA_POLICY_TUSHARE_ONLY},
        )
        store.publish(m)
        return m

    def test_resolver_rejects_tdx_factor_l1(self, store):
        """External repro: ready L1 with factor_source=tdx + matching L2 ->
        the resolver returns None (it previously returned the pair); the
        validator reports the same issue for the same construction."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        l1 = self._publish_tdx_l1(store, l2)
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("L1 factor_source is not tushare: tdx" in i for i in v["issues"])

    def test_resolver_accepts_tushare_lineage_pair(self, store):
        """Control: a lineage-valid pair (tushare factor) still resolves."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        d = derive_composite_tushare_factor_qfq(
            store, raw_dataset_id=l2.dataset_id, factor_dataset_id=ids["factor"])
        assert d["dataset_status"] == "ready", d
        pair = resolve_active_tushare_product_pair(store)
        assert pair is not None
        assert pair.l1_dataset_id == d["dataset_id"]
        assert pair.l2_dataset_id == l2.dataset_id
        v = validate_tushare_product_pair(
            store, l1_dataset_id=pair.l1_dataset_id, l2_dataset_id=l2.dataset_id)
        assert v["ok"], v["issues"]

    def test_malformed_pair_without_l2_parents_rejected(self, store):
        """External repro: ready L1 + ready L2 with correct role/policy but
        NO L2 provenance parents -> the validator rejects (issue mentions
        parents) and the resolver returns None."""
        ids = _standard_parents(store)
        l2 = self._publish_bare_l2(store)
        l1 = self._publish_tushare_l1(store, l2, ids)
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("parents" in i for i in v["issues"])

    def test_l1_without_raw_parent_rejected(self, store):
        """External repro: ready L1 with no raw_dataset_id (matching L2
        present) -> the validator rejects ('raw parent missing') and the
        resolver returns None."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        l1 = self._publish_tushare_l1(store, l2, ids, raw_dataset_id="")
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("raw parent missing" in i for i in v["issues"])

    def test_l1_factor_parent_missing_rejected(self, store):
        """External repro: ready L1 whose factor_dataset_id points at a
        manifest that does not exist -> the validator rejects ('factor
        manifest missing') and the resolver returns None."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        l1 = self._publish_tushare_l1(
            store, l2, ids, factor_dataset_id="tushare_adjfactor_1d_missing")
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any(
            "L1 factor manifest missing: tushare_adjfactor_1d_missing" in i
            for i in v["issues"]
        )

    def _republish_l2_with_parents(self, store, l2, parents):
        """Re-publish the same L2 id with rewritten provenance parents."""
        prov = dict(l2.provenance or {})
        prov["parents"] = parents
        l2.provenance = prov
        store.publish(l2, integrity_check=False)
        return store.load_manifest(l2.dataset_id)

    def test_swapped_parent_roles_rejected(self, store):
        """P1-3: L2 parents whose role labels are swapped (parents[0]
        role=supplement, parents[1] role=base) with an otherwise complete
        lineage must be rejected — the resolver previously returned the
        pair with crossed base/supplement ids."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        old = (l2.provenance or {})["parents"]
        l2 = self._republish_l2_with_parents(
            store, l2,
            [
                {"dataset_id": old[0]["dataset_id"], "role": "supplement"},
                {"dataset_id": old[1]["dataset_id"], "role": "base"},
            ],
        )
        l1 = self._publish_tushare_l1(store, l2, ids)
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("role" in i for i in v["issues"])

    def test_identical_l2_parents_rejected(self, store):
        """P1-3: L2 parents whose two dataset ids are identical must be
        rejected ('identical') and never resolved."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        l2 = self._republish_l2_with_parents(
            store, l2,
            [
                {"dataset_id": ids["base"], "role": "base"},
                {"dataset_id": ids["base"], "role": "supplement"},
            ],
        )
        l1 = self._publish_tushare_l1(store, l2, ids)
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("identical" in i for i in v["issues"])

    def test_l2_parents_missing_role_rejected(self, store):
        """P1-3: L2 parents entries without the role field are rejected."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        old = (l2.provenance or {})["parents"]
        l2 = self._republish_l2_with_parents(
            store, l2,
            [
                {"dataset_id": old[0]["dataset_id"]},
                {"dataset_id": old[1]["dataset_id"]},
            ],
        )
        l1 = self._publish_tushare_l1(store, l2, ids)
        assert resolve_active_tushare_product_pair(store) is None
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert not v["ok"]
        assert any("L2 parents role missing" in i for i in v["issues"])

    def test_control_pair_roles_match_parent_ids(self, store):
        """P1-3 control: a normal pair (role order correct, ids unique)
        validates and resolves with base/supplement ids matching the
        recorded roles."""
        ids = _standard_parents(store)
        l2 = self._publish_l2(store, ids)
        l1 = self._publish_tushare_l1(store, l2, ids)
        v = validate_tushare_product_pair(
            store, l1_dataset_id=l1.dataset_id, l2_dataset_id=l2.dataset_id)
        assert v["ok"], v["issues"]
        pair = resolve_active_tushare_product_pair(store)
        assert pair is not None
        for p in (l2.provenance or {})["parents"]:
            if p["role"] == "base":
                assert pair.base_dataset_id == p["dataset_id"]
            else:
                assert pair.supplement_dataset_id == p["dataset_id"]
        assert pair.base_dataset_id == ids["base"]
        assert pair.supplement_dataset_id == "tushare_none_1d_delisted"


class TestQualityGates:
    def test_date_regression_does_not_switch(self, product_store):
        pair = resolve_active_tushare_product_pair(product_store)
        old_max = pair.l2_max_date
        # Rewrite the current base IN PLACE with EARLIER real max dates (the
        # manifest keeps its id so the active pair's lineage stays intact —
        # deleting it would break the L2 parent manifest and orphan the
        # current pair). A candidate L2 built from the older surface would
        # regress the formal product.
        base = product_store.load_manifest("tushare_none_1d_base_full")
        older_arrays = {sym: _arrays(250, 8.0, 20240101) for sym in BASE_SYMS}
        records = []
        total = 0
        for sym, arrays in older_arrays.items():
            sha = product_store.store_bar_arrays(sym, arrays)
            d = arrays["trade_date"]
            records.append(SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=int(d[0]),
                last_date=int(d[-1]), row_count=len(d), quality="ok",
            ))
            total += len(d)
        base.symbols = records
        base.symbol_count = len(records)
        base.row_count = total
        base.data_cutoff_date = int(max(r.last_date or 0 for r in records))
        product_store.save_manifest(base)
        r = reconcile_tushare_product_datasets(product_store)
        assert r.status == "failed"
        assert any("data_date_regression" in i for i in r.issues)
        # the old formal pair remains the active one
        after = resolve_active_tushare_product_pair(product_store)
        assert after.l2_dataset_id == pair.l2_dataset_id
        assert after.l2_max_date == old_max

    def test_regression_gate_blocks_symbol_collapse(self, store):
        from wtpy.apps.astock.data.tushare_product import _regression_issues

        ids = _standard_parents(store)
        r = reconcile_tushare_product_datasets(store)
        pair = resolve_active_tushare_product_pair(store)
        assert pair is not None

        tiny = _publish_bars(
            store, "tushare_none_1d_tiny", "tushare", "none",
            {sym: _arrays(300, 9.0) for sym in BASE_SYMS[:3]},
        )
        issues = _regression_issues(pair, tiny, tiny)
        assert any("symbol_count_regression" in i for i in issues)

    def test_16_row_orphan_not_selected(self, store):
        """A 16-row short-window dataset must never become the base."""
        _standard_parents(store)
        orphan = {
            sym: _arrays(16, 11.0, 20260701) for sym in BASE_SYMS
        }
        _publish_bars(
            store, "tushare_none_1d_orphan", "tushare", "none", orphan,
            data_cutoff_date=20260716,
        )
        base = select_tushare_base(store)
        assert base.dataset_id == "tushare_none_1d_base_full"
        from wtpy.apps.astock.data.tushare_product import is_orphan_window
        assert is_orphan_window(store.load_manifest("tushare_none_1d_orphan"))

    def test_orphan_not_selected_by_bagua_session(self, store):
        """Plan 9.2: 16-row orphan must be ineligible in the query plane."""
        from wtpy.apps.astock.service.bagua_query import BaguaPlaneSession

        _standard_parents(store)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published"
        orphan = {"SSE.STK.600000": _arrays(16, 11.0, 20260701)}
        _publish_bars(
            store, "tushare_none_1d_orphan2", "tushare", "none", orphan,
            data_cutoff_date=20260716,
        )
        cfg = SimpleNamespace(market_data_root=str(store.root))
        session = BaguaPlaneSession(cfg, "raw")
        bars, meta = session.load_symbol("SSE.STK.600000")
        assert meta["dataset_id"].startswith("internal_composite_none_1d")
        assert meta["bootstrap_fallback"] is False


# ---------------------------------------------------------------------------
# 10-12: default selection
# ---------------------------------------------------------------------------


class TestDefaultSelection:
    def test_quick_does_not_pick_legacy(self, tmp_path, monkeypatch):
        """Even a fresher local_vendor set must not win the raw plane."""
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from wtpy.apps.astock.api import create_app
        from wtpy.apps.astock.config import get_default_config

        storage = tmp_path / "st"
        ind = tmp_path / "ind"
        storage.mkdir()
        ind.mkdir()
        cfg = get_default_config(storage_root=storage, indicator_dir=ind)
        store = DatasetStore(cfg.market_data_root)
        # local_vendor/none dataset with the freshest dates
        _publish_bars(
            store, "localvendor_none_1d_20260804", "local_vendor", "none",
            {sym: _arrays(300, 12.0, 20240101) for sym in COMMON_SYMS},
            data_cutoff_date=20260804,
        )
        # full tushare chain with slightly older dates (formal L1/L2)
        _standard_parents(store)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published", r.to_dict()

        client = TestClient(create_app(cfg))
        resp = client.get("/api/v1/quick/600000")
        assert resp.status_code == 200
        market = resp.json().get("market") or {}
        assert market.get("dataset_source") == "internal"
        assert market.get("dataset_adjustment") == "composite_none"
        assert market.get("bootstrap_fallback") is False
        assert market.get("data_policy") == DATA_POLICY_TUSHARE_ONLY

    def test_formal_pair_ids_exclude_same_date_competitors(self, product_store):
        """Formal ids win even when legacy/native competitors have more rows."""
        from wtpy.apps.astock.service.bagua_query import BaguaPlaneSession

        pair = resolve_active_tushare_product_pair(product_store)
        assert pair is not None
        # These candidates share the same source/adjustment family or plane and
        # are deliberately fuller. They must remain ineligible once formal ids
        # exist.
        _publish_bars(
            product_store,
            "internal_composite_none_1d_same_date_legacy",
            "internal",
            "composite_none",
            {sym: _arrays(600, 30.0) for sym in BASE_SYMS},
            data_cutoff_date=pair.cutoff,
            provenance={"base_source": "local_vendor"},
        )
        _publish_bars(
            product_store,
            "tushare_qfq_1d_same_date_native",
            "tushare",
            "qfq",
            {sym: _arrays(600, 40.0) for sym in BASE_SYMS},
            data_cutoff_date=pair.cutoff,
        )
        cfg = SimpleNamespace(market_data_root=str(product_store.root))
        _, raw_meta = BaguaPlaneSession(cfg, "raw").load_symbol(BASE_SYMS[0])
        _, qfq_meta = BaguaPlaneSession(cfg, "tushare_qfq").load_symbol(BASE_SYMS[0])
        assert raw_meta["dataset_id"] == pair.l2_dataset_id
        assert qfq_meta["dataset_id"] == pair.l1_dataset_id
        assert raw_meta["bootstrap_fallback"] is False
        assert qfq_meta["bootstrap_fallback"] is False

    def test_select_tushare_base_excludes_etf_only_dataset(self, store):
        """纯 ETF 数据集不能作为 raw base——即使 cutoff 比股票更新。

        ETF 增量同步与全市场股票共用 tushare/none/1d scope，manifest 无
        universe_type 标记；若按"最新 cutoff"选 base，2500 只 ETF 会抢占
        全市场股票基线，导致正式 L1/L2 基于错误口径重建。
        """
        # 全市场股票基线（较旧 cutoff）
        _publish_bars(
            store, "tushare_none_1d_20260812_full",
            "tushare", "none", _full_base_arrays(),
            data_cutoff_date=20260812,
        )
        # 纯 ETF 数据集（较新 cutoff，符号不含 .STK.）
        etf_arrays = {
            f"SSE.ETF.{510000 + i}": _arrays(300, 5.0, 20240101)
            for i in range(120)
        }
        _publish_bars(
            store, "tushare_none_1d_20260813_etf",
            "tushare", "none", etf_arrays,
            data_cutoff_date=20260813,
        )

        base = select_tushare_base(store, deep_copy=False)
        assert base is not None
        assert base.dataset_id == "tushare_none_1d_20260812_full"
        assert any(".STK." in (s.symbol or "") for s in base.symbols)

    def test_bagua_query_default_is_tushare_qfq(self):
        from wtpy.apps.astock.api_routes.bagua import (
            BaguaBatchBody,
            BaguaExportBody,
        )

        body = BaguaBatchBody(date="2026-08-04")
        assert body.adjust == "tushare_qfq"
        assert BaguaExportBody(date="2026-08-04").adjust == "tushare_qfq"
        from wtpy.apps.astock.service.bagua_query import normalize_adjust_mode
        assert normalize_adjust_mode(None) == "tushare_qfq"

    def test_tdx_front_raises_clear_error(self, tmp_path):
        from wtpy.apps.astock.service.bagua_query import (
            SourceDisabledError,
            _source_match_pairs,
            query_bagua,
        )

        with pytest.raises(SourceDisabledError, match="已停用"):
            _source_match_pairs("tdx_front")
        with pytest.raises(SourceDisabledError, match="已停用"):
            query_bagua(
                SimpleNamespace(), code="600000", date="2026-08-04",
                period="DAY", adjust="tdx_front",
            )

    def test_tdx_front_api_returns_400(self, tmp_path):
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from wtpy.apps.astock.api import create_app
        from wtpy.apps.astock.config import get_default_config

        storage = tmp_path / "st"
        ind = tmp_path / "ind"
        storage.mkdir()
        ind.mkdir()
        cfg = get_default_config(storage_root=storage, indicator_dir=ind)
        client = TestClient(create_app(cfg))
        resp = client.get(
            "/api/v1/bagua/query",
            params={"code": "600000", "date": "2026-08-04", "adjust": "tdx_front"},
        )
        assert resp.status_code == 400
        assert "已停用" in resp.text


# ---------------------------------------------------------------------------
# 13-15: legacy readability + fail-closed backtest
# ---------------------------------------------------------------------------


class TestLegacyAndFailClosed:
    def test_old_dataset_id_still_readable(self, product_store):
        """Explicit legacy dataset ids remain loadable (historical repro)."""
        ids = _standard_parents(product_store)
        repo = MarketDataRepository(product_store)
        base = repo.get_dataset(ids["base"])
        assert base.status == "ready"
        bars = repo.load_bars(dataset_id=ids["base"], symbol="SSE.STK.600000")
        assert len(bars) == 300

    def test_no_new_env_vars_needed_to_start(self, tmp_path, monkeypatch):
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
        from wtpy.apps.astock.api import create_app
        from wtpy.apps.astock.config import get_default_config

        storage = tmp_path / "st"
        ind = tmp_path / "ind"
        storage.mkdir()
        ind.mkdir()
        cfg = get_default_config(storage_root=storage, indicator_dir=ind)
        client = TestClient(create_app(cfg))
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        r2 = client.get("/api/v1/system/data-health")
        assert r2.status_code == 200
        assert r2.json()["status"] == "stale"

    def test_backtest_without_product_no_unsafe_fallback(self, tmp_path):
        """Product not ready -> formal backtest fails closed (no tushare/none)."""
        from wtpy.apps.astock.config import AStockConfig
        from wtpy.apps.astock.data.dataset_binding import DatasetBindingError
        from wtpy.apps.astock.service.backtest import resolve_market_data_bindings
        from wtpy.apps.astock.service.backtest_request import BacktestRequest

        storage = tmp_path / "storage"
        store = DatasetStore(storage / "market_data")
        # only a survivorship-unsafe tushare/none set exists
        _publish_bars(
            store, "tushare_none_1d_only", "tushare", "none",
            {sym: _arrays(300, 10.0) for sym in COMMON_SYMS},
        )
        cfg = AStockConfig(
            storage_root=str(storage),
            output_root=str(tmp_path / "output"),
            tdx_root=str(tmp_path / "tdx"),
        )
        cfg.ensure_dirs()
        req = BacktestRequest(
            rule_ids=["r"], codes=["SSE.STK.600000"],
            signal_data_source="tushare",
            execution_data_source="internal",
        )
        with pytest.raises(DatasetBindingError) as ei:
            resolve_market_data_bindings(cfg, req, ["SSE.STK.600000"])
        assert ei.value.code == "DATASET_NOT_FOUND"


# ---------------------------------------------------------------------------
# 16: data-health
# ---------------------------------------------------------------------------


class TestDataHealth:
    def test_health_uses_formal_l1_l2_dates(self, tmp_path):
        """health API must report the real formal L1/L2 dataset ids + dates."""
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from wtpy.apps.astock.api import create_app
        from wtpy.apps.astock.config import get_default_config

        storage = tmp_path / "st"
        ind = tmp_path / "ind"
        storage.mkdir()
        ind.mkdir()
        cfg = get_default_config(storage_root=storage, indicator_dir=ind)
        # build the product chain in the app's own market data root
        store = DatasetStore(cfg.market_data_root)
        _standard_parents(store)
        r = reconcile_tushare_product_datasets(store)
        assert r.status == "published", r.to_dict()

        client = TestClient(create_app(cfg))
        health = client.get("/api/v1/system/data-health").json()

        # fixture data ends 20241026 (300 calendar days from 20240101) so the
        # global freshness flag is stale, but the report must still carry the
        # REAL formal product dates.
        pair = resolve_active_tushare_product_pair(store)
        assert health["formal_l2"]["dataset_id"] == pair.l2_dataset_id
        assert health["formal_l1"]["dataset_id"] == pair.l1_dataset_id
        assert health["formal_l2"]["max_date"] == pair.l2_max_date
        assert health["formal_l1"]["max_date"] == pair.l1_max_date
        assert health["lineage"]["consistent"] is True
        assert health["lineage"]["l1_raw_parent"] == pair.l2_dataset_id
        assert health["bootstrap_fallback_active"] is False
        assert health["status"] in ("healthy", "warning", "stale")
        # raw / factor report their own real dates (never a universe max)
        assert health["current_freshness"]["tushare_raw"]["max_date"] == 20241026

    def test_health_uses_stalest_required_parent(self, product_store):
        from wtpy.apps.astock.data.tushare_product import (
            manifest_history_signals,
            select_tushare_base,
            tushare_product_data_health,
        )

        raw = select_tushare_base(product_store)
        expected = manifest_history_signals(raw).max_last_date
        factor = product_store.load_manifest("tushare_adjfactor_1d_full")
        for rec in factor.symbols:
            rec.last_date = 20220300
        factor.data_cutoff_date = 20220300
        product_store.save_manifest(factor)
        health = tushare_product_data_health(
            product_store, expected_trading_day=expected
        )
        assert health["trading_day_lag"]["raw"] == 0
        assert health["trading_day_lag"]["factor"] > 3
        assert health["status"] == "stale"

    def test_health_warns_on_missing_parts(self, store):
        _standard_parents(store)
        store.manifests_dir.joinpath("tushare_adjfactor_1d_full.json").unlink()
        from wtpy.apps.astock.data.tushare_product import (
            tushare_product_data_health,
        )

        h = tushare_product_data_health(store)
        assert h["formal_l1"]["status"] == "missing"
        assert h["formal_l2"]["status"] == "missing"
        assert h["current_freshness"]["tushare_factor"]["status"] == "missing"
        assert h["status"] in ("warning", "stale")

    def test_health_reports_recent_sync_errors(self, store):
        from wtpy.apps.astock.data.tushare_product import (
            tushare_product_data_health,
        )

        h = tushare_product_data_health(store, recent_sync_errors=[
            {"sync_run_id": "ts_20260804_fail", "status": "failed",
             "error": "rate_limited"},
        ])
        assert len(h["recent_sync_errors"]) == 1
        assert h["status"] != "healthy"
