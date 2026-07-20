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
