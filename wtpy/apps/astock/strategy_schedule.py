# -*- coding: utf-8 -*-
"""Schedule / session / weekday helpers for portfolio backtest."""

from __future__ import annotations

from datetime import date as _date
from typing import List, Optional, Sequence, Set, Tuple

from .study import SignalEvent
from .strategy_models import (
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    LEGACY_HOLD_EXPIRED,
)

_WEEKDAY_ALIASES = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "mon": 1, "monday": 1, "一": 1, "周一": 1, "星期一": 1,
    "tue": 2, "tues": 2, "tuesday": 2, "二": 2, "周二": 2, "星期二": 2,
    "wed": 3, "wednesday": 3, "三": 3, "周三": 3, "星期三": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "四": 4, "周四": 4, "星期四": 4,
    "fri": 5, "friday": 5, "五": 5, "周五": 5, "星期五": 5,
    "sat": 6, "saturday": 6, "六": 6, "周六": 6, "星期六": 6,
    "sun": 7, "sunday": 7, "日": 7, "天": 7, "周日": 7, "周天": 7, "星期日": 7, "星期天": 7,
}
_WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def parse_price_session(value, *, default: str = "open") -> str:
    """Normalize buy/sell session: 'open' or 'close'."""
    if value is None or value == "":
        return default
    s = str(value).strip().lower()
    if s in ("open", "o", "开盘", "开", "op"):
        return "open"
    if s in ("close", "c", "收盘", "收", "cl"):
        return "close"
    raise ValueError("price session must be open or close, got %r" % (value,))


def session_label_cn(session: str) -> str:
    return "开盘" if parse_price_session(session) == "open" else "收盘"


def bar_session_price(bar, session: str) -> float:
    """Raw price from bar for open/close session (no slippage)."""
    session = parse_price_session(session)
    if session == "close":
        return float(bar.close)
    return float(bar.open)


def parse_signal_weekdays(value) -> Optional[List[int]]:
    """Parse weekday allow-list.

    Accepts None / empty (all days), list/tuple of ints or strings, or comma-separated
    string like "5" / "fri,五" / "1,3,5". Returns sorted unique ints in 1..7, or None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s in ("*", "all", "全部", "any"):
            return None
        parts = [p.strip() for p in s.replace("，", ",").replace("、", ",").split(",") if p.strip()]
        value = parts
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [int(value)]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("signal_weekdays must be list/str/int, got %r" % (type(value),))
    if len(value) == 0:
        return None
    out: Set[int] = set()
    for item in value:
        if item is None or item == "":
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            n = int(item)
            if n == 0:
                n = 7  # allow 0 as Sunday alias
            if n < 1 or n > 7:
                raise ValueError("signal_weekdays item out of range 1..7: %s" % item)
            out.add(n)
            continue
        key = str(item).strip().lower()
        if key in _WEEKDAY_ALIASES:
            out.add(_WEEKDAY_ALIASES[key])
            continue
        # bare digit string already handled via int path if pure digit
        if key.isdigit():
            n = int(key)
            if n == 0:
                n = 7
            if 1 <= n <= 7:
                out.add(n)
                continue
        raise ValueError("unknown weekday in signal_weekdays: %r" % (item,))
    if not out:
        return None
    return sorted(out)



def parse_single_weekday(value) -> Optional[int]:
    """Parse one weekday 1..7, or None if empty (use legacy lag/hold)."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    days = parse_signal_weekdays(value if not isinstance(value, (int, float)) else int(value))
    if not days:
        return None
    return int(days[0])


def format_single_weekday(day: Optional[int]) -> str:
    if day is None:
        return "—"
    return format_signal_weekdays([int(day)])


def format_signal_weekdays(days: Optional[Sequence[int]]) -> str:
    if not days:
        return "全部"
    return "、".join(_WEEKDAY_CN.get(int(d), str(d)) for d in days)


def yyyymmdd_isoweekday(d: int) -> int:
    """Return ISO weekday 1..7 for YYYYMMDD int."""
    y, m, day = int(d) // 10000, (int(d) // 100) % 100, int(d) % 100
    return _date(y, m, day).isoweekday()


def filter_events_by_signal_weekdays(
    events: Sequence[SignalEvent],
    weekdays: Optional[Sequence[int]],
) -> List[SignalEvent]:
    """Keep only events whose signal date weekday is allowed. None/empty = all."""
    allowed = parse_signal_weekdays(weekdays)
    if not allowed:
        return list(events)
    allow_set = set(int(x) for x in allowed)
    kept: List[SignalEvent] = []
    for ev in events:
        try:
            wd = yyyymmdd_isoweekday(int(ev.date))
        except Exception:
            continue
        if wd in allow_set:
            kept.append(ev)
    return kept


def normalize_exit_reason(reason: Optional[str]) -> str:
    """Map legacy codes to phase-1 canonical exit reasons."""
    if not reason:
        return EXIT_REASON_TIME_EXIT
    r = str(reason)
    if r == LEGACY_HOLD_EXPIRED:
        return EXIT_REASON_TIME_EXIT
    return r


def compose_sell_reason(
    trigger: Optional[str],
    defer: Optional[str],
    fallback: str = EXIT_REASON_TIME_EXIT,
) -> str:
    """Compose fill reason preserving original risk trigger across deferrals.

    Canonical fallbacks: time_exit (hold N), weekday_exit (weekday anchor).
    Legacy hold_expired is normalized to time_exit.
    """
    fb = normalize_exit_reason(fallback)
    if trigger and defer:
        return f"{trigger}_deferred_{defer}"
    if trigger:
        return trigger
    if defer in ("suspended", "limit_down", "bad_price"):
        return f"{fb}_deferred_{defer}"
    return fb


def _week_key(d: int) -> Tuple[int, int]:
    from datetime import date

    y, m, day = d // 10000, (d // 100) % 100, d % 100
    iso = date(y, m, day).isocalendar()
    return int(iso[0]), int(iso[1])


def _month_key(d: int) -> Tuple[int, int]:
    return d // 10000, (d // 100) % 100


