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

    # V3 is main UI; legacy index still ships full date dropdowns.
    v3 = (STATIC_DIR / "index_v3.html").read_text(encoding="utf-8")
    legacy = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "全市场" in v3 or "全部 A 股" in v3 or "use_full_market" in v3 or "full" in v3
    assert "use_full_market" in legacy or "全市场" in legacy
    # V3 date parts
    assert "btStartYear" in v3 and "btStartMonth" in v3 and "btStartDay" in v3
    assert "btEndYear" in v3 or "btEndMonth" in v3
    # Legacy date parts
    assert "startYear" in legacy and "endYear" in legacy
    assert "startMonth" in legacy and "endMonth" in legacy
    assert "startDay" in legacy and "endDay" in legacy
    assert "/api/v1/calendar/range" in legacy or "/api/v1/calendar/range" in v3


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
    assert "全部 A 股" in page.text or "全市场" in page.text or "AStock" in page.text
    # Main page is V3 — date controls use btStartYear*
    assert "btStartYear" in page.text or "btStartMonth" in page.text
    # Legacy static still has startYear
    from wtpy.apps.astock.api import STATIC_DIR

    assert "startYear" in (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _publish_raw_base(store, symbols: dict) -> None:
    """Minimal tushare/none ready baseline (mirrors reconcile test helper)."""
    import numpy as np

    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        SymbolRecord,
    )

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
        dataset_id=f"tushare_none_1d_20260812_{abs(hash(sym)) % 999999:x}",
        source="tushare",
        adjustment="none",
        period="1d",
        status="ready",
        data_cutoff_date=20260812,
        symbols=records,
        symbol_count=len(records),
        row_count=total,
    )
    store.publish(m)


def test_full_market_falls_back_to_raw_manifest_without_universe_json(
        tmp_path: Path, monkeypatch
    ):
    """Tushare-only fresh installs never produce universe.json; the full
    market must be derived from the raw baseline instead of collapsing to
    the two demo codes (600000/000001)."""
    import numpy as np

    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    storage.mkdir()
    md = tmp_path / "md"
    md.mkdir()
    # no universe.json on purpose; MARKET_DATA_ROOT env drives the data root
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md))
    cfg = get_default_config(storage_root=storage)

    def _bar_dict(n_rows: int = 300) -> dict:
        import datetime as _dt

        d0 = _dt.datetime.strptime("20260101", "%Y%m%d").date()
        dates = np.array(
            [int((d0 + _dt.timedelta(days=i)).strftime("%Y%m%d"))
             for i in range(n_rows)],
            dtype=np.int64,
        )
        close = np.linspace(10.0, 12.0, n_rows)
        return {
            "trade_date": dates,
            "open": close - 0.5,
            "high": close + 0.3,
            "low": close - 0.8,
            "close": close,
            "volume": np.full(n_rows, 1_000_000.0),
            "amount": np.full(n_rows, 10_000_000.0),
        }

    store = DatasetStore(md)
    _publish_raw_base(
        store,
        {sym: _bar_dict() for sym in (
            "SSE.STK.600000", "SSE.STK.600004", "SZSE.STK.000001",
        )},
    )

    from wtpy.apps.astock.service.backtest_universe import select_universe

    full = select_universe(cfg, None)
    # derives from the raw baseline (all 3 symbols), not the demo pair
    assert "SSE.STK.600000" in full
    assert len(full) >= 3


def test_full_market_empty_data_root_falls_back_to_demo(
        tmp_path: Path, monkeypatch
    ):
    """No universe.json AND no usable raw baseline -> demo codes, never crash."""
    storage = tmp_path / "st"
    storage.mkdir()
    md = tmp_path / "md"
    md.mkdir()
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md))
    cfg = get_default_config(storage_root=storage)

    from wtpy.apps.astock.service.backtest_universe import (
        DEMO_CODES,
        select_universe,
    )

    full = select_universe(cfg, None)
    assert set(full) == set(DEMO_CODES)
