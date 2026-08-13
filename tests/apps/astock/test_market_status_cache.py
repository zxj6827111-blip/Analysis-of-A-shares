# -*- coding: utf-8 -*-
"""market-data/status 30s TTL cache behavior (page-refresh perf fix)."""

from __future__ import annotations

from pathlib import Path


def test_market_data_status_ttl_cache(tmp_path: Path, monkeypatch):
    """Second call within TTL returns cached payload without rescanning."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data import repository as repo_mod

    calls = {"n": 0}
    orig_list = repo_mod.MarketDataRepository.list_datasets

    def counting(self, **kwargs):
        calls["n"] += 1
        return orig_list(self, **kwargs)

    monkeypatch.setattr(repo_mod.MarketDataRepository, "list_datasets", counting)

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)

    app = create_app(cfg)
    client = TestClient(app)

    r1 = client.get("/api/v1/market-data/status")
    assert r1.status_code == 200
    n1 = calls["n"]
    assert n1 >= 1, "first call must actually scan the warehouse"

    r2 = client.get("/api/v1/market-data/status")
    assert r2.status_code == 200
    assert calls["n"] == n1, "second call within TTL must hit the cache"
    assert r1.json() == r2.json()


def test_market_data_status_cache_expiry(tmp_path: Path, monkeypatch):
    """After TTL elapses a fresh scan runs again."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.api_routes import system as sys_mod
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data import repository as repo_mod

    calls = {"n": 0}
    orig_list = repo_mod.MarketDataRepository.list_datasets

    def counting(self, **kwargs):
        calls["n"] += 1
        return orig_list(self, **kwargs)

    monkeypatch.setattr(repo_mod.MarketDataRepository, "list_datasets", counting)

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)

    app = create_app(cfg)
    client = TestClient(app)

    client.get("/api/v1/market-data/status")
    assert calls["n"] == 1

    # force TTL to expire
    mdc = app.state.astock.md_status_cache
    mdc["ts"] = 0.0

    client.get("/api/v1/market-data/status")
    assert calls["n"] == 2


def test_tushare_tile_excludes_etf_only_dataset(tmp_path: Path, monkeypatch):
    """纯 ETF 数据集不能覆盖「Tushare日线」卡（raw 显示全市场股票）。"""
    import numpy as np
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)
    store = DatasetStore(md_root)

    import datetime as _dt

    d0 = _dt.datetime.strptime("20260101", "%Y%m%d").date()

    def _publish(dataset_id, symbols, cutoff):
        recs = []
        for sym in symbols:
            n = 300
            dates = np.array(
                [int((d0 + _dt.timedelta(days=i)).strftime("%Y%m%d"))
                 for i in range(n)],
                dtype=np.int64,
            )
            close = np.linspace(10.0, 12.0, n)
            arr = {
                "trade_date": dates, "open": close - 0.5,
                "high": close + 0.3, "low": close - 0.8, "close": close,
                "volume": np.full(n, 1_000_000.0),
                "amount": np.full(n, 10_000_000.0),
            }
            sha = store.store_bar_arrays(sym, arr)
            recs.append(SymbolRecord(
                symbol=sym, blob_sha256=sha,
                first_date=int(dates[0]), last_date=int(dates[-1]),
                row_count=n, quality="ok",
            ))
        store.publish(DatasetManifest(
            dataset_id=dataset_id, source="tushare", adjustment="none",
            period="1d", status="ready", data_cutoff_date=cutoff,
            symbols=recs, symbol_count=len(recs), row_count=300 * len(recs),
        ))

    # 股票基线（较旧 cutoff）
    _publish("tushare_none_1d_20260812_full",
             ["SSE.STK.600000", "SSE.STK.600004", "SZSE.STK.000001"],
             20260812)
    # 纯 ETF 数据集（较新 cutoff）
    _publish("tushare_none_1d_20260813_etf",
             ["SSE.ETF.510300", "SSE.ETF.510500", "SSE.ETF.510050"],
             20260813)

    app = create_app(cfg)
    client = TestClient(app)
    r = client.get("/api/v1/market-data/status")
    assert r.status_code == 200
    sf = {it["key"]: it for it in r.json()["source_freshness"]}
    assert sf["tushare"]["dataset_id"] == "tushare_none_1d_20260812_full"
    assert sf["tushare"]["symbol_count"] == 3
