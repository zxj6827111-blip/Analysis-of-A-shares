# -*- coding: utf-8 -*-
"""Tests for bagua single-stock query service."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service import bagua_query as bq


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def test_normalize_period_and_code():
    assert bq.normalize_period("日") == "DAY"
    assert bq.normalize_period("按月") == "MONTH"
    assert bq.normalize_period("WEEK") == "WEEK"
    assert bq.normalize_query_code("600000") == "SSE.STK.600000"
    assert bq.normalize_query_code("sh600000") == "SSE.STK.600000"
    assert bq.normalize_query_code("000001") == "SZSE.STK.000001"
    assert bq.display_code("SSE.STK.600000") == "sh600000"


def test_parse_ymd():
    assert bq._parse_ymd("2024-01-15") == 20240115
    assert bq._parse_ymd(20240115) == 20240115
    assert bq._parse_ymd("2024/01/15") == 20240115


def test_find_day_bar_fallback():
    bars = [
        DayBar(20240102, 10, 11, 9, 10.5, 1, 1),
        DayBar(20240103, 10.5, 11, 10, 10.8, 1, 1),
        DayBar(20240105, 10.8, 11.2, 10.5, 11, 1, 1),
    ]
    bar, exact = bq._find_day_bar(bars, 20240103)
    assert exact and bar.date == 20240103
    bar2, exact2 = bq._find_day_bar(bars, 20240104)  # holiday
    assert not exact2 and bar2.date == 20240103


def test_query_bagua_with_mock_bars(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [
        DayBar(
            date=20240103,
            open=6.27,
            high=7.33,
            low=5.90,
            close=5.90,
            amount=1.0,
            volume=1.0,
        ),
    ]

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
    )
    monkeypatch.setattr(bq, "load_day_bars", lambda _cfg, _code: bars)

    out = bq.query_bagua(cfg, code="600000", date="2024-01-03", period="DAY")
    assert out["ok"] is True
    assert out["period"] == "DAY"
    assert out["code"] == "sh600000"
    assert out["bar"]["open"] == 6.27
    assert out["bagua"]["upper_id"] == 7
    assert out["bagua"]["lower_id"] == 6
    assert out["bagua"]["yao_order"] == 3
    assert out["summary"]["yao_order"] == 3
    assert "山水" in (out["summary"]["full_name"] or out["bagua"].get("full_name") or "")


def test_calculator_same_as_query(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")
    calc = BaguaCalculator.from_json(JSON_PATH)
    r = calc.calculate(open_price=6.27, high_price=7.33, low_price=5.90, close_price=5.90)
    bars = [
        DayBar(20240103, 6.27, 7.33, 5.90, 5.90, 1, 1),
    ]
    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
    )
    monkeypatch.setattr(bq, "load_day_bars", lambda *_a, **_k: bars)
    out = bq.query_bagua(cfg, code="sz000001", date=20240103, period="DAY")
    assert out["bagua"]["full_name"] == r.full_name
    assert out["bagua"]["yao_name"] == r.yao_name
    assert "name" in out
    assert "display" in out
