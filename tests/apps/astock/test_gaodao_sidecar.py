# -*- coding: utf-8 -*-
"""《高岛易断》sidecar 数据文件校验（Task 1）。

只校验已生成的 bagua_gaodao.json 与 bagua_384.json 的一致性，
不依赖原始 txt（原始书籍在开发机外挂目录，CI 上不存在）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

BAGUA_DIR = (
    Path(__file__).resolve().parents[3] / "wtpy" / "apps" / "astock" / "bagua"
)
KB_PATH = BAGUA_DIR / "bagua_384.json"
SIDECAR_PATH = BAGUA_DIR / "bagua_gaodao.json"

# 高岛原书对这 5 爻没有任何占断类别，属已知缺口（由 market_judgement 兜底）
EXPECTED_MISSING = {"11-4", "26-2", "33-4", "47-4", "61-5"}


@pytest.fixture(scope="module")
def sidecar() -> dict:
    if not SIDECAR_PATH.exists():
        pytest.skip("bagua_gaodao.json missing")
    return json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kb_state_ids() -> set:
    if not KB_PATH.exists():
        pytest.skip("bagua_384.json missing")
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return {str(e["state_id"]) for e in kb.get("entries") or []}


def test_sidecar_header_fields(sidecar):
    for key in (
        "schema_version",
        "source_file",
        "source_sha256",
        "generated_at",
        "extractor_version",
        "policy",
        "counts",
        "missing_state_ids",
        "by_state_id",
    ):
        assert key in sidecar, key
    assert sidecar["policy"]["fallback"] == ["时运", "功名"]
    assert "营商" in sidecar["policy"]["primary"]
    # sha256 是 64 位十六进制，保证与源书绑定可追溯
    assert len(sidecar["source_sha256"]) == 64


def test_sidecar_counts_are_self_consistent(sidecar):
    counts = sidecar["counts"]
    entries = sidecar["by_state_id"]
    assert counts["state_total"] == 384
    assert counts["total"] == 379
    assert counts["primary"] == 375
    assert counts["fallback"] == 4
    assert counts["missing"] == 5
    assert counts["primary"] + counts["fallback"] == counts["total"]
    assert counts["total"] + counts["missing"] == counts["state_total"]
    assert len(entries) == counts["total"] == 379

    primary_aliases = set(sidecar["policy"]["primary"])
    fallback_aliases = set(sidecar["policy"]["fallback"])
    n_primary = sum(1 for v in entries.values() if v["category"] in primary_aliases)
    n_fallback = sum(1 for v in entries.values() if v["category"] in fallback_aliases)
    assert n_primary == counts["primary"]
    assert n_fallback == counts["fallback"]
    assert n_primary + n_fallback == len(entries)


def test_sidecar_keys_subset_of_384(sidecar, kb_state_ids):
    assert len(kb_state_ids) == 384
    unknown = set(sidecar["by_state_id"]) - kb_state_ids
    assert not unknown, f"sidecar 含非法 state_id: {sorted(unknown)[:10]}"


def test_missing_state_ids_exact_and_absent(sidecar, kb_state_ids):
    missing = set(sidecar["missing_state_ids"])
    assert missing == EXPECTED_MISSING
    assert missing <= kb_state_ids
    # 缺失爻必须不在 by_state_id 内，否则统计口径自相矛盾
    assert not (missing & set(sidecar["by_state_id"]))
    # 缺失 + 命中 = 全部 384 爻
    assert missing | set(sidecar["by_state_id"]) == kb_state_ids


def test_entry_shape_and_qian_first_line(sidecar):
    for sid, item in sidecar["by_state_id"].items():
        assert set(item) == {"text", "category", "gua_name", "yao_name"}, sid
        assert item["text"].strip(), sid
        assert item["category"].strip(), sid
    # 乾·初九「潜龙勿用」的营商断语必含"潜"，作为抽取正确性的锚点
    first = sidecar["by_state_id"]["01-1"]
    assert first["category"] == "营商"
    assert first["gua_name"] == "乾为天"
    assert first["yao_name"] == "初九"
    assert "潜" in first["text"]


def test_gua_names_match_knowledge_base(sidecar):
    """sidecar 记录的卦名/爻名必须与 384 知识库逐条一致（防配对错位）。"""
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    by_sid = {str(e["state_id"]): e for e in kb.get("entries") or []}
    for sid, item in sidecar["by_state_id"].items():
        e = by_sid[sid]
        assert item["gua_name"] == e["gua_name"], sid
        assert item["yao_name"] == e["yao_name"], sid
    # 抽取阶段若出现卦名不一致会记录在此字段，正常应为空
    assert sidecar.get("gua_name_mismatch") == []
