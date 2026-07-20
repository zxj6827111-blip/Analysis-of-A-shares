"""Tests for forecast module: matching, weekly import, API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.forecast.kb_loader import ForecastKnowledgeBase
from wtpy.apps.astock.forecast.matcher import match_period
from wtpy.apps.astock.forecast.name_norm import (
    biangua_loose_equal,
    normalize_stock_code,
    parse_bian_field,
    yao_index_to_order,
)
from wtpy.apps.astock.forecast.service import ForecastService

ROOT = Path(__file__).resolve().parents[3]
WEEKLY_XLSX = ROOT / "weekly_analysis_v2_20260717-W29.xlsx"
BAGUA_JSON = (
    ROOT / "wtpy" / "apps" / "astock" / "bagua" / "bagua_384.json"
)
INDICATOR_XLSX = next(
    (ROOT / "指标").glob("*384*.xlsx"),
    None,
)


@pytest.fixture
def forecast_cfg(tmp_path: Path):
    return get_default_config(storage_root=tmp_path / "storage")


@pytest.fixture
def svc(forecast_cfg) -> ForecastService:
    s = ForecastService(forecast_cfg)
    s.seed_kb_from_backtest()
    return s


def test_yao_index_mapping_circular():
    """1..5 = 第1..5爻；0 = 第6爻（上爻）。"""
    assert yao_index_to_order(0) == 6
    assert yao_index_to_order(1) == 1
    assert yao_index_to_order(2) == 2
    assert yao_index_to_order(5) == 5
    assert yao_index_to_order(None) is None
    assert yao_index_to_order(9) is None


def test_parse_bian_field():
    idx, name = parse_bian_field("1-山雷颐")
    assert idx == 1
    assert name == "山雷颐"
    idx2, name2 = parse_bian_field("4-坎为水")
    assert idx2 == 4
    assert name2 == "坎为水"


def test_name_norm_and_code():
    assert normalize_stock_code(1) == "000001"
    assert normalize_stock_code("1") == "000001"
    assert normalize_stock_code("SSE.STK.600000") == "600000"
    assert biangua_loose_equal("颐", "山雷颐")
    assert biangua_loose_equal("坎水", "坎为水")


def test_match_bian_only_shanze_sun(svc: ForecastService):
    """Gold: 0-山泽损 -> 山泽损 第6爻 上九 不减反增无咎大吉."""
    kb = svc._kb
    assert kb is not None
    m = match_period(
        kb,
        period="week",
        ben_raw="地泽临",  # 本卦忽略
        bian_raw="0-山泽损",
    )
    assert m.yao_order == 6
    assert m.bian_norm == "山泽损"
    assert m.match_status == "ok"
    assert "不减反增" in (m.judgement or "")
    # operation_signal may be empty if old kb without 操作信号 column
    assert m.operation_signal is not None
    entry = kb.lookup_ben_yao("山泽损", 6)
    assert entry is not None
    assert entry["market_judgement"] == m.judgement


def test_match_bian_digit_one_is_first_yao(svc: ForecastService):
    """1-山泽损 -> 第1爻."""
    m = match_period(svc._kb, period="week", ben_raw="", bian_raw="1-山泽损")
    assert m.yao_order == 1
    assert m.match_status == "ok"
    assert m.judgement


def test_match_miss_status(svc: ForecastService):
    m = match_period(
        svc._kb,
        period="week",
        ben_raw="山地剥",
        bian_raw="0-不存在的卦",
    )
    assert m.match_status == "miss"
    assert m.judgement == ""
    assert any("检查" in tip for tip in m.tips)


def test_weekly_import_and_quote(svc: ForecastService):
    if not WEEKLY_XLSX.exists():
        pytest.skip("weekly xlsx not in repo")
    meta = svc.upload_weekly(WEEKLY_XLSX)
    assert meta["stock_count"] >= 5000
    assert meta["week_key"] == "2026-W29"

    q = svc.quote("000001")
    assert q["found"] is True
    assert q["name"] == "平安银行"
    # 本周 1-山雷颐 -> 山雷颐 第1爻
    assert q["week_match"]["yao_order"] == 1
    assert q["week_match"]["bian_norm"] == "山雷颐"
    assert q["week_match"]["judgement"]
    # 上月 4-坎为水 -> 坎为水 第4爻
    assert q["month_match"]["yao_order"] == 4
    assert q["month_match"]["bian_norm"] == "坎为水"

    # pinyin initials
    hits = svc.search("payh")
    assert any(h["code"] == "000001" for h in hits)

    # fuzzy name
    hits2 = svc.search("平安银行")
    assert hits2 and hits2[0]["code"] == "000001"

    # missing stock
    miss = svc.quote("999998")
    assert miss["found"] is False
    assert any("未在本周" in t for t in miss["tips"])


def test_batch_and_export(svc: ForecastService, tmp_path: Path):
    if not WEEKLY_XLSX.exists():
        pytest.skip("weekly xlsx not in repo")
    svc.upload_weekly(WEEKLY_XLSX)
    batch = svc.batch_query(["000001", "000002", "999999"])
    assert batch["count"] == 3
    assert batch["results"][0]["found"] is True
    assert batch["results"][2]["found"] is False

    out = svc.export_xlsx(tmp_path / "out.xlsx")
    assert out.exists() and out.stat().st_size > 1000


def test_isolation_paths(forecast_cfg):
    svc = ForecastService(forecast_cfg)
    fr = Path(forecast_cfg.forecast_root)
    assert "forecast" in str(fr).replace("\\", "/")
    assert Path(forecast_cfg.forecast_kb_path) != Path(forecast_cfg.bagua_json)
    # seed writes only under forecast
    svc.seed_kb_from_backtest()
    assert Path(forecast_cfg.forecast_kb_path).exists()
    # bagua_json unchanged path still exists separately
    assert Path(forecast_cfg.bagua_json).exists()


def test_api_forecast_routes(forecast_cfg, tmp_path: Path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    if not WEEKLY_XLSX.exists():
        pytest.skip("weekly xlsx not in repo")

    app = create_app(forecast_cfg)
    client = TestClient(app)

    h = client.get("/api/v1/forecast/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    seed = client.post("/api/v1/forecast/kb/seed")
    assert seed.status_code == 200
    assert seed.json()["count_yao"] == 384

    with WEEKLY_XLSX.open("rb") as f:
        up = client.post(
            "/api/v1/forecast/weekly/upload",
            files={
                "file": (
                    WEEKLY_XLSX.name,
                    f,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["stock_count"] >= 5000

    quote = client.get("/api/v1/forecast/quote", params={"code": "1"})
    assert quote.status_code == 200
    data = quote.json()
    assert data["found"] is True
    assert data["week_match"]["judgement"]

    search = client.get("/api/v1/forecast/search", params={"q": "payh"})
    assert search.status_code == 200
    assert any(x["code"] == "000001" for x in search.json())

    batch = client.post(
        "/api/v1/forecast/batch/query",
        json={"codes": ["000001", "000002"]},
    )
    assert batch.status_code == 200
    assert batch.json()["count"] == 2

    exp = client.get("/api/v1/forecast/export")
    assert exp.status_code == 200
    assert (
        "spreadsheet"
        in exp.headers.get("content-type", "")
        or exp.content[:2] == b"PK"
    )


def test_kb_import_xlsx_if_present(forecast_cfg):
    if INDICATOR_XLSX is None or not INDICATOR_XLSX.exists():
        pytest.skip("indicator xlsx missing")
    svc = ForecastService(forecast_cfg)
    res = svc.import_kb_xlsx(INDICATOR_XLSX)
    assert res["count_yao"] == 384
    assert svc._kb is not None
    assert svc._kb.lookup_ben_yao("乾为天", 1) is not None
