# -*- coding: utf-8 -*-
"""卦象操作信号 × 高岛断语 的共识倾向判定与导出着色。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.bagua import consensus as cs

BAGUA_DIR = (
    Path(__file__).resolve().parents[3] / "wtpy" / "apps" / "astock" / "bagua"
)
KB_PATH = BAGUA_DIR / "bagua_384.json"
SIDECAR_PATH = BAGUA_DIR / "bagua_gaodao.json"


# ---------------------------------------------------------------------------
# 卦象侧：操作信号 → 立场
# ---------------------------------------------------------------------------


def test_gua_side_mapping():
    assert cs.gua_side("新开仓") == cs.SIDE_GOOD
    assert cs.gua_side("加仓") == cs.SIDE_GOOD
    assert cs.gua_side("减仓") == cs.SIDE_BAD
    assert cs.gua_side("清仓") == cs.SIDE_BAD
    # 持有 / 空值 / 未知信号一律中性，不参与共识
    assert cs.gua_side("持有") == cs.SIDE_NEUTRAL
    assert cs.gua_side("") == cs.SIDE_NEUTRAL
    assert cs.gua_side(None) == cs.SIDE_NEUTRAL
    assert cs.gua_side("观望") == cs.SIDE_NEUTRAL


# ---------------------------------------------------------------------------
# 高岛侧：文言语气
# ---------------------------------------------------------------------------


def test_gaodao_side_clear_cases():
    assert cs.gaodao_side("货物合宜，不必减价，无不获利。") == cs.SIDE_GOOD
    assert cs.gaodao_side("凡有所谋，必无所终也。凶。") == cs.SIDE_BAD


def test_gaodao_side_ambiguous_not_forced():
    """文言转折句（褒贬词并存）必须判为不明，不能强行归类。

    这是本模块最容易出错的地方：若强判，"须日夜防备，可脱险而获利也"
    会被当成纯正面，而其真实语气是"有风险、谨慎方可获利"。
    """
    turn = "度其贸易必是危地，须日夜防备，可脱险而获利也。"
    assert cs.gaodao_side(turn) == cs.SIDE_UNKNOWN
    # 空文本（原书无该爻占断的 5 爻）同样不明
    assert cs.gaodao_side("") == cs.SIDE_UNKNOWN
    assert cs.gaodao_side(None) == cs.SIDE_UNKNOWN


# ---------------------------------------------------------------------------
# 共识：只在方向一致时给结论
# ---------------------------------------------------------------------------


def test_consensus_double_good_and_bad():
    assert cs.consensus("加仓", "货物合宜，无不获利。") == cs.CONSENSUS_GOOD
    assert cs.consensus("新开仓", "得财得利大吉。") == cs.CONSENSUS_GOOD
    assert cs.consensus("清仓", "凡有所谋，必无所终也。凶。") == cs.CONSENSUS_BAD
    assert cs.consensus("减仓", "销路不得其当，可免耗失。") == cs.CONSENSUS_BAD


def test_consensus_conflict_is_flagged_not_hidden():
    """方向对立要显式标出「分歧」，不能静默当作无结论。

    实测全市场导出约 13% 的行属此类（如卦象抄底 vs 高岛"买入者必多剥耗"），
    这类分歧本身是信息，应交回人工判断。
    """
    assert cs.consensus("加仓", "凡有所谋，必无所终也。凶。") == cs.CONSENSUS_CONFLICT
    assert cs.consensus("清仓", "往返经营，俱得大利。") == cs.CONSENSUS_CONFLICT


def test_consensus_neutral_or_unknown_gives_blank():
    # 卦象中性
    assert cs.consensus("持有", "货物合宜，无不获利。") == cs.CONSENSUS_NONE
    # 高岛语气不明
    assert cs.consensus("加仓", "度其贸易必是危地，须日夜防备，可脱险而获利也。") == cs.CONSENSUS_NONE
    # 高岛无断语（原书无占断的 5 爻）
    assert cs.consensus("清仓", "") == cs.CONSENSUS_NONE
    # 两侧都缺
    assert cs.consensus(None, None) == cs.CONSENSUS_NONE


def test_consensus_labels_carry_symbol_not_color_only():
    """标签自带 ▲▼ 符号：颜色对色弱用户/黑白打印会丢失，信息不能只靠颜色。"""
    assert "▲" in cs.CONSENSUS_GOOD
    assert "▼" in cs.CONSENSUS_BAD


def test_consensus_style_only_colors_agreement():
    good_font, good_fill = cs.consensus_style(cs.CONSENSUS_GOOD)
    bad_font, bad_fill = cs.consensus_style(cs.CONSENSUS_BAD)
    assert good_font and good_fill
    assert bad_font and bad_fill
    assert good_font != bad_font
    # 分歧与空值不着色
    assert cs.consensus_style(cs.CONSENSUS_CONFLICT) == (None, None)
    assert cs.consensus_style(cs.CONSENSUS_NONE) == (None, None)
    assert cs.consensus_style("任意未知标签") == (None, None)


# ---------------------------------------------------------------------------
# 真实数据：384 爻 × sidecar 上的分布必须落在合理区间
# ---------------------------------------------------------------------------


@pytest.fixture
def real_data():
    if not KB_PATH.exists() or not SIDECAR_PATH.exists():
        pytest.skip("bagua data missing")
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    gd = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))["by_state_id"]
    return kb["entries"], gd


def test_real_distribution_is_sane(real_data):
    """双好/双差/分歧三类都必须真实存在，且有结论的标签不能占绝对多数。

    若某类为 0，说明词表或映射失效；若「有结论」的类别占比过半，
    说明判定过于激进（本功能的定位是只在两套解读一致时才提示，
    大部分爻应当留空——持有信号 140 爻 + 高岛语气不明的爻天然落入留空）。
    """
    entries, gd = real_data
    counts = {cs.CONSENSUS_GOOD: 0, cs.CONSENSUS_BAD: 0,
              cs.CONSENSUS_CONFLICT: 0, cs.CONSENSUS_NONE: 0}
    for e in entries:
        sid = e.get("state_id")
        text = (gd.get(sid) or {}).get("text", "")
        counts[cs.consensus(e.get("action_signal"), text)] += 1

    total = len(entries)
    assert total == 384
    assert counts[cs.CONSENSUS_GOOD] > 0, "双好不应为 0"
    assert counts[cs.CONSENSUS_BAD] > 0, "双差不应为 0"
    assert counts[cs.CONSENSUS_CONFLICT] > 0, "分歧不应为 0（两套体系确实会打架）"
    # 只约束「有结论」的标签，留空本就应占多数
    decisive = (
        counts[cs.CONSENSUS_GOOD]
        + counts[cs.CONSENSUS_BAD]
        + counts[cs.CONSENSUS_CONFLICT]
    )
    assert decisive < total * 0.6, f"有结论标签占比过高 {decisive}/{total}，判定过于激进"
    for label in (cs.CONSENSUS_GOOD, cs.CONSENSUS_BAD, cs.CONSENSUS_CONFLICT):
        assert counts[label] < total * 0.4, f"{label} 单类占比过高 {counts[label]}/{total}"


def test_real_missing_states_never_marked(real_data):
    """原书无占断的 5 爻永远不着色（无高岛依据，不能单凭卦象下结论）。"""
    entries, gd = real_data
    by_sid = {e.get("state_id"): e for e in entries}
    for sid in ("11-4", "26-2", "33-4", "47-4", "61-5"):
        e = by_sid[sid]
        text = (gd.get(sid) or {}).get("text", "")
        assert text == "", sid
        assert cs.consensus(e.get("action_signal"), text) == cs.CONSENSUS_NONE, sid


def test_real_hold_signal_never_marked(real_data):
    """持有信号的爻不着色，无论高岛怎么说（卦象未表态就不算共识）。"""
    entries, gd = real_data
    checked = 0
    for e in entries:
        if str(e.get("action_signal") or "") != "持有":
            continue
        text = (gd.get(e.get("state_id")) or {}).get("text", "")
        assert cs.consensus("持有", text) == cs.CONSENSUS_NONE
        checked += 1
    assert checked > 0
