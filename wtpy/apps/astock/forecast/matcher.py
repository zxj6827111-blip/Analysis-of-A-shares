"""Match weekly 变卦 (digit + gua name) to forecast knowledge base judgements.

User rule (authoritative):
  Weekly field like ``0-山泽损`` / ``1-山雷颐``:
  - **Only the 变卦 name** is used to locate the 64-gua block in the 简判 table
    (NOT 本卦).
  - Prefix digit selects the line **inside that 变卦**:
      1→初爻 … 5→五爻，**0→上爻（第6爻）**.
  - Example: ``0-山泽损`` → 山泽损 · 上九 → 「不减反增无咎大吉，获利丰厚」.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .kb_loader import ForecastKnowledgeBase
from .name_norm import (
    normalize_gua_name,
    parse_bian_field,
    yao_index_to_order,
)


@dataclass
class GuaMatch:
    period: str  # week | month
    raw_ben: str = ""  # 本卦（仅展示，不参与简判勾稽）
    raw_bian: str = ""  # 变卦原文，如 0-山泽损
    yao_index_raw: Optional[int] = None
    yao_order: Optional[int] = None
    ben_norm: str = ""
    bian_norm: str = ""  # 变卦卦名，简判定位主键
    match_status: str = "empty_input"  # ok | miss | empty_input
    tips: List[str] = field(default_factory=list)
    yao_name: str = ""
    biangua_kb: str = ""  # KB 中该爻的「变卦」列（动爻所变，仅参考）
    judgement: str = ""
    core_gang: str = ""
    gua_ci: str = ""
    yao_ci: str = ""
    note: str = ""
    operation_signal: str = ""
    full_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def match_period(
    kb: Optional[ForecastKnowledgeBase],
    *,
    period: str,
    ben_raw: Optional[str],
    bian_raw: Optional[str],
    yao_index_base: int = 0,
) -> GuaMatch:
    """
    Match using **变卦 only**.

    ``ben_raw`` is retained for display / 本卦展示，不参与简判查找。
    ``yao_index_base`` ignored for 0..5 circular rule (kept for API compatibility).
    """
    del yao_index_base  # not used; circular 0→6 is fixed
    ben = normalize_gua_name(ben_raw)
    idx, bian_name = parse_bian_field(bian_raw)
    m = GuaMatch(
        period=period,
        raw_ben=str(ben_raw or "") if ben_raw is not None else "",
        raw_bian=str(bian_raw or "") if bian_raw is not None else "",
        yao_index_raw=idx,
        ben_norm=ben,
        bian_norm=bian_name,
    )
    if not bian_name and idx is None:
        m.match_status = "empty_input"
        m.tips.append("周报中无变卦，无法匹配简判")
        return m

    yao_order = yao_index_to_order(idx)
    m.yao_order = yao_order

    if kb is None:
        m.match_status = "miss"
        m.tips.append("简判库未加载")
        return m

    if not bian_name or yao_order is None:
        m.match_status = "miss"
        m.tips.append("未命中简判库，请检查变卦（卦名或爻位数字缺失/非法）")
        return m

    # 只看变卦：在简判表中打开「变卦」对应的那一卦，再按数字取第 N 爻
    entry = kb.lookup_ben_yao(bian_name, yao_order)
    if not entry:
        m.match_status = "miss"
        m.tips.append(
            f"未命中简判库，请检查变卦：{bian_name} 第{yao_order}爻（前缀{idx}）"
        )
        return m

    m.yao_name = str(entry.get("yao_name") or "")
    m.biangua_kb = str(entry.get("biangua") or "")
    m.judgement = str(entry.get("market_judgement") or "")
    m.core_gang = str(entry.get("core_gang") or "")
    m.gua_ci = str(entry.get("gua_ci") or "")
    m.yao_ci = str(entry.get("yao_ci") or "")
    m.note = str(entry.get("note") or "")
    m.operation_signal = str(entry.get("operation_signal") or "")
    m.full_name = str(entry.get("full_name") or entry.get("gua_name") or "")
    m.match_status = "ok"
    if not m.judgement:
        m.tips.append("命中爻位但简判文案为空")
    return m
