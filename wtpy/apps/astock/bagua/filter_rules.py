# -*- coding: utf-8 -*-
"""Bagua yao allowlists used when backtests enable 八卦.

Default product policy (2026-07): when the user includes 八卦OHLC / with_bagua,
signals are filtered by **最佳3爻** (user judgment list from the 64-gua study),
not merely labelled.

Lists match ``outputs/astock/_run_735_bagua_user_allow.py``.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

# 用户确认的「最佳3爻」：地雷复初九 / 地风升初六 / 地天泰初九
BEST3: List[Tuple[str, str]] = [
    ("地雷复", "初九"),
    ("地风升", "初六"),
    ("地天泰", "初九"),
]

BAGUA_MODE_BEST3 = "best3"
DEFAULT_BAGUA_FILTER_MODE = BAGUA_MODE_BEST3

# Human labels for titles / Excel / history
BAGUA_MODE_LABELS = {
    BAGUA_MODE_BEST3: "八卦最佳3爻",
}


def strip_gua(name: str) -> str:
    if not name:
        return ""
    s = str(name)
    while s and not ("\u4e00" <= s[0] <= "\u9fff"):
        s = s[1:]
    return s


def pair_from_bagua(bg: Optional[dict]) -> Optional[Tuple[str, str]]:
    if not bg:
        return None
    full = bg.get("full_name") or bg.get("gua_name") or ""
    yao = bg.get("yao_name") or ""
    gua = strip_gua(full)
    if not gua or not yao:
        return None
    return gua, str(yao)


def match_allow(gua: str, yao: str, allow: Sequence[Tuple[str, str]]) -> bool:
    for key, y in allow:
        if yao != y:
            continue
        if gua == key or key in gua or gua in key:
            return True
    return False


def allowlist_for_mode(mode: str) -> Sequence[Tuple[str, str]]:
    m = (mode or DEFAULT_BAGUA_FILTER_MODE).strip().lower()
    if m in ("best3", "user_best3", "最佳3爻", "最佳三爻"):
        return BEST3
    raise ValueError(f"unsupported bagua filter mode: {mode!r} (only best3 is enabled)")


def mode_label(mode: str) -> str:
    m = (mode or DEFAULT_BAGUA_FILTER_MODE).strip().lower()
    if m in ("best3", "user_best3", "最佳3爻", "最佳三爻"):
        return BAGUA_MODE_LABELS[BAGUA_MODE_BEST3]
    return f"八卦:{mode}"


def event_matches_allow(ev, allow: Sequence[Tuple[str, str]]) -> bool:
    bg = getattr(ev, "bagua", None) or {}
    if isinstance(bg, dict):
        p = pair_from_bagua(bg)
    else:
        p = None
    if not p:
        return False
    gua, yao = p
    return match_allow(gua, yao, allow)


def filter_events_by_bagua_mode(events: Iterable, mode: str = DEFAULT_BAGUA_FILTER_MODE) -> List:
    """Keep only events whose attached bagua (卦, 爻) is on the allowlist for mode."""
    allow = allowlist_for_mode(mode)
    out = []
    for ev in events:
        if event_matches_allow(ev, allow):
            out.append(ev)
    return out


def best3_display_pairs() -> List[str]:
    return [f"{g}|{y}" for g, y in BEST3]
