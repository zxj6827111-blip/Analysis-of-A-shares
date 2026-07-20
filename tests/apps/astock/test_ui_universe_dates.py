"""Universe resolution: full market token vs explicit codes."""

from __future__ import annotations

import json
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.backtest import select_universe


def test_select_universe_full_market_token(tmp_path: Path):
    storage = tmp_path / "st"
    storage.mkdir()
    uni = {
        "count": 3,
        "exclude_bj": True,
        "schema_version": 1,
        "survivor_bias_warning": "",
        "symbols": [
            {"raw": "sh600000", "std_code": "SSE.STK.600000", "exchange": "SSE", "code": "600000", "name": "", "product": "STK"},
            {"raw": "sh600004", "std_code": "SSE.STK.600004", "exchange": "SSE", "code": "600004", "name": "", "product": "STK"},
            {"raw": "sz000001", "std_code": "SZSE.STK.000001", "exchange": "SZSE", "code": "000001", "name": "", "product": "STK"},
        ],
    }
    (storage / "universe.json").write_text(json.dumps(uni), encoding="utf-8")
    cfg = get_default_config(storage_root=storage)

    full = select_universe(cfg, ["ALL"])
    assert len(full) == 3
    assert "SSE.STK.600000" in full

    full2 = select_universe(cfg, "全市场")
    assert set(full2) == set(full)

    full3 = select_universe(cfg, None)
    assert set(full3) == set(full)

    custom = select_universe(cfg, ["sh600000"])
    assert custom == ["SSE.STK.600000"]


def test_frontend_has_full_market_and_date_dropdowns():
    from wtpy.apps.astock.api import STATIC_DIR

    text = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'value="full"' in text or "全市场" in text
    assert "use_full_market" in text
    assert "startYear" in text and "endYear" in text
    assert "startMonth" in text and "endMonth" in text
    assert "startDay" in text and "endDay" in text
    assert "/api/v1/calendar/range" in text


def test_api_calendar_and_full_market_flag(tmp_path: Path):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    (storage / "calendar.json").write_text(
        json.dumps({"dates": [20240102, 20240103, 20240630, 20241231]}),
        encoding="utf-8",
    )
    uni = {
        "count": 2,
        "exclude_bj": True,
        "schema_version": 1,
        "survivor_bias_warning": "",
        "symbols": [
            {"raw": "sh600000", "std_code": "SSE.STK.600000", "exchange": "SSE", "code": "600000", "name": "", "product": "STK"},
            {"raw": "sz000001", "std_code": "SZSE.STK.000001", "exchange": "SZSE", "code": "000001", "name": "", "product": "STK"},
        ],
    }
    (storage / "universe.json").write_text(json.dumps(uni), encoding="utf-8")
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    cal = client.get("/api/v1/calendar/range")
    assert cal.status_code == 200
    body = cal.json()
    assert 2024 in body["years"]
    assert body["min_date"] == 20240102
    assert body["max_date"] == 20241231

    u = client.get("/api/v1/universe/summary")
    assert u.status_code == 200
    assert u.json()["global_universe_count"] == 2
    assert u.json().get("full_market_token") == "ALL"

    page = client.get("/")
    assert page.status_code == 200
    assert "全部 A 股" in page.text or "全市场" in page.text
    assert "startYear" in page.text
