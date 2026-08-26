# -*- coding: utf-8 -*-
"""状态列表 / 卦象目录透出高岛字段（Task 3）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wtpy.apps.astock.bagua import gaodao as gd
from wtpy.apps.astock.config import AStockConfig, get_default_config
from wtpy.apps.astock.service import gua as gua_svc


@pytest.fixture(autouse=True)
def _clear_cache():
    gd.invalidate_gaodao_cache()
    yield
    gd.invalidate_gaodao_cache()


@pytest.fixture
def cfg() -> AStockConfig:
    c = get_default_config()
    if not Path(c.bagua_json).exists():
        pytest.skip("bagua_384.json missing")
    if not Path(c.bagua_gaodao_json).exists():
        pytest.skip("bagua_gaodao.json missing")
    return c


def _find(items, state_id):
    for it in items:
        if it.get("state_id") == state_id:
            return it
    raise AssertionError(f"state_id {state_id} not in items")


def test_list_states_exposes_gaodao(cfg):
    data = gua_svc.list_states(cfg, page_size=384)
    assert data["total"] == 384
    hit = _find(data["items"], "01-1")
    assert "潜" in hit["gaodao_commerce"]
    assert hit["gaodao_category"] == "营商"
    # 11-4 是原书无占断的 5 爻之一：留空由 market_judgement 兜底
    miss = _find(data["items"], "11-4")
    assert miss["gaodao_commerce"] == ""
    assert miss["gaodao_category"] == ""
    assert miss["market_judgement"]  # 兜底文本仍在


def test_list_states_gaodao_coverage_payload(cfg):
    data = gua_svc.list_states(cfg, page_size=1)
    cov = data["gaodao_coverage"]
    assert cov["total"] == 379
    assert cov["missing"] == 5
    assert "11-4" in cov["missing_state_ids"]


def test_list_states_fallback_category_has_suffix(cfg):
    """时运/功名兜底的爻在展示串里标注类别，避免误当营商断语。"""
    data = gua_svc.list_states(cfg, page_size=384)
    fallbacks = [
        it for it in data["items"]
        if it["gaodao_category"] and it["gaodao_category"] not in ("营商", "商业", "经商",
                                                                  "买卖", "贸易", "营业",
                                                                  "生意", "财运")
    ]
    assert len(fallbacks) == 4
    for it in fallbacks:
        assert it["gaodao_commerce"].endswith("（" + it["gaodao_category"] + "）")


def test_list_hexagrams_lines_expose_gaodao(cfg):
    items = gua_svc.list_hexagrams(cfg)
    assert len(items) == 64
    qian = items[0]
    line1 = qian["lines"][0]
    assert line1["state_id"] == "01-1"
    assert "潜" in line1["gaodao_commerce"]
    assert line1["gaodao_category"] == "营商"
    # 每爻都必须带这两个键（无值为空串），前端可无条件读取
    for h in items:
        for ln in h["lines"]:
            assert "gaodao_commerce" in ln
            assert "gaodao_category" in ln


def test_list_states_survives_missing_sidecar(cfg, tmp_path):
    """sidecar 不存在时仍返回完整 384 条，高岛字段为空（fail-open 回归）。"""
    broken = AStockConfig(
        bagua_json=cfg.bagua_json, bagua_gaodao_json=tmp_path / "absent.json"
    )
    data = gua_svc.list_states(broken, page_size=384)
    assert data["total"] == 384
    assert len(data["items"]) == 384
    assert data["gaodao_coverage"] is None
    assert all(it["gaodao_commerce"] == "" for it in data["items"])
    hexes = gua_svc.list_hexagrams(broken)
    assert len(hexes) == 64
    assert hexes[0]["lines"][0]["gaodao_commerce"] == ""
