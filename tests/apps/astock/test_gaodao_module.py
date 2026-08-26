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


def test_fail_open_when_by_state_id_wrong_type(tmp_path):
    """by_state_id 类型错误（非 dict）必须被 _load_raw 拒绝，降级为 None（fail-open）。

    任务显式要求覆盖此类型错误场景：sidecar 顶层是 dict，
    但 by_state_id 被写成 list / str / null 时，模块不得把它当成映射来查询，
    否则 GaodaoIndex.__init__ 里 data.get("by_state_id") 会拿到非法对象。
    """
    for bad in (
        json.dumps({"by_state_id": ["01-1", "01-2"]}),          # list
        json.dumps({"by_state_id": "not-a-dict"}),               # str
        json.dumps({"by_state_id": None, "counts": {}}),          # null
        json.dumps({"by_state_id": 123}),                          # int
    ):
        p = tmp_path / "bad_type.json"
        p.write_text(bad, encoding="utf-8")
        # utime 推进，规避秒级 mtime 分辨率导致的缓存串扰
        import os as _os
        import time as _time
        _os.utime(p, (_time.time() + 1, _time.time() + 1))
        gd.invalidate_gaodao_cache()
        cfg = AStockConfig(bagua_gaodao_json=p)
        assert gd.load_gaodao(cfg) is None
        # 消费入口一律 fail-open：取断语为 None、展示串为空、覆盖度为 None
        assert gd.gaodao_for_state("01-1", cfg) is None
        assert gd.gaodao_display("01-1", cfg) == ""
        assert gd.gaodao_coverage(cfg) is None
        # 索引对象也应为空（bool False），列表类接口据此逐爻留空
        assert not gd.gaodao_index(cfg)


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


# ---------------------------------------------------------------------------
# is_fallback：兜底类别的权威判定（供前端替代硬编码类别名）
# ---------------------------------------------------------------------------


def test_is_fallback_flag(tmp_path):
    p = tmp_path / "side.json"
    _write_sidecar(
        p,
        {
            "01-1": {"text": "甲", "category": "营商", "gua_name": "乾为天", "yao_name": "初九"},
            "05-2": {"text": "乙", "category": "时运", "gua_name": "水天需", "yao_name": "九二"},
        },
    )
    cfg = AStockConfig(bagua_gaodao_json=p)
    assert gd.gaodao_is_fallback("01-1", cfg) is False   # 营商类
    assert gd.gaodao_is_fallback("05-2", cfg) is True    # 时运兜底
    assert gd.gaodao_is_fallback("99-9", cfg) is False   # 无断语的爻不算兜底
    assert gd.gaodao_is_fallback(None, cfg) is False


def test_is_fallback_on_real_sidecar():
    cfg = get_default_config()
    if not Path(cfg.bagua_gaodao_json).exists():
        pytest.skip("bagua_gaodao.json missing")
    # 营商/商业类不是兜底
    assert gd.gaodao_is_fallback("01-1", cfg) is False
    # 实际 sidecar 中的 4 个兜底爻（时运 3 + 功名 1）
    for sid in ("03-4", "05-2", "09-4", "36-3"):
        assert gd.gaodao_is_fallback(sid, cfg) is True, sid
    # 原书无占断的 5 爻不算兜底
    for sid in ("11-4", "26-2", "33-4", "47-4", "61-5"):
        assert gd.gaodao_is_fallback(sid, cfg) is False, sid


def test_is_fallback_fail_open_without_sidecar(tmp_path):
    cfg = AStockConfig(bagua_gaodao_json=tmp_path / "nope.json")
    assert gd.gaodao_is_fallback("05-2", cfg) is False


# ---------------------------------------------------------------------------
# coverage_label：写入导出 meta 的人读串，counts 不完整时必须优雅降级
# ---------------------------------------------------------------------------


def _label_for(tmp_path, name, payload) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 逐个用例换文件名 + 推进 mtime，避免秒级 mtime 分辨率造成缓存串扰
    future = time.time() + 2
    os.utime(p, (future, future))
    gd.invalidate_gaodao_cache()
    return gd.coverage_label(AStockConfig(bagua_gaodao_json=p))


BASE_ENTRY = {"by_state_id": {"01-1": {"text": "x", "category": "营商"}}}


def test_coverage_label_complete_counts():
    cfg = get_default_config()
    if not Path(cfg.bagua_gaodao_json).exists():
        pytest.skip("bagua_gaodao.json missing")
    label = gd.coverage_label(cfg)
    assert label == "379/384（营商 375 + 兜底 4，缺失 5）"


def test_coverage_label_never_prints_none(tmp_path):
    """counts 缺项时不得把 None 格式化进给人看的表格（回归 review 发现的缺陷）。

    修复前模板直接 .format(**{k: cov.get(k)})，counts 不完整会产出
    "1/None（营商 None + 兜底 None，缺失 None）" 写进导出 Excel 的 meta sheet。
    """
    cases = {
        "only_total.json": {**BASE_ENTRY, "counts": {"total": 1}},
        "empty_counts.json": {**BASE_ENTRY, "counts": {}},
        "no_counts_key.json": dict(BASE_ENTRY),
        "no_detail.json": {**BASE_ENTRY, "counts": {"total": 1, "state_total": 384}},
        "partial_detail.json": {
            **BASE_ENTRY,
            "counts": {"total": 1, "state_total": 384, "primary": 1},
        },
    }
    for name, payload in cases.items():
        label = _label_for(tmp_path, name, payload)
        assert "None" not in label, (name, label)
        assert label.strip(), name


def test_coverage_label_degradation_texts(tmp_path):
    assert _label_for(
        tmp_path, "d1.json", {**BASE_ENTRY, "counts": {"total": 1, "state_total": 384}}
    ) == "1/384（sidecar 未记录明细）"
    assert _label_for(
        tmp_path, "d2.json", {**BASE_ENTRY, "counts": {}}
    ) == "sidecar 未记录 counts，覆盖度未知"
    assert _label_for(
        tmp_path, "d3.json", {**BASE_ENTRY, "counts": {"total": 1}}
    ) == "1/-（sidecar 未记录明细）"


def test_coverage_label_without_sidecar(tmp_path):
    gd.invalidate_gaodao_cache()
    cfg = AStockConfig(bagua_gaodao_json=tmp_path / "absent.json")
    assert gd.coverage_label(cfg) == "sidecar 缺失，高岛列为空"


# ---------------------------------------------------------------------------
# 记忆化 resolve 不得破坏 mtime 失效语义（性能修复的回归护栏）
# ---------------------------------------------------------------------------


def test_canonical_memoization_preserves_invalidation(tmp_path):
    """_canonical 缓存路径解析后，改文件内容仍要能被读到新值。

    resolve() 在 Windows 约 270µs，占单次取用开销 96%，全市场导出上万次调用
    会白耗约 2.3s，故按路径字符串记忆化；但 mtime 仍每次 stat，
    失效语义必须保持不变。
    """
    p = tmp_path / "memo.json"
    _write_sidecar(p, {"01-1": {"text": "v1", "category": "营商"}})
    cfg = AStockConfig(bagua_gaodao_json=p)
    assert gd.gaodao_for_state("01-1", cfg)["text"] == "v1"

    _write_sidecar(p, {"01-1": {"text": "v2", "category": "营商"}})
    future = time.time() + 3
    os.utime(p, (future, future))
    # 未显式清缓存：仅靠 mtime 变化就应读到新内容
    assert gd.gaodao_for_state("01-1", cfg)["text"] == "v2"
