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


def test_list_states_all_five_missing_states_blank_at_catalog(cfg):
    """原书无占断的全部 5 个爻在目录消费点均优雅留空，且 market_judgement 兜底仍在。

    既有用例只覆盖 11-4；任务要求 5 个 state_id（11-4/26-2/33-4/47-4/61-5）
    在各消费点都验证。list_states 覆盖全部 384 爻，是校验这 5 个缺口的
    最直接消费入口（无需构造 OHLC 反查）。
    """
    expected_missing = {"11-4", "26-2", "33-4", "47-4", "61-5"}
    data = gua_svc.list_states(cfg, page_size=384)
    assert data["total"] == 384
    for sid in expected_missing:
        it = _find(data["items"], sid)
        assert it["gaodao_commerce"] == "", sid
        assert it["gaodao_category"] == "", sid
        # 高岛留空但原有行情简判必须兜底，避免整段空白
        assert it["market_judgement"], sid
    # 覆盖度上报的 missing 清单须与这 5 个爻精确一致
    assert set(data["gaodao_coverage"]["missing_state_ids"]) == expected_missing


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


def test_list_states_exposes_fallback_flag(cfg):
    """兜底标志由后端判定并透出，前端据此标注出处（不再硬编码类别名）。"""
    data = gua_svc.list_states(cfg, page_size=384)
    # 营商类：非兜底
    assert _find(data["items"], "01-1")["gaodao_is_fallback"] is False
    # 实际 sidecar 的 4 个兜底爻
    for sid in ("03-4", "05-2", "09-4", "36-3"):
        assert _find(data["items"], sid)["gaodao_is_fallback"] is True, sid
    # 原书无占断的 5 爻：无断语，不算兜底
    for sid in ("11-4", "26-2", "33-4", "47-4", "61-5"):
        assert _find(data["items"], sid)["gaodao_is_fallback"] is False, sid
    # 标志与类别后缀必须自洽：置位的爻其展示串应带类别括注
    for it in data["items"]:
        if it["gaodao_is_fallback"]:
            assert it["gaodao_commerce"].endswith("（" + it["gaodao_category"] + "）")


def test_list_hexagrams_lines_expose_fallback_flag(cfg):
    items = gua_svc.list_hexagrams(cfg)
    for h in items:
        for ln in h["lines"]:
            # 每爻都必须带该键，前端可无条件读取
            assert "gaodao_is_fallback" in ln
            assert isinstance(ln["gaodao_is_fallback"], bool)
    assert items[0]["lines"][0]["gaodao_is_fallback"] is False  # 01-1 营商类


def test_fallback_flag_false_without_sidecar(cfg, tmp_path):
    """sidecar 缺失时标志一律 False，前端走"来源"分支不会误标兜底。"""
    broken = AStockConfig(
        bagua_json=cfg.bagua_json, bagua_gaodao_json=tmp_path / "absent.json"
    )
    data = gua_svc.list_states(broken, page_size=384)
    assert all(it["gaodao_is_fallback"] is False for it in data["items"])


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
