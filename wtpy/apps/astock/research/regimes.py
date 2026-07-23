# -*- coding: utf-8 -*-
"""Market regime assignment helpers for evaluation (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Union


def _to_float_list(values: Sequence[Any]) -> List[float]:
    out: List[float] = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _synthetic_equity_from_returns(returns: Sequence[float], start: float = 1.0) -> List[float]:
    eq = start
    series = [eq]
    for r in returns:
        eq = eq * (1.0 + float(r))
        series.append(eq)
    # length n returns -> n+1 equity points; align later to dates of returns
    return series[1:] if len(series) > 1 else series


def assign_regime(
    dates: Sequence[Any],
    equity_or_returns: Sequence[Any],
    method: str = "simple",
    *,
    window: int = 20,
    bull_threshold: float = 0.05,
    bear_threshold: float = -0.05,
    is_returns: bool = False,
) -> List[Dict[str, Any]]:
    """Assign bull / bear / sideways regimes along a date axis.

    ``method="simple"`` uses rolling return of synthetic equity (or equity series)
    vs thresholds.
    """
    if len(dates) != len(equity_or_returns):
        raise ValueError("dates and equity_or_returns must have the same length")
    vals = _to_float_list(equity_or_returns)
    if is_returns:
        equity = _synthetic_equity_from_returns(vals)
    else:
        equity = vals

    n = len(equity)
    w = max(1, int(window))
    out: List[Dict[str, Any]] = []
    for i in range(n):
        if method == "simple":
            j0 = max(0, i - w + 1)
            base = equity[j0] if equity[j0] != 0 else 1e-12
            roll_ret = (equity[i] / base) - 1.0
            if roll_ret >= bull_threshold:
                regime = "bull"
            elif roll_ret <= bear_threshold:
                regime = "bear"
            else:
                regime = "sideways"
        else:
            # fallback same as simple
            j0 = max(0, i - w + 1)
            base = equity[j0] if equity[j0] != 0 else 1e-12
            roll_ret = (equity[i] / base) - 1.0
            if roll_ret >= bull_threshold:
                regime = "bull"
            elif roll_ret <= bear_threshold:
                regime = "bear"
            else:
                regime = "sideways"
        out.append({"date": dates[i], "regime": regime, "rolling_return": roll_ret})
    return out


def slice_metrics_by_regime(
    trades_or_yearly: Sequence[Dict[str, Any]],
    regimes: Sequence[Dict[str, Any]],
    *,
    date_key: str = "date",
    regime_key: str = "regime",
    metric_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate dict metric fixtures by regime label.

    ``trades_or_yearly`` items should carry a date field matching regime dates
    (or a ``regime`` field already). Returns per-regime aggregates:
    count, sum/mean of numeric metrics.
    """
    date_to_regime: Dict[Any, str] = {}
    for r in regimes:
        date_to_regime[r.get("date")] = str(r.get(regime_key) or "unknown")

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in trades_or_yearly:
        if not isinstance(row, dict):
            continue
        reg = row.get(regime_key)
        if reg is None:
            reg = date_to_regime.get(row.get(date_key), "unknown")
        reg = str(reg)
        buckets.setdefault(reg, []).append(row)

    keys = list(metric_keys) if metric_keys is not None else None
    result: Dict[str, Dict[str, Any]] = {}
    for reg, rows in buckets.items():
        agg: Dict[str, Any] = {"count": len(rows), "items": rows}
        # auto-detect numeric keys if not provided
        scan_keys = keys
        if scan_keys is None:
            seen = set()
            for r in rows:
                for k, v in r.items():
                    if k in (date_key, regime_key):
                        continue
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        seen.add(k)
            scan_keys = sorted(seen)
        for k in scan_keys:
            vals = []
            for r in rows:
                if k in r and isinstance(r[k], (int, float)) and not isinstance(r[k], bool):
                    vals.append(float(r[k]))
            if vals:
                agg[k + "_sum"] = sum(vals)
                agg[k + "_mean"] = sum(vals) / len(vals)
                agg[k + "_n"] = len(vals)
        result[reg] = agg
    return result


__all__ = ["assign_regime", "slice_metrics_by_regime"]
