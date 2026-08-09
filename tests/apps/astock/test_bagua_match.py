# -*- coding: utf-8 -*-
"""Tests for same-hexagram (同卦) and same-day-pillar (同日柱) stock matching."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.service import bagua_query as bq

JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def _cfg(**over):
    base = dict(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        market_data_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fake_target(state_id="05-3"):
    return {
        "ok": True,
        "code": "sh600000",
        "name": "浦发银行",
        "display": "sh600000 浦发银行",
        "std_code": "SSE.STK.600000",
        "symbol_type": "stock",
        "period": "DAY",
        "adjust": "raw",
        "bar": {"date": 20240103, "start_date": 20240103, "end_date": 20240103,
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        "bagua": {"state_id": state_id, "full_name": "䷇水地比", "gua_name": "水地比",
                  "yao_name": "六三", "yao_order": 3, "action_signal": "",
                  "market_judgement": ""},
        "summary": {"state_id": state_id, "full_name": "䷇水地比",
                    "yao_name": "六三", "yao_order": 3},
    }


def _fake_row(code, name, std, state_id, o=1.0, c=1.0):
    return {
        "ok": True,
        "code": code,
        "name": name,
        "display": f"{code} {name}",
        "std_code": std,
        "symbol_type": "stock",
        "period": "DAY",
        "adjust": "raw",
        "bar": {"date": 20240103, "start_date": 20240103, "end_date": 20240103,
                "open": o, "high": o, "low": o, "close": c},
        "bagua": {"state_id": state_id, "full_name": "䷇水地比", "gua_name": "水地比",
                  "yao_name": "六三", "yao_order": 3, "action_signal": "",
                  "market_judgement": ""},
        "summary": {"state_id": state_id, "full_name": "䷇水地比",
                    "yao_name": "六三", "yao_order": 3},
    }


def _no_session(*_a, **_k):
    raise FileNotFoundError("no market data root")


def _monkey_no_session(monkeypatch):
    monkeypatch.setattr(bq, "BaguaPlaneSession", _no_session)


# ---------------------------------------------------------------- find_same_bagua

def test_find_same_bagua_filters_by_state_id(monkeypatch):
    _monkey_no_session(monkeypatch)
    monkeypatch.setattr(bq, "query_bagua", lambda *_a, **_k: _fake_target("05-3"))
    monkeypatch.setattr(
        bq,
        "batch_query_bagua",
        lambda *_a, **_k: {
            "ok": True,
            "requested": 5,
            "count": 5,
            "results": [
                _fake_row("sh600000", "浦发银行", "SSE.STK.600000", "05-3"),  # target
                _fake_row("sh600001", "邯郸钢铁", "SSE.STK.600001", "05-3"),
                _fake_row("sz000001", "平安银行", "SZSE.STK.000001", "11-1"),
                _fake_row("sz000002", "万科A", "SZSE.STK.000002", "05-3"),
                _fake_row("sz000003", "深振业A", "SZSE.STK.000003", "02-4"),
            ],
        },
    )
    out = bq.find_same_bagua(_cfg(), code="600000", date="2024-01-03", period="DAY")
    assert out["ok"] is True
    assert out["mode"] == "bagua"
    assert out["match_key"] == "05-3"
    assert out["target"]["std_code"] == "SSE.STK.600000"
    # target excluded, only other stocks with the same state_id kept
    got = [r["std_code"] for r in out["results"]]
    assert got == ["SSE.STK.600001", "SZSE.STK.000002"]
    assert out["count"] == 2


def test_find_same_bagua_limit(monkeypatch):
    _monkey_no_session(monkeypatch)
    monkeypatch.setattr(bq, "query_bagua", lambda *_a, **_k: _fake_target("05-3"))
    monkeypatch.setattr(
        bq,
        "batch_query_bagua",
        lambda *_a, **_k: {
            "ok": True,
            "requested": 3,
            "count": 3,
            "results": [
                _fake_row("sh600001", "邯郸钢铁", "SSE.STK.600001", "05-3"),
                _fake_row("sz000002", "万科A", "SZSE.STK.000002", "05-3"),
                _fake_row("sz000003", "深振业A", "SZSE.STK.000003", "05-3"),
            ],
        },
    )
    out = bq.find_same_bagua(_cfg(), code="600000", date=20240103, limit=1)
    assert out["count"] == 1
    assert len(out["results"]) == 1


def test_find_same_bagua_no_match(monkeypatch):
    _monkey_no_session(monkeypatch)
    monkeypatch.setattr(bq, "query_bagua", lambda *_a, **_k: _fake_target("05-3"))
    monkeypatch.setattr(
        bq,
        "batch_query_bagua",
        lambda *_a, **_k: {
            "ok": True,
            "requested": 2,
            "count": 2,
            "results": [
                _fake_row("sz000001", "平安银行", "SZSE.STK.000001", "11-1"),
                _fake_row("sz000003", "深振业A", "SZSE.STK.000003", "02-4"),
            ],
        },
    )
    out = bq.find_same_bagua(_cfg(), code="600000", date=20240103)
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["results"] == []


def test_find_same_bagua_target_error_raises(monkeypatch):
    _monkey_no_session(monkeypatch)
    monkeypatch.setattr(
        bq,
        "query_bagua",
        lambda *_a, **_k: {"ok": False, "code": "sh600000", "error": "no market data"},
    )
    with pytest.raises(ValueError, match="目标股票"):
        bq.find_same_bagua(_cfg(), code="600000", date=20240103)


def test_find_same_bagua_tdx_front_raises(monkeypatch):
    from wtpy.apps.astock.service.bagua_query import SourceDisabledError

    _monkey_no_session(monkeypatch)
    monkeypatch.setattr(bq, "query_bagua", lambda *_a, **_k: _fake_target("05-3"))
    with pytest.raises(SourceDisabledError, match="已停用"):
        bq.find_same_bagua(_cfg(), code="600000", date=20240103, adjust="tdx_front")


# --------------------------------------------------------------- find_same_rizhu

def test_find_same_rizhu_filters_and_excludes_target(monkeypatch):
    monkeypatch.setattr(
        bq,
        "load_rizhu_map",
        lambda _p=None: {"600000": "甲子", "000001": "乙丑", "000002": "甲子", "000003": "丙寅"},
    )
    monkeypatch.setattr(
        bq,
        "_resolve_batch_codes",
        lambda _cfg, codes, all_stocks=False: ["600000", "000001", "000002", "000003"],
    )
    monkeypatch.setattr(
        bq,
        "resolve_stock_name",
        lambda _cfg, _code, std_code="": f"股票{std_code.split('.')[-1]}",
    )
    out = bq.find_same_rizhu(_cfg(), code="600000")
    assert out["ok"] is True
    assert out["mode"] == "rizhu"
    assert out["match_key"] == "甲子"
    assert out["target"]["code6"] == "600000"
    got = [r["code6"] for r in out["results"]]
    # target (600000) excluded; only 000002 shares 甲子
    assert got == ["000002"]
    assert out["count"] == 1


def test_find_same_rizhu_not_in_table(monkeypatch):
    monkeypatch.setattr(bq, "load_rizhu_map", lambda _p=None: {"000001": "乙丑"})
    monkeypatch.setattr(
        bq,
        "_resolve_batch_codes",
        lambda _cfg, codes, all_stocks=False: ["000001"],
    )
    monkeypatch.setattr(bq, "resolve_stock_name", lambda _c, _k, std_code="": "")
    out = bq.find_same_rizhu(_cfg(), code="600000")
    assert out["ok"] is False
    assert "未在日柱表中找到" in out["error"]
    assert out["count"] == 0


def test_find_same_rizhu_limit(monkeypatch):
    monkeypatch.setattr(
        bq,
        "load_rizhu_map",
        lambda _p=None: {"600000": "甲子", "000001": "甲子", "000002": "甲子", "000003": "甲子"},
    )
    monkeypatch.setattr(
        bq,
        "_resolve_batch_codes",
        lambda _cfg, codes, all_stocks=False: ["600000", "000001", "000002", "000003"],
    )
    monkeypatch.setattr(
        bq,
        "resolve_stock_name",
        lambda _cfg, _code, std_code="": f"股票{std_code.split('.')[-1]}",
    )
    out = bq.find_same_rizhu(_cfg(), code="600000", limit=2)
    assert out["count"] == 2
    assert len(out["results"]) == 2


def test_find_same_rizhu_scanned_reports_universe(monkeypatch):
    monkeypatch.setattr(
        bq,
        "load_rizhu_map",
        lambda _p=None: {"600000": "甲子", "000001": "乙丑"},
    )
    monkeypatch.setattr(
        bq,
        "_resolve_batch_codes",
        lambda _cfg, codes, all_stocks=False: ["600000", "000001"],
    )
    monkeypatch.setattr(bq, "resolve_stock_name", lambda _c, _k, std_code="": "")
    out = bq.find_same_rizhu(_cfg(), code="600000")
    assert out["scanned"] == 2
    assert out["count"] == 0


# ------------------------------------------------------------- identity semantics

def test_state_id_identity_captures_main_and_moving_line():
    """Same (upper, lower, yao) -> same state_id; different yao -> different."""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")
    calc = BaguaCalculator.from_json(JSON_PATH)
    r1 = calc.calculate(open_price=6.27, high_price=7.33, low_price=5.90, close_price=5.90)
    # different OHLC but same digit sums -> same state
    r2 = calc.calculate(open_price=6.27, high_price=5.90, low_price=7.33, close_price=5.90)
    # high+low digit sums swap -> same total -> same yao
    assert r1.state_id == r2.state_id
    assert r1.gua_order == r2.gua_order
    # change close digit sum -> different lower trigram / main hexagram
    r3 = calc.calculate(open_price=6.27, high_price=7.33, low_price=5.90, close_price=6.27)
    assert (r1.state_id != r3.state_id) or (r1.gua_order != r3.gua_order)
