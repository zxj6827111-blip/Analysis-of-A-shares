"""Normalize stock codes and bagua names for forecast matching."""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Strip I Ching symbols ䷀-䷿ and whitespace
_GUA_SYMBOL_RE = re.compile(r"[\u4dc0-\u4dff]")
_WS_RE = re.compile(r"\s+")

# Common short aliases seen in 简判表 biangua column vs full gua names
_BIANGUA_ALIASES = {
    "坤地": "坤为地",
    "坎水": "坎为水",
    "离火": "离为火",
    "震雷": "震为雷",
    "艮山": "艮为山",
    "兑泽": "兑为泽",
    "巽风": "巽为风",
    "乾天": "乾为天",
    "坎": "坎为水",
    "离": "离为火",
    "震": "震为雷",
    "艮": "艮为山",
    "兑": "兑为泽",
    "巽": "巽为风",
    "乾": "乾为天",
    "坤": "坤为地",
    "雷": "震为雷",
}

# Core one-character / short names that appear as biangua in KB
_CORE_SHORT = {
    "姤",
    "同人",
    "履",
    "中孚",
    "大有",
    "夬",
    "复",
    "师",
    "谦",
    "豫",
    "比",
    "剥",
    "井",
    "既济",
    "节",
    "泰",
    "小畜",
    "屯",
    "蹇",
    "萃",
    "观",
    "升",
    "明夷",
    "临",
    "大壮",
    "需",
    "大畜",
    "大过",
    "家人",
    "小过",
    "归妹",
    "恒",
    "损",
    "旅",
    "无妄",
    "晋",
    "未济",
    "涣",
    "益",
    "睽",
    "蛊",
    "贲",
    "遁",
    "随",
    "革",
    "颐",
    "鼎",
    "蒙",
    "讼",
    "解",
    "丰",
    "咸",
    "噬嗑",
    "困",
    "否",
}


def strip_gua_symbol(name: str) -> str:
    s = _GUA_SYMBOL_RE.sub("", str(name or ""))
    return _WS_RE.sub("", s).strip()


def normalize_gua_name(name: Optional[str]) -> str:
    """Normalize 本卦 / 变卦 full names for equality lookup."""
    if name is None:
        return ""
    s = strip_gua_symbol(str(name))
    if not s or s.lower() == "nan":
        return ""
    if s in _BIANGUA_ALIASES:
        return _BIANGUA_ALIASES[s]
    return s


def normalize_biangua_token(name: Optional[str]) -> str:
    """Normalize short biangua labels from knowledge base or weekly suffix."""
    s = normalize_gua_name(name)
    if not s:
        return ""
    return s


def biangua_core(name: Optional[str]) -> str:
    """Reduce full names to short core used in many KB biangua cells (e.g. 山雷颐 -> 颐)."""
    s = normalize_biangua_token(name)
    if not s:
        return ""
    if s in _CORE_SHORT or len(s) <= 2:
        return s
    if "为" in s and len(s) >= 3:
        head = s[0]
        return head
    for n in (2, 1):
        tail = s[-n:]
        if tail in _CORE_SHORT:
            return tail
    for short in sorted(_CORE_SHORT, key=len, reverse=True):
        if short in s:
            return short
    return s


def biangua_loose_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Loose equality for biangua validation (short vs full)."""
    na = normalize_biangua_token(a)
    nb = normalize_biangua_token(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ca, cb = biangua_core(na), biangua_core(nb)
    if ca and cb and ca == cb:
        return True
    if na in nb or nb in na:
        return True
    ea = _BIANGUA_ALIASES.get(na, na)
    eb = _BIANGUA_ALIASES.get(nb, nb)
    if ea == eb:
        return True
    return biangua_core(ea) == biangua_core(eb)


def parse_bian_field(raw: Optional[str]) -> Tuple[Optional[int], str]:
    """
    Parse weekly 变卦 field like '1-山雷颐' or '0-山泽损'.

    Returns (yao_index_raw 0..5 or None, bian_gua_name).
    """
    if raw is None:
        return None, ""
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None, ""
    if "-" in s:
        left, right = s.split("-", 1)
        left = left.strip()
        right = right.strip()
        try:
            idx = int(left)
        except ValueError:
            return None, normalize_gua_name(s)
        return idx, normalize_gua_name(right)
    return None, normalize_gua_name(s)


def yao_index_to_order(yao_index: Optional[int], *, base: int = 0) -> Optional[int]:
    """
    Map weekly 变卦 prefix digit to yao_order 1..6 **within the 变卦 hexagram**.

    Business rule (user-confirmed):
      - digit 1 → 第1爻 (初爻, yao_order=1)
      - digit 2 → 第2爻
      - digit 3 → 第3爻
      - digit 4 → 第4爻
      - digit 5 → 第5爻
      - digit 0 → 第6爻 (上爻, yao_order=6)  # wrap / 上九·上六

    Example: ``0-山泽损`` → 查「山泽损」第 6 爻简判。

    ``base`` is kept for config compatibility but the circular 0→6 rule is authoritative
    for weekly report digits 0..5.
    """
    if yao_index is None:
        return None
    # Primary: weekly 0..5 circular mapping
    if yao_index == 0:
        return 6
    if 1 <= yao_index <= 5:
        return yao_index
    # Tolerate already 1..6 (direct yao_order)
    if yao_index == 6:
        return 6
    return None


def normalize_stock_code(code) -> str:
    """Normalize to 6-digit A-share style code string when possible."""
    if code is None:
        return ""
    s = str(code).strip()
    if not s or s.lower() == "nan":
        return ""
    for pref in ("SSE.STK.", "SZSE.STK.", "BJSE.STK.", "SSE.", "SZSE.", "BJSE."):
        if s.upper().startswith(pref.upper()):
            s = s[len(pref) :]
            break
    digits = re.sub(r"\D", "", s)
    if digits.isdigit():
        if len(digits) <= 6:
            return digits.zfill(6)
        return digits[-6:]
    return s


def is_numeric_query(q: str) -> bool:
    return bool(re.fullmatch(r"\d{1,6}", (q or "").strip()))
