# -*- coding: utf-8 -*-
"""Promotion helpers: select top candidates for full-engine retest (Phase 6)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .scoring import rank_candidates


def _metric_value(row: Dict[str, Any], metric: str) -> float:
    if metric in row and row[metric] is not None:
        try:
            return float(row[metric])
        except (TypeError, ValueError):
            pass
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    if metric in metrics and metrics[metric] is not None:
        try:
            return float(metrics[metric])
        except (TypeError, ValueError):
            pass
    if metric == "composite_score":
        if "_composite" in row:
            try:
                return float(row["_composite"])
            except (TypeError, ValueError):
                pass
        from .scoring import composite_score

        return float(composite_score(row))
    return float("-inf")


def select_for_full_retest(
    candidates: Sequence[Dict[str, Any]],
    top_n: int,
    metric: str = "composite_score",
) -> List[Dict[str, Any]]:
    """Return list of param dicts for full engine retest (top_n by metric).

    Integrates with evaluate_trials ranking when candidates already carry
    metrics / composite scores. Each returned item is a param dict suitable
    for enqueue (prefers ``params`` sub-dict if present, else strips known
    metric keys).
    """
    top_n = max(0, int(top_n))
    if top_n == 0 or not candidates:
        return []

    rows = [dict(c) for c in candidates]
    # Prefer existing ranking order if metric is composite and ranking-like fields exist
    if metric in ("composite_score", "composite", "rank"):
        try:
            ranked = rank_candidates(rows, mode="composite")
        except Exception:
            ranked = sorted(
                rows,
                key=lambda r: _metric_value(r, "composite_score"),
                reverse=True,
            )
    else:
        ranked = sorted(rows, key=lambda r: _metric_value(r, metric), reverse=True)

    selected = ranked[:top_n]
    out: List[Dict[str, Any]] = []
    meta_keys = {
        "id",
        "trial_id",
        "metrics",
        "total_return",
        "max_drawdown",
        "win_rate",
        "n_trades",
        "stability",
        "sharpe_like",
        "composite_score",
        "_composite",
        "_hard_ok",
        "_hard_reasons",
        "in_metrics",
        "out_metrics",
        "paper_trading_observe",
        "rank",
        "score",
    }
    for row in selected:
        if isinstance(row.get("params"), dict):
            params = dict(row["params"])
        else:
            params = {k: v for k, v in row.items() if k not in meta_keys}
        # keep identity hints for traceability without polluting engine params heavily
        if "id" in row and "id" not in params:
            params.setdefault("_source_id", row["id"])
        if "trial_id" in row and "trial_id" not in params:
            params.setdefault("_source_trial_id", row["trial_id"])
        out.append(params)
    return out


__all__ = ["select_for_full_retest"]
