"""API + service structural and functional tests (TestClient)."""

from __future__ import annotations

from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

import pytest

from wtpy.apps.astock.api import STATIC_DIR, create_app
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.backtest import BacktestRequest


def test_static_frontend_exists():
    index = STATIC_DIR / "index.html"
    assert index.is_file(), f"missing frontend {index}"
    text = index.read_text(encoding="utf-8")
    assert "entry_lag" in text or "entryLag" in text
    assert "/api/v1/backtests" in text
    assert "/api/v1/rules" in text


def test_api_health_and_rules(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    ind.mkdir()
    storage.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    h = client.get("/api/v1/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    v = client.post(
        "/api/v1/rules/validate",
        json={"formula_text": "XG:C>0;", "name": "t"},
    )
    assert v.status_code == 200
    assert v.json()["ok"] is True

    c = client.post(
        "/api/v1/rules",
        json={"name": "api_rule", "formula_text": "XG:C>OPEN;"},
    )
    assert c.status_code == 200
    rid = c.json()["id"]
    assert rid.startswith("user_")

    lst = client.get("/api/v1/rules")
    assert lst.status_code == 200
    assert any(r["id"] == rid for r in lst.json())

    page = client.get("/")
    assert page.status_code == 200
    assert "回测" in page.text


def test_backtest_request_to_dict_includes_entry_lag():
    req = BacktestRequest(rule_ids=["x"], entry_lag=2, hold=3)
    d = req.to_dict()
    assert d["entry_lag"] == 2
    assert d["hold"] == 3
    assert d["rule_ids"] == ["x"]
    assert d["corporate_action_policy"] is None


def test_api_maps_corporate_action_policy_to_request(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.service.backtest import BacktestService

    captured = {}

    def fake_run(self, req, *, progress_cb=None):
        captured["request"] = req
        return {"run_id": "bt_ca_api", "status": "ok"}

    monkeypatch.setattr(BacktestService, "run", fake_run)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/api/v1/backtests",
        json={
            "rule_ids": ["test_rule"],
            "codes": ["SSE.STK.600000"],
            "corporate_action_policy": "event_ledger",
        },
    )

    assert response.status_code == 200
    assert captured["request"].corporate_action_policy == "event_ledger"

def test_factor_sync_start_adds_universe_file_from_env(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    cmd = started["args"][0]
    assert "--adjustment" in cmd
    assert "adj_factor" in cmd
    assert "--universe-file" in cmd
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)
    assert cmd[cmd.index("--end-date") + 1]


def test_factor_sync_start_reuses_latest_manifest_universe(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUSHARE_FACTOR_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("ASTOCK_FACTOR_UNIVERSE_FILE", raising=False)
    universe = tmp_path / "manifest_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    store = DatasetStore(cfg.market_data_root)
    store.save_manifest(
        DatasetManifest(
            dataset_id="tushare_adjfactor_1d_test",
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            status="ready",
            dataset_type="factor",
            data_cutoff_date=20260729,
            symbol_count=1,
            universe_file=str(universe),
        )
    )

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    cmd = started["args"][0]
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)


def test_dashboard_overview_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    r = client.get("/api/v1/dashboard/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for key in ("data", "sync", "ca", "universe", "findings", "watchlist"):
        assert key in d
    # Graceful on a bare server: data block reports the missing root, no crash.
    assert d["data"]["exists"] is False
    assert isinstance(d["findings"], list)
    assert isinstance(d["watchlist"]["count"], (int, type(None)))

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "关键发现" in page.text

    index = client.get("/")
    assert "/dashboard" in index.text


def test_quick_query_endpoint_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    # Structure is stable regardless of data availability.
    r = client.get("/api/v1/quick/600000")
    assert r.status_code == 200
    d = r.json()
    assert d["std_code"] in ("SSE.STK.600000", "sh600000")
    for key in ("code", "name", "std_code", "market", "gua", "related_runs"):
        assert key in d
    assert isinstance(d["related_runs"], list)
    # market degrades gracefully without warehouse data
    assert "market" in d and isinstance(d["market"], dict)

    # Chinese-name input resolves to a code.
    r = client.get("/api/v1/quick/平安银行")
    assert r.status_code == 200

    # Invalid input -> 4xx, not 500.
    r = client.get("/api/v1/quick/zzzzz")
    assert r.status_code in (400, 404)

    page = client.get("/quick.html?code=600000")
    assert page.status_code == 200
    assert "个股快速查询" in page.text

    index = client.get("/")
    assert "quickCode" in index.text and "/quick.html" in index.text