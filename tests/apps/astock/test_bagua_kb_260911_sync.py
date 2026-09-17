# -*- coding: utf-8 -*-
"""260911 稿（10 列 Excel）同步校验：简判改写、备注新增、操作信号扩表。

背景：20260911 稿相对 20260719 稿有 4 类差异——7 条「个股行情简判」重写、
2 条「备注&实操总结」新增、5 条「操作信号」调整（新增 观察/不碰/持有或开仓
三个取值）、变卦列 384/384 填全（新增「变卦全称」列）。

这里锁住的就是这几类差异本身，防止后续重建时被静默回退；同时校验
「变卦全称」与动爻推算法 384/384 一致——这是采信 Excel 的前提。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.bagua import consensus as cs
from wtpy.apps.astock.bagua.filter_rules import KNOWN_ACTION_SIGNALS
from wtpy.apps.astock.bagua.rebuild_from_excel import (
    ACTION_SIGNAL_VOCAB,
    changed_hexagram_ul,
    default_excel_path,
    rebuild_knowledge_from_excel,
    short_gua_name,
    validate_knowledge,
)

ROOT = Path(__file__).resolve().parents[3]
BAGUA_DIR = ROOT / "wtpy" / "apps" / "astock" / "bagua"
KB_PATH = BAGUA_DIR / "bagua_384.json"
REPORT_PATH = BAGUA_DIR / "gua_validation_report.json"

# 本稿新增/改写的条目（state_id 取自 卦序-爻位）
EXPECTED_SIGNAL_CHANGES = {
    "47-6": "不碰",        # ䷮泽水困 上六
    "54-1": "观察",        # ䷵雷泽归妹 初九
    "54-2": "观察",        # ䷵雷泽归妹 九二
    "54-5": "观察",        # ䷵雷泽归妹 六五
    "64-5": "持有或开仓",  # ䷿火水未济 六五
}
EXPECTED_JUDGEMENT_CHANGES = ("07-2", "34-2", "36-3", "47-2", "52-5", "56-2", "56-5")
EXPECTED_NEW_NOTES = {
    "47-6": "进退都不易，曾在金螳螂失",
    "64-5": "哈药验证可以",
}
# 260911 稿之前的旧文本，出现即说明数据被回退
STALE_JUDGEMENTS = (
    "主升中段持仓待涨，利润丰厚",
    "坚守仓位吉利",
    "南方寻机抓住龙头，不可急进",
    "富贵中被困利好将至，进攻凶但无咎",
    "管住嘴巴说话有序，懊悔消散",
    "旅途中住店带足钱财，得可靠帮手吉利",
    "射野鸡丢一箭，最终获荣誉",
)


@pytest.fixture(scope="module")
def kb() -> dict:
    return json.loads(KB_PATH.read_text(encoding="utf-8"))


def _by_state(kb: dict) -> dict:
    return {e["state_id"]: e for e in kb["entries"]}


# ---------------------------------------------------------------------------
# 内容差异：本稿到底改了什么
# ---------------------------------------------------------------------------


def test_new_signal_values_present(kb):
    by_state = _by_state(kb)
    for sid, sig in EXPECTED_SIGNAL_CHANGES.items():
        assert by_state[sid]["action_signal"] == sig, f"{sid} 信号未按 260911 稿更新"


def test_judgement_rewritten_and_old_text_gone(kb):
    entries = kb["entries"]
    for sid in EXPECTED_JUDGEMENT_CHANGES:
        e = next(x for x in entries if x["state_id"] == sid)
        # 重写后的简判明显更长（旧版多为一句 8~14 字的概括）
        assert len(e["market_judgement"]) >= 20, f"{sid} 简判似乎仍是旧版短句"
        assert e["market_summary"] == e["market_judgement"], f"{sid} summary/judgement 未同步"
    blob = json.dumps(entries, ensure_ascii=False)
    for stale in STALE_JUDGEMENTS:
        assert stale not in blob, f"旧版简判文本仍存在：{stale}"


def test_new_notes_added(kb):
    by_state = _by_state(kb)
    for sid, note in EXPECTED_NEW_NOTES.items():
        assert by_state[sid]["note"] == note
    # 13 = 旧稿已有 11 条 + 本稿新增 2 条；后续再补备注时同步更新此数
    assert sum(1 for e in kb["entries"] if e.get("note")) == 13


def test_action_signal_counts_updated(kb):
    assert kb["action_signal_counts"] == {
        "持有": 137,
        "新开仓": 75,
        "加仓": 14,
        "清仓": 88,
        "减仓": 65,
        "不碰": 1,
        "观察": 3,
        "持有或开仓": 1,
    }


# ---------------------------------------------------------------------------
# 变卦：Excel 与动爻推算必须一致（本稿变卦列 384/384 填全）
# ---------------------------------------------------------------------------


def test_every_entry_has_biangua_full_name(kb):
    assert all(e.get("biangua_full_name") for e in kb["entries"])


def test_biangua_matches_line_flip_algorithm(kb):
    """独立重算 384 爻变卦，须与 KB 中的 changed_hexagram_id 完全一致。"""
    ul_to_order = {}
    for e in kb["entries"]:
        ul_to_order.setdefault((e["upper"], e["lower"]), e["gua_order"])
    name_to_order = {}
    for e in kb["entries"]:
        name_to_order.setdefault(e["gua_name"], e["gua_order"])
    mismatches = []
    for e in kb["entries"]:
        nu, nl = changed_hexagram_ul(e["upper"], e["lower"], e["yao_order"])
        expect = ul_to_order.get((nu, nl))
        if expect != e["changed_hexagram_id"]:
            mismatches.append((e["state_id"], e["biangua"], e["changed_hexagram_id"], expect))
        # 简写名须等于全称名的截短形式
        assert e["biangua"] == short_gua_name(e["biangua_full_name"]), e["state_id"]
        assert name_to_order[e["biangua_full_name"]] == e["changed_hexagram_id"]
    assert mismatches == []
    assert kb["biangua_conflicts"] == []
    assert kb["biangua_fill"]["empty_left"] == 0
    assert kb["biangua_fill"]["missing_full_name"] == 0


# ---------------------------------------------------------------------------
# 词表一致性：数据/后端/前端三处不能各写一份
# ---------------------------------------------------------------------------


def test_vocab_single_source_matches_kb(kb):
    assert ACTION_SIGNAL_VOCAB == KNOWN_ACTION_SIGNALS, "词表两处顺序/内容已走样"
    used = {e["action_signal"] for e in kb["entries"]}
    assert used <= set(KNOWN_ACTION_SIGNALS), f"知识库出现词表外信号：{used - set(KNOWN_ACTION_SIGNALS)}"
    assert used == set(KNOWN_ACTION_SIGNALS), "词表里有知识库从未使用的取值，检查是否漏同步"


def test_new_signals_have_stance_and_ui_style(kb):
    """新信号的共识立场 + 前端徽标样式都要存在，否则新爻会渲染成裸文本。"""
    assert cs.gua_side("不碰") == cs.SIDE_BAD
    assert cs.gua_side("观察") == cs.SIDE_NEUTRAL
    assert cs.gua_side("持有或开仓") == cs.SIDE_NEUTRAL
    assert "不碰" in cs.GUA_BEARISH and "不碰" not in cs.GUA_BULLISH

    html = (ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html").read_text(
        encoding="utf-8"
    )
    for sig in ("不碰", "观察", "持有或开仓"):
        assert f".gua-act-{sig}" in html, f"缺少 .gua-act-{sig} 样式"
        assert f'value="{sig}"' in html, f"筛选面板缺少 {sig} 勾选项"
    # 浏览分组顺序须覆盖全部信号，否则新信号会掉到「其他」分组之后
    order_block = re.search(r"const order = \[([^\]]*)\]", html)
    assert order_block, "未找到按操作信号浏览的分组顺序定义"
    for sig in KNOWN_ACTION_SIGNALS:
        assert f'"{sig}"' in order_block.group(1), f"浏览分组顺序缺少 {sig}"


def test_new_signal_filters_by_literal_value():
    """新信号按字面参与筛选：勾「不碰」只命中该信号，不误伤相邻的「持有」。"""
    from wtpy.apps.astock.bagua.filter_rules import GuaFilter, event_matches_gua_filter

    class _Ev:
        def __init__(self, bagua):
            self.bagua = bagua

    hit = _Ev({"state_id": "47-6", "gua_order": 47, "action_signal": "不碰"})
    miss = _Ev({"state_id": "47-5", "gua_order": 47, "action_signal": "持有"})
    gf = GuaFilter(enabled=True, selection_mode="action_signal", selected_action_signals=["不碰"])
    assert event_matches_gua_filter(hit, gf)
    assert not event_matches_gua_filter(miss, gf)


# ---------------------------------------------------------------------------
# 可复现性：提交的 JSON 必须能由提交的脚本 + 归档 Excel 重新生成
# ---------------------------------------------------------------------------


def test_report_is_ok():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["ok"] is True, report["issues"]
    assert report["count_entries"] == 384 and report["count_gua"] == 64
    assert report["source_file"] == "64卦384爻行情简判完整版-260911.xlsx"


def test_kb_regenerates_identically_from_archived_excel(tmp_path):
    """指标/ 被 gitignore（CI 无此目录），本机跑；不一致说明脚本与数据脱节。"""
    try:
        xlsx = default_excel_path(ROOT)
    except FileNotFoundError:
        pytest.skip("excel not present (指标/ is gitignored)")
    sha = hashlib.sha256(xlsx.read_bytes()).hexdigest()
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    assert kb["source_sha256"] == sha, "归档 Excel 与 KB 记录的不是同一份文件"

    out = tmp_path / "bagua_regen.json"
    regenerated = rebuild_knowledge_from_excel(xlsx, out)
    assert validate_knowledge(regenerated)["ok"] is True
    committed = json.loads(KB_PATH.read_text(encoding="utf-8"))
    assert regenerated["entries"] == committed["entries"], "脚本重建结果与提交数据不一致"
    assert regenerated["source_sha256"] == committed["source_sha256"]
    assert regenerated["action_signal_counts"] == committed["action_signal_counts"]
