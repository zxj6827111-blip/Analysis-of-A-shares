# -*- coding: utf-8 -*-
"""Performance drift detection vs baseline (Phase 6)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


DEFAULT_THRESHOLDS: Dict[str, float] = {
    "total_return_drop": 0.10,  # absolute drop in total_return
    "max_drawdown_worse": 0.05,  # max_drawdown increase (worse)
    "win_rate_drop": 0.08,
}


def _f(m: Dict[str, Any], key: str, default: float = 0.0) -> float:
    if not isinstance(m, dict):
        return default
    v = m.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def detect_drift(
    recent_metrics: Dict[str, Any],
    baseline_metrics: Dict[str, Any],
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Compare recent vs baseline metrics.

    Returns ``{drift: bool, reasons: [], severity: str}``.
    Severity: none | low | medium | high
    """
    thr = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        thr.update({k: float(v) for k, v in thresholds.items() if v is not None})

    reasons: List[str] = []
    score = 0  # weight hits for severity

    r_ret = _f(recent_metrics, "total_return")
    b_ret = _f(baseline_metrics, "total_return")
    ret_drop = b_ret - r_ret
    if ret_drop > thr.get("total_return_drop", 0.10):
        reasons.append(
            f"total_return drop {ret_drop:.4f} > {thr['total_return_drop']:.4f} "
            f"(baseline={b_ret:.4f}, recent={r_ret:.4f})"
        )
        score += 2 if ret_drop > thr["total_return_drop"] * 1.5 else 1

    r_dd = _f(recent_metrics, "max_drawdown")
    b_dd = _f(baseline_metrics, "max_drawdown")
    dd_worse = r_dd - b_dd
    if dd_worse > thr.get("max_drawdown_worse", 0.05):
        reasons.append(
            f"max_drawdown worse by {dd_worse:.4f} > {thr['max_drawdown_worse']:.4f} "
            f"(baseline={b_dd:.4f}, recent={r_dd:.4f})"
        )
        score += 2 if dd_worse > thr["max_drawdown_worse"] * 1.5 else 1

    r_wr = _f(recent_metrics, "win_rate")
    b_wr = _f(baseline_metrics, "win_rate")
    wr_drop = b_wr - r_wr
    if wr_drop > thr.get("win_rate_drop", 0.08):
        reasons.append(
            f"win_rate drop {wr_drop:.4f} > {thr['win_rate_drop']:.4f} "
            f"(baseline={b_wr:.4f}, recent={r_wr:.4f})"
        )
        score += 1

    drifted = bool(reasons)
    if not drifted:
        severity = "none"
    elif score >= 4:
        severity = "high"
    elif score >= 2:
        severity = "medium"
    else:
        severity = "low"

    return {
        "drift": drifted,
        "reasons": reasons,
        "severity": severity,
        "deltas": {
            "total_return": ret_drop,
            "max_drawdown": dd_worse,
            "win_rate": wr_drop,
        },
        "thresholds": thr,
    }


__all__ = ["detect_drift", "DEFAULT_THRESHOLDS"]
