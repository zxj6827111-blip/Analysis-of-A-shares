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
