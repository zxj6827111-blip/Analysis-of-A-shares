# -*- coding: utf-8 -*-
"""卦象操作信号 × 《高岛易断》营商断语 的「共识倾向」判定。

用途：在导出表里标出两套解读**一致**的行——都看好标红、都看差标绿，
分歧或语气不明一律不标。仅作阅读辅助，不参与选股与回测。

为什么要求两者一致
------------------
两套解读同源于 384 爻骨架，但解读对象不同：
  - ``action_signal``（新开仓/加仓/持有/减仓/清仓）是现代股市仓位解读，
    语境是二级市场买卖股票；
  - 高岛「问营商」是 1901 年实体商业占断，语境是货物贩运、囤积、店基。
实测两者在全市场导出中约有 13% 的行明确对立（如「卦象抄底 vs 高岛买入者必多剥耗」），
因此不能把两者合并成单一结论，只在一致时给出提示，分歧交回人工判断。

高岛语气判定的已知局限（重要）
------------------------------
高岛断语是文言自由文本，**没有结构化好坏标签**，这里用关键词法推断：
含决定性正面词且无负面词 → 好；反之 → 差；两者皆有（文言转折结构，
如「度其贸易必是危地，须日夜防备，可脱险而获利也」）→ 不明，不归类。
实测约 26% 的断语落入「不明」，且已归类的部分仍可能误判
（如「利行商，不利坐贾」中的「不利」只针对坐商）。
故本模块结论方向可信、精度有限，词表可直接在下方调整。
"""
from __future__ import annotations

from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 卦象侧：操作信号 → 立场（人工策展的结构化字段，覆盖 384/384，无歧义）
# ---------------------------------------------------------------------------
GUA_BULLISH = ("新开仓", "加仓")
GUA_BEARISH = ("减仓", "清仓")
# "持有" 视为中性，不参与共识判定

# ---------------------------------------------------------------------------
# 高岛侧：关键词词表（可调）
# 只收「结论性」词汇，避免中性描述词混入造成误判
# ---------------------------------------------------------------------------
GAODAO_POSITIVE = (
    "大利", "获利", "得利", "得大利", "获厚利", "必得利", "有利",
    "大吉", "皆吉", "终吉", "无不利", "无不获利",
)
GAODAO_NEGATIVE = (
    "凶", "损失", "亏", "不利", "勿动", "未可", "宜守", "不可",
    "戒", "防", "败", "穷矣", "耗", "危", "剥耗", "失机",
)

# 对外的三个标签 + 空值
SIDE_GOOD = "好"
SIDE_BAD = "差"
SIDE_NEUTRAL = "中"
SIDE_UNKNOWN = "不明"

CONSENSUS_GOOD = "▲双好"
CONSENSUS_BAD = "▼双差"
CONSENSUS_CONFLICT = "分歧"
CONSENSUS_NONE = ""

# Excel 着色（A股习惯：红涨绿跌）。字体色 + 浅底色，同时保留 ▲▼ 符号，
# 避免信息只靠颜色传递（红绿对色弱用户不友好、打印黑白也会丢失）。
# 用显式 8 位 ARGB：openpyxl 对 6 位值会补成 "00xxxxxx"，写死 FF 前缀避免歧义。
COLOR_GOOD_FONT = "FFC00000"   # 深红
COLOR_GOOD_FILL = "FFFFE8E8"   # 浅红
COLOR_BAD_FONT = "FF0F7B0F"    # 深绿
COLOR_BAD_FILL = "FFE6F4E6"    # 浅绿


def gua_side(action_signal: Optional[str]) -> str:
    """操作信号 → 卦象立场。空信号/持有 均为中性。"""
    s = str(action_signal or "").strip()
    if s in GUA_BULLISH:
        return SIDE_GOOD
    if s in GUA_BEARISH:
        return SIDE_BAD
    return SIDE_NEUTRAL


def gaodao_side(text: Optional[str]) -> str:
    """高岛断语 → 语气。褒贬词同时出现（文言转折）时返回「不明」，不强判。"""
    t = str(text or "")
    if not t.strip():
        return SIDE_UNKNOWN
    pos = any(w in t for w in GAODAO_POSITIVE)
    neg = any(w in t for w in GAODAO_NEGATIVE)
    if pos and not neg:
        return SIDE_GOOD
    if neg and not pos:
        return SIDE_BAD
    return SIDE_UNKNOWN


def consensus(
    action_signal: Optional[str], gaodao_text: Optional[str]
) -> str:
    """两套解读的共识标签。

    只有**方向一致**才给出 双好/双差；一方中性或语气不明返回空串；
    方向对立返回「分歧」（提示该爻两套体系打架，值得人工看一眼）。
    """
    g = gua_side(action_signal)
    h = gaodao_side(gaodao_text)
    if g == SIDE_GOOD and h == SIDE_GOOD:
        return CONSENSUS_GOOD
    if g == SIDE_BAD and h == SIDE_BAD:
        return CONSENSUS_BAD
    if (g == SIDE_GOOD and h == SIDE_BAD) or (g == SIDE_BAD and h == SIDE_GOOD):
        return CONSENSUS_CONFLICT
    return CONSENSUS_NONE


def consensus_style(label: str) -> Tuple[Optional[str], Optional[str]]:
    """标签 → (字体色, 填充色)；无需着色返回 (None, None)。"""
    if label == CONSENSUS_GOOD:
        return COLOR_GOOD_FONT, COLOR_GOOD_FILL
    if label == CONSENSUS_BAD:
        return COLOR_BAD_FONT, COLOR_BAD_FILL
    return None, None
