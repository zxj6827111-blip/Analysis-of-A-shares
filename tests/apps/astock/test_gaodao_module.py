# -*- coding: utf-8 -*-
"""高岛易断 sidecar 读取模块测试（Task 2）。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from wtpy.apps.astock.bagua import gaodao as gd
from wtpy.apps.astock.config import AStockConfig, get_default_config


@pytest.fixture(autouse=True)
def _clear_cache():
    gd.invalidate_gaodao_cache()
    yield
    gd.invalidate_gaodao_cache()


def _write_sidecar(path: Path, by_state_id: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_file": "高岛易断_全文.txt",
                "source_sha256": "0" * 64,
                "generated_at": "2026-01-01T00:00:00+08:00",
                "extractor_version": "test",
                "policy": {"primary": ["营商", "商业"], "fallback": ["时运", "功名"]},
                "counts": {
                    "total": len(by_state_id),
                    "primary": len(by_state_id),
                    "fallback": 0,
                    "missing": 384 - len(by_state_id),
                    "state_total": 384,
                },
                "missing_state_ids": [],
                "by_state_id": by_state_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_default_config_points_to_packaged_sidecar():
    cfg = get_default_config()
    assert cfg.bagua_gaodao_json is not None
    assert Path(cfg.bagua_gaodao_json).name == "bagua_gaodao.json"
    # 与 bagua_384.json 同目录，便于一起备份/迁移
    assert Path(cfg.bagua_gaodao_json).parent == Path(cfg.bagua_json).parent
    assert gd.gaodao_path(cfg) == Path(cfg.bagua_gaodao_json)


def test_real_sidecar_hit_and_miss():
    cfg = get_default_config()
    if not Path(cfg.bagua_gaodao_json).exists():
        pytest.skip("bagua_gaodao.json missing")
    hit = gd.gaodao_for_state("01-1", cfg)
    assert hit and "潜" in hit["text"]
    assert hit["category"] == "营商"
    assert gd.gaodao_display("01-1", cfg) == hit["text"]  # 营商类不加后缀
    # 11-4 是原书无占断的 5 爻之一
    assert gd.gaodao_for_state("11-4", cfg) is None
    assert gd.gaodao_display("11-4", cfg) == ""
    # 非法/空 state_id 不抛异常
    assert gd.gaodao_for_state(None, cfg) is None
    assert gd.gaodao_for_state("99-9", cfg) is None


def test_real_sidecar_coverage_summary():
    cfg = get_default_config()
    if not Path(cfg.bagua_gaodao_json).exists():
        pytest.skip("bagua_gaodao.json missing")
    cov = gd.gaodao_coverage(cfg)
    assert cov["total"] == 379
    assert cov["missing"] == 5
    assert cov["state_total"] == 384
    assert len(cov["missing_state_ids"]) == 5


def test_fail_open_when_file_missing(tmp_path):
    cfg = AStockConfig(bagua_gaodao_json=tmp_path / "nope.json")
    assert gd.load_gaodao(cfg) is None
    assert gd.gaodao_for_state("01-1", cfg) is None
    assert gd.gaodao_display("01-1", cfg) == ""
    assert gd.gaodao_coverage(cfg) is None
    # sidecar 不可用时类别判定保守返回 True（不给展示串加后缀）
    assert gd.is_primary_category("时运", cfg) is True


def test_fail_open_when_file_corrupted(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    cfg = AStockConfig(bagua_gaodao_json=p)
    assert gd.load_gaodao(cfg) is None
    assert gd.gaodao_for_state("01-1", cfg) is None

    # 结构不对（缺 by_state_id）同样降级
    p2 = tmp_path / "wrong_shape.json"
    p2.write_text(json.dumps({"counts": {}}), encoding="utf-8")
    cfg2 = AStockConfig(bagua_gaodao_json=p2)
    assert gd.load_gaodao(cfg2) is None


def test_fallback_category_gets_suffix(tmp_path):
    p = tmp_path / "side.json"
    _write_sidecar(
        p,
        {
            "01-1": {"text": "甲", "category": "营商", "gua_name": "乾为天", "yao_name": "初九"},
            "05-2": {"text": "乙", "category": "时运", "gua_name": "水天需", "yao_name": "九二"},
        },
    )
    cfg = AStockConfig(bagua_gaodao_json=p)
    assert gd.gaodao_display("01-1", cfg) == "甲"
    assert gd.gaodao_display("05-2", cfg) == "乙（时运）"
    assert gd.is_primary_category("营商", cfg) is True
    assert gd.is_primary_category("时运", cfg) is False


def test_cache_invalidates_on_mtime_change(tmp_path):
    p = tmp_path / "side.json"
    _write_sidecar(
        p, {"01-1": {"text": "旧", "category": "营商", "gua_name": "乾为天", "yao_name": "初九"}}
    )
    cfg = AStockConfig(bagua_gaodao_json=p)
    assert gd.gaodao_for_state("01-1", cfg)["text"] == "旧"

    _write_sidecar(
        p, {"01-1": {"text": "新", "category": "营商", "gua_name": "乾为天", "yao_name": "初九"}}
    )
    # 文件系统 mtime 分辨率可能是秒级，显式推进 mtime 保证缓存键变化
    future = time.time() + 2
    os.utime(p, (future, future))
    assert gd.gaodao_for_state("01-1", cfg)["text"] == "新"
