# -*- coding: utf-8 -*-
"""Variant constraint filters for research parameter spaces.

Rules are **conservative**: prefer keeping schedulable weekday paths
(e.g. Fri signal → Mon buy → later exit) over aggressive rejection.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

VALID_SESSIONS = frozenset({"open", "close"})


def _reason(code: str, detail: str = "") -> Dict[str, Any]:
    return {"code": code, "detail": detail or code}


def validate_variant(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return list of rejection reason dicts (empty if valid)."""
    reasons: List[Dict[str, Any]] = []

    rule_ids = params.get("rule_ids") or []
    if not rule_ids or all(not r for r in rule_ids):
        reasons.append(_reason("missing_rule_ids", "rule_ids empty or missing"))

    buy_on = (params.get("buy_on") or "open").lower()
    sell_on = (params.get("sell_on") or "open").lower()
    if buy_on not in VALID_SESSIONS:
        reasons.append(
            _reason("invalid_buy_on", f"buy_on must be open|close, got {buy_on!r}")
        )
    if sell_on not in VALID_SESSIONS:
        reasons.append(
            _reason("invalid_sell_on", f"sell_on must be open|close, got {sell_on!r}")
        )

    buy_weekday = params.get("buy_weekday")
    exit_weekday = params.get("exit_weekday")
    entry_lag = params.get("entry_lag")
    hold = params.get("hold")

    # T+N path: when buy_weekday is not set, entry_lag must be >= 1
    if buy_weekday is None:
        try:
            lag_i = int(entry_lag) if entry_lag is not None else 1
        except (TypeError, ValueError):
            lag_i = -1
        if lag_i < 1:
            reasons.append(
                _reason(
                    "entry_lag_lt_1",
                    f"entry_lag must be >= 1 on T+N path, got {entry_lag!r}",
                )
            )

    # Same-session causality: buy open on signal day without lag when signal
    # is close-confirmed is engine-dependent; simple rule:
    # if signal_weekdays intersects buy_weekday and buy_on==open and entry_lag==0
    # → buy_before_signal_session
    signal_weekdays = params.get("signal_weekdays")
    try:
        lag_for_sig = int(entry_lag) if entry_lag is not None else 1
    except (TypeError, ValueError):
        lag_for_sig = 1
    if (
        buy_weekday is not None
        and signal_weekdays
        and isinstance(signal_weekdays, (list, tuple))
        and buy_weekday in signal_weekdays
        and buy_on == "open"
        and lag_for_sig < 1
    ):
        reasons.append(
            _reason(
                "buy_before_signal_session",
                "buy open same weekday as signal with entry_lag < 1",
            )
        )

    # exit_before_entry_impossible: same weekday exit with hold<=0 and sell before buy session
    try:
        hold_i = int(hold) if hold is not None else 1
    except (TypeError, ValueError):
        hold_i = 1
    if (
        buy_weekday is not None
        and exit_weekday is not None
        and buy_weekday == exit_weekday
        and hold_i <= 0
    ):
        reasons.append(
            _reason(
                "exit_before_entry_impossible",
                "exit_weekday == buy_weekday with hold <= 0",
            )
        )

    # t1_same_day_exit: A-share cannot exit on entry day when hold is explicitly 0
    # (weekday equal alone is OK — engine rolls to next week)
    if (
        buy_weekday is not None
        and exit_weekday is not None
        and buy_weekday == exit_weekday
        and hold_i == 0
    ):
        # already covered by exit_before_entry; also tag t1 explicitly
        if not any(r["code"] == "t1_same_day_exit" for r in reasons):
            reasons.append(
                _reason(
                    "t1_same_day_exit",
                    "same buy/exit weekday with hold=0 violates T+1",
                )
            )

    for field, code in (("stop_loss", "invalid_stop_loss"), ("take_profit", "invalid_take_profit")):
        val = params.get(field)
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            reasons.append(_reason(code, f"{field} not a number: {val!r}"))
            continue
        if not (0.0 < f < 1.0):
            reasons.append(
                _reason(code, f"{field} must be in (0,1), got {f}")
            )

    return reasons


def filter_variants(
    variants: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Filter variants; rejected items include ``_reject`` with reason codes.

    Returns:
        (kept, rejected) where rejected entries are shallow copies of input
        with ``_reject: {"reasons": [...], "codes": [...]}``.
    """
    kept: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for v in variants:
        reasons = validate_variant(v)
        if not reasons:
            kept.append(v)
            continue
        bad = dict(v)
        codes = [r["code"] for r in reasons]
        bad["_reject"] = {"reasons": reasons, "codes": codes}
        rejected.append(bad)
    return kept, rejected


def summarize_rejections(rejected: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Count rejection reason codes across rejected variants."""
    c: Counter = Counter()
    for item in rejected:
        rej = item.get("_reject") or {}
        codes = rej.get("codes")
        if not codes:
            reasons = rej.get("reasons") or []
            codes = [r.get("code") if isinstance(r, dict) else str(r) for r in reasons]
        for code in codes or ["unknown"]:
            c[str(code)] += 1
    return dict(c)
