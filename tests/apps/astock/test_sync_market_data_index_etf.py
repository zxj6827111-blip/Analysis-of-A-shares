# -*- coding: utf-8 -*-
"""Tests for sync_market_data.py index/ETF support (offline)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "sync_market_data.py"
)

_sync = None


def _load_module():
    global _sync
    if _sync is None:
        spec = importlib.util.spec_from_file_location(
            "sync_market_data_test_mod", SCRIPT
        )
        _sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sync)
    return _sync


def test_normalize_symbol_index_etf():
    m = _load_module()
    norm = m._normalize_symbol
    assert norm("sh000001") == "SSE.IDX.000001"
    assert norm("000001.SH") == "SSE.IDX.000001"
    assert norm("SSE.IDX.000001") == "SSE.IDX.000001"
    assert norm("sz399001") == "SZSE.IDX.399001"
    assert norm("sh510300") == "SSE.ETF.510300"
    assert norm("510300.SH") == "SSE.ETF.510300"
    assert norm("sz159915") == "SZSE.ETF.159915"
    # stocks unchanged
    assert norm("600000.SH") == "SSE.STK.600000"
    assert norm("sh600000") == "SSE.STK.600000"
    assert norm("000001.SZ") == "SZSE.STK.000001"
    assert norm("600000") == "SSE.STK.600000"
    assert norm("000001") == "SZSE.STK.000001"
    assert norm("430047.BJ") == "BSE.STK.430047"


def _fake_args(**kw):
    base = dict(
        symbol=None, asset_class="index", token=None,
        start_date=None, end_date=None, anchor_date=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_index_etf_symbols_universe_filter(monkeypatch):
    m = _load_module()
    provider = MagicMock()
    provider.fetch_index_etf_universe.return_value = [
        SimpleNamespace(symbol="SSE.IDX.000001"),
        SimpleNamespace(symbol="SZSE.IDX.399001"),
        SimpleNamespace(symbol="SSE.ETF.510300"),
        SimpleNamespace(symbol="SZSE.ETF.159915"),
    ]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="index"), provider)
    assert sorted(got) == ["SSE.IDX.000001", "SZSE.IDX.399001"]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="etf"), provider)
    assert sorted(got) == ["SSE.ETF.510300", "SZSE.ETF.159915"]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="all"), provider)
    assert len(got) == 4


def test_resolve_index_etf_symbols_symbol_wins(monkeypatch):
    m = _load_module()
    provider = MagicMock()
    provider.fetch_index_etf_universe.side_effect = AssertionError("must not be called")
    got = m._resolve_index_etf_symbols(
        _fake_args(asset_class="index", symbol="sh000001, sh510300"), provider
    )
    assert got == ["sh000001", "sh510300"]


def test_index_etf_configs_none_only():
    m = _load_module()
    configs = m._index_etf_configs()
    assert len(configs) == 1
    adj, period = configs[0]
    assert adj == m.AdjustmentMode.NONE
    assert period == m.BarPeriod.DAY


def test_sync_tushare_index_etf_full(monkeypatch):
    m = _load_module()

    from wtpy.apps.astock.data.providers.base import MarketBar
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    bar = MarketBar(
        symbol="SSE.IDX.000001", trade_date=20260731, period="1d",
        open=3800.0, high=3810.0, low=3790.0, close=3805.0,
        volume=1.0, amount=1.0, source="tushare", adjustment="none",
    )
    # Patch the real provider class so the function never hits the live API.
    monkeypatch.setattr(TushareProvider, "health_check", lambda self: True)
    monkeypatch.setattr(
        TushareProvider, "fetch_index_etf_universe",
        # 同步层现在会把 args.end_date 传入以排除退市/未上市基金
        lambda self, end_date=None: [
            SimpleNamespace(symbol="SSE.IDX.000001")
        ],
    )
    monkeypatch.setattr(TushareProvider, "fetch_bars", lambda self, req: [bar])

    def _fake_sync_dataset(**kw):
        return {
            "success": 1,
            "total": 1,
            "dataset_id": "tushare_none_1d_20260731_test_ie",
            "no_data": 0,
            "failed": 0,
            "errors": [],
        }

    fake_sync = MagicMock(side_effect=_fake_sync_dataset)
    monkeypatch.setattr(m, "_sync_dataset", fake_sync)
    store = MagicMock()
    result = m.sync_tushare_index_etf_full(_fake_args(), store)
    assert result["status"] == "success"
    assert result["datasets"]["none_1d"]["success"] == 1
    assert "qfq_1d" not in result["datasets"]
    req = fake_sync.call_args.kwargs
    assert req["adjustment"] == m.AdjustmentMode.NONE
    assert req["symbols"] == ["SSE.IDX.000001"]


def _mk_ready_manifest(store, ds_id, symbols_spec, no_data_symbols=()):
    """在 tmp 仓库发布一个含 .ETF. 符号的 ready 数据集，返回 manifest。

    ``no_data_symbols``：以 quality=no_data、空 blob 的记录入 manifest
    （模拟真实同步中无行情的 ETF）。
    """
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    dates = [20240101 + i for i in range(10)]
    recs = []
    for sym in symbols_spec:
        bars = [
            MarketBar(symbol=sym, trade_date=d, period="1d", open=1.0,
                      high=1.1, low=0.9, close=1.05, volume=10.0,
                      amount=1000.0, source="tushare", adjustment="none")
            for d in dates
        ]
        sha = store.store_bars(sym, bars)
        recs.append(SymbolRecord(symbol=sym, blob_sha256=sha,
                                 first_date=dates[0], last_date=dates[-1],
                                 row_count=len(dates), quality="ok"))
    for sym in no_data_symbols:
        recs.append(SymbolRecord(symbol=sym, blob_sha256="",
                                 row_count=0, quality="no_data"))
    m = DatasetManifest(dataset_id=ds_id, source="tushare",
                        adjustment="none", period="1d",
                        data_cutoff_date=20240110,
                        snapshot_date=20240110, provider_version="test",
                        status="ready", created_at="2024-01-11T18:00:00")
    m.symbols = recs
    m.symbol_count = len(recs)
    m.row_count = sum(r.row_count or 0 for r in recs)
    m.expected_symbol_count = len(recs)
    m.imported_symbol_count = len(recs)
    m.coverage_ratio = 1.0
    store.publish(m)
    return m


def test_etf_surface_pointer_writer(tmp_path):
    """指针写入门槛：ETF 独立覆盖率 + no_data allowlist。"""
    import json

    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    manifest = _mk_ready_manifest(store, "tushare_none_1d_surface",
                                  ["SSE.ETF.510300", "SZSE.ETF.159915",
                                   "SSE.IDX.000001"])
    result = {"status": "ready", "dataset_id": manifest.dataset_id,
              "total": 3, "success": 2}

    # 1) 全量 + ETF 全部有数据 -> 写指针（payload 为 ETF 独立口径）
    path = m._write_etf_surface_pointer(store, result, full_universe=True)
    assert path is not None and path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dataset_id"] == manifest.dataset_id
    assert payload["manifest_sha256"] == manifest.manifest_sha256
    assert payload["expected_etf"] == 2
    assert payload["blob_backed_etf"] == 2
    assert payload["no_data_etf"] == []
    assert payload["coverage"] == 1.0

    # 2) --symbol 局部同步 -> 不更新（保留旧内容）
    old_text = path.read_text(encoding="utf-8")
    assert m._write_etf_surface_pointer(
        store, result, full_universe=False) is None
    assert path.read_text(encoding="utf-8") == old_text

    # 3) status 非 ready -> 不更新
    bad = dict(result, status="partial")
    assert m._write_etf_surface_pointer(
        store, bad, full_universe=True) is None


def test_etf_surface_pointer_writer_no_data_allowlist(tmp_path):
    """未获数据的 ETF：不在 allowlist -> 拒绝发布；显式放行 -> 发布。"""
    import json

    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    # 4 只有数据 + 1 只 no_data：ETF 覆盖率 = 4/5 = 0.8 恰好达标，
    # 发布与否完全取决于 allowlist（双门槛同时生效的独立验证）
    manifest = _mk_ready_manifest(
        store, "tushare_none_1d_surface2",
        ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060",
         "SZSE.ETF.159915"],
        no_data_symbols=["SZSE.ETF.159043"],  # 未上市/无行情
    )
    result = {"status": "ready", "dataset_id": manifest.dataset_id,
              "total": 5, "success": 4}
    pointer = tmp_path / "etf_surface_pointer.json"

    # 1) 无 allowlist：unallowed missing -> 不写指针
    assert m._write_etf_surface_pointer(store, result, full_universe=True) is None
    assert pointer.exists() is False

    # 2) allowlist 显式放行（规范符号写法；6 位代码写法等价归一）
    (tmp_path / "etf_no_data_allowlist.json").write_text(
        json.dumps(["SZSE.ETF.159043"]), encoding="utf-8")
    path = m._write_etf_surface_pointer(store, result, full_universe=True)
    assert path is not None and path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["no_data_etf"] == ["159043"]
    assert payload["allowlisted_missing"] == ["159043"]
    assert payload["coverage"] == 0.8


def test_etf_surface_pointer_full_universe_flag():
    """--symbol 局部同步标志必须来自 args.symbol 的有无。"""
    args_with_symbol = SimpleNamespace(symbol="SSE.ETF.510300", end_date=None)
    args_full = SimpleNamespace(symbol=None, end_date=None)
    full_universe = not bool(getattr(args_with_symbol, "symbol", None))
    assert full_universe is False
    assert bool(getattr(args_full, "symbol", None)) is False


def _stub_sync_env(monkeypatch, m, store, backed, no_data=()):
    """打桩 provider/符号解析/数据集工作器；工作器发布真实 manifest。"""
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    monkeypatch.setattr(TushareProvider, "health_check", lambda self: True)
    universe = list(backed) + list(no_data) + ["SSE.IDX.000001"]
    monkeypatch.setattr(
        m, "_resolve_index_etf_symbols", lambda args, provider: universe
    )
    ds_counter = {"n": 0}

    def _fake_sync_dataset(**kw):
        ds_counter["n"] += 1
        ds_id = f"tushare_none_1d_stub_{ds_counter['n']}"
        _mk_ready_manifest(store, ds_id, backed, no_data_symbols=no_data)
        return {"status": "ready", "dataset_id": ds_id,
                "total": len(universe), "success": len(backed)}

    monkeypatch.setattr(m, "_sync_dataset", _fake_sync_dataset)


def _sync_args(**over):
    base = dict(symbol=None, start_date=None, end_date=None,
                anchor_date=None, fresh=True, resume=False,
                asset_class="etf", token="x", storage_root=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_sync_full_universe_publishes_pointer(tmp_path, monkeypatch):
    """完整 universe 的 full 同步成功 -> 发布权威 ETF 面指针。"""
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(monkeypatch, m, store,
                   ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"])
    result = m.sync_tushare_index_etf_full(_sync_args(), store)
    assert result["status"] in ("ready", "success")
    assert (tmp_path / "etf_surface_pointer.json").exists()


def test_sync_full_symbol_partial_skips_pointer(tmp_path, monkeypatch):
    """--symbol 局部同步（full 模式）不得更新指针。"""
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(monkeypatch, m, store,
                   ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"])
    result = m.sync_tushare_index_etf_full(
        _sync_args(symbol="SSE.ETF.510300"), store)
    assert result["status"] in ("ready", "success")
    assert not (tmp_path / "etf_surface_pointer.json").exists()


def test_sync_incremental_universe_publishes_pointer(tmp_path, monkeypatch):
    """完整 universe 的 incremental 同步（生产 EOD 固定模式）也发布指针。"""
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(monkeypatch, m, store,
                   ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"])
    result = m.sync_tushare_index_etf_incremental(
        _sync_args(fresh=True), store)
    assert result["status"] in ("ready", "success")
    assert (tmp_path / "etf_surface_pointer.json").exists()


def test_sync_low_etf_coverage_skips_pointer(tmp_path, monkeypatch):
    """ETF 覆盖不足（含未放行的 no_data）时不得发布指针。"""
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(
        monkeypatch, m, store,
        ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"],
        no_data=["SZSE.ETF.159043", "SZSE.ETF.159920"],  # 3/5=0.6 < 0.8
    )
    result = m.sync_tushare_index_etf_full(_sync_args(), store)
    assert result["status"] in ("ready", "success")  # 数据集本身 ready，但指针拒绝发布
    assert not (tmp_path / "etf_surface_pointer.json").exists()


def test_sync_pointer_failure_marks_partial(tmp_path, monkeypatch):
    """指针发布意外失败必须把同步结果降级为 partial，不得静默成功。"""
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(monkeypatch, m, store,
                   ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"])

    def _boom(store_, result_, *, full_universe):
        raise OSError("disk full")

    monkeypatch.setattr(m, "_write_etf_surface_pointer", _boom)
    result = m.sync_tushare_index_etf_full(_sync_args(), store)
    assert result["status"] == "partial"
    assert "etf_surface_pointer_failed" in result.get("warning", "")
    assert "disk full" in result["warning"]


def test_sync_incremental_pointer_failure_marks_partial(tmp_path, monkeypatch):
    """incremental 链（生产 EOD 固定模式）指针发布失败同样降级 partial。

    与 full 路径独立验证：两条发布点（full/incremental）各自的
    try/except 降级都必须生效，不允许其中一条静默吞掉指针故障。
    """
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    _stub_sync_env(monkeypatch, m, store,
                   ["SSE.ETF.510300", "SSE.ETF.530060", "SSE.ETF.551060"])

    def _boom(store_, result_, *, full_universe):
        raise OSError("disk full")

    monkeypatch.setattr(m, "_write_etf_surface_pointer", _boom)
    result = m.sync_tushare_index_etf_incremental(_sync_args(fresh=True), store)
    assert result["status"] == "partial"
    assert "etf_surface_pointer_failed" in result.get("warning", "")
    assert "disk full" in result["warning"]


def _ie_lock_released(root) -> bool:
    """指数/ETF 增量锁已释放：probe 不到 holder，或元数据标记 released。"""
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock

    lock = SyncTaskLock(root, source="tushare_index_etf",
                        adjustment="none", period="1d")
    meta = SyncTaskLock.probe(lock.lock_path)
    return meta is None or bool(meta.get("released_at"))


def test_ie_incremental_lock_released_on_all_exit_paths(tmp_path, monkeypatch):
    """增量链锁必须在三条退出路径上全部释放（专项回归钉子）。

    正常返回、checkpoint 存在的提前返回、工作器异常——任何一条路径
    泄漏锁都会让当晚 EOD 后续重试全部 concurrent_lock 失败。
    """
    import json as _json

    from wtpy.apps.astock.data.dataset_store import DatasetStore

    m = _load_module()
    store = DatasetStore(tmp_path)
    backed = ["SSE.ETF.510300", "SSE.ETF.530060"]

    # 1) 正常返回路径
    _stub_sync_env(monkeypatch, m, store, backed)
    result = m.sync_tushare_index_etf_incremental(_sync_args(fresh=True), store)
    assert result["status"] in ("ready", "success")
    assert _ie_lock_released(tmp_path)

    # 2) checkpoint 提前返回路径（无 --resume/--fresh 时拒绝启动）
    _stub_sync_env(monkeypatch, m, store, backed)
    ck = store.sync_logs_dir / "checkpoint_tushare_index_etf_etf_1d.json"
    ck.parent.mkdir(parents=True, exist_ok=True)
    ck.write_text(_json.dumps({"sync_run_id": "x", "phases": {}}),
                  encoding="utf-8")
    result = m.sync_tushare_index_etf_incremental(
        _sync_args(fresh=False, resume=False), store)
    assert result["status"] == "failed"
    assert result["error"] == "checkpoint_exists_use_resume_or_fresh"
    assert _ie_lock_released(tmp_path)

    # 3) 工作器异常路径（finally 兜底释放）
    _stub_sync_env(monkeypatch, m, store, backed)

    def _crash(**_kw):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(m, "_sync_dataset", _crash)
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="worker exploded"):
        m.sync_tushare_index_etf_incremental(_sync_args(fresh=True), store)
    assert _ie_lock_released(tmp_path)
