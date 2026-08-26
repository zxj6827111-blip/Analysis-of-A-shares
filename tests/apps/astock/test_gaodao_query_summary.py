# -*- coding: utf-8 -*-
"""单股 / 指数 ETF 卦象查询 summary 透出高岛易断解读（Task 4）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.bagua import gaodao as gd
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service import bagua_query as bq

BAGUA_DIR = (
    Path(__file__).resolve().parents[3] / "wtpy" / "apps" / "astock" / "bagua"
)
JSON_PATH = BAGUA_DIR / "bagua_384.json"
SIDECAR_PATH = BAGUA_DIR / "bagua_gaodao.json"

# 已验证的 OHLC → state_id 映射（BaguaCalculator 反查得到，见 build 说明）
#   6.27/7.33/5.90/5.90  -> 04-3 山水蒙·六三（有营商断语）
#   10.07/10.57/9.57/1.00 -> 11-4 地天泰·六四（原书无任何占断，5 个缺口之一）
BARS_HIT = DayBar(20240103, 6.27, 7.33, 5.90, 5.90, 1.0, 1.0)
BARS_MISS = DayBar(20240103, 10.07, 10.57, 9.57, 1.00, 1.0, 1.0)


@pytest.fixture(autouse=True)
def _no_tushare_network(monkeypatch):
    def _fail(*_a, **_k):
        raise RuntimeError("Tushare network disabled in tests")

    monkeypatch.setattr(bq, "_fetch_symbol_meta_from_tushare", _fail)
    monkeypatch.setattr(bq, "_SYMBOL_META_CACHE", {})
    gd.invalidate_gaodao_cache()
    yield
    gd.invalidate_gaodao_cache()


@pytest.fixture(autouse=True)
def _require_data():
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")
    if not SIDECAR_PATH.exists():
        pytest.skip("bagua_gaodao.json missing")


def make_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        bagua_json=JSON_PATH,
        bagua_gaodao_json=SIDECAR_PATH,
        storage_root=Path("."),
        market_data_root=Path("."),
        tdx_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_stock_summary_has_gaodao(monkeypatch):
    monkeypatch.setattr(bq, "load_day_bars", lambda *_a, **_k: [BARS_HIT])
    out = bq.query_bagua(make_cfg(), code="600000", date="2024-01-03", period="DAY")
    assert out["bagua"]["state_id"] == "04-3"
    s = out["summary"]
    assert s["gaodao_commerce"]
    assert s["gaodao_category"] == "营商"
    # 高岛断语与行情简判是两条独立文本，不应互相覆盖
    assert s["market_judgement"]
    assert s["gaodao_commerce"] != s["market_judgement"]


def test_stock_summary_gaodao_empty_for_missing_state(monkeypatch):
    monkeypatch.setattr(bq, "load_day_bars", lambda *_a, **_k: [BARS_MISS])
    out = bq.query_bagua(make_cfg(), code="600000", date="2024-01-03", period="DAY")
    assert out["bagua"]["state_id"] == "11-4"  # 原书无占断
    s = out["summary"]
    assert s["gaodao_commerce"] == ""
    assert s["gaodao_category"] == ""
    assert s["market_judgement"]  # 兜底文本仍在，不至于整段空白


def test_index_summary_has_gaodao(monkeypatch):
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda *_a, **_k: [BARS_HIT])
    out = bq.query_bagua(make_cfg(), code="sh000001", date="2024-01-03", period="DAY")
    assert out["symbol_type"] == "index"
    assert out["bagua"]["state_id"] == "04-3"
    assert out["summary"]["gaodao_commerce"]
    assert out["summary"]["gaodao_category"] == "营商"


def test_etf_summary_has_gaodao(monkeypatch):
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda *_a, **_k: [BARS_HIT])
    out = bq.query_bagua(make_cfg(), code="sh510300", date="2024-01-03", period="DAY")
    assert out["symbol_type"] == "etf"
    assert out["summary"]["gaodao_commerce"]


def test_summary_keys_always_present_without_sidecar(monkeypatch, tmp_path):
    """sidecar 缺失时字段仍存在（空串），前端可无条件读取（fail-open 回归）。"""
    monkeypatch.setattr(bq, "load_day_bars", lambda *_a, **_k: [BARS_HIT])
    cfg = make_cfg(bagua_gaodao_json=tmp_path / "absent.json")
    out = bq.query_bagua(cfg, code="600000", date="2024-01-03", period="DAY")
    assert out["summary"]["gaodao_commerce"] == ""
    assert out["summary"]["gaodao_category"] == ""
    assert out["ok"] is True
