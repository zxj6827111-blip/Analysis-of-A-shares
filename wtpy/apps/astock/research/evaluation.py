# -*- coding: utf-8 -*-
"""Evaluation center facade (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .gua_gain import pair_gua_gain
from .regimes import assign_regime, slice_metrics_by_regime
from .heatmap import build_heatmap
from .scoring import (
    composite_score,
    hard_filter,
    neighborhood_stability,
    pareto_front,
    rank_candidates,
    spike_risk_flag,
)
from .validation import score_in_out


def evaluate_trials(
    trials: List[Dict[str, Any]],
    *,
    rank_mode: str = "composite",
    hard_rules: Optional[Dict[str, Any]] = None,
    heatmap_x: str = "exit_weekday",
    heatmap_y: str = "sell_on",
    heatmap_metric: str = "total_return",
    stability_metric: str = "total_return",
) -> Dict[str, Any]:
    """Evaluate a list of trial dicts → ranking, pareto, gua gains, heatmaps, flags."""
    trials = list(trials or [])
    default_rules = hard_rules if hard_rules is not None else {
        "min_trades": 5,
        "max_drawdown": 0.5,
    }

    # normalize: ensure metrics at top-level for helpers
    normed: List[Dict[str, Any]] = []
    for t in trials:
        row = dict(t)
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        for k, v in metrics.items():
            if k not in row:
                row[k] = v
        # composite for convenience
        row["_composite"] = composite_score(row)
        ok, reasons = hard_filter(row, default_rules)
        row["_hard_ok"] = ok
        row["_hard_reasons"] = reasons
        normed.append(row)

    ranking = rank_candidates(normed, mode=rank_mode, rules=default_rules)
    front = pareto_front(normed)
    gains = pair_gua_gain(normed)

    heatmaps: Dict[str, Any] = {}
    # primary heatmap if axes present
    if any(heatmap_x in r and heatmap_y in r for r in normed):
        heatmaps["primary"] = build_heatmap(normed, heatmap_x, heatmap_y, heatmap_metric)
    # optional gua x exit
    if any("gua_key" in r and heatmap_x in r for r in normed):
        heatmaps["gua_x_exit"] = build_heatmap(normed, heatmap_x, "gua_key", heatmap_metric)

    # stability on grid if params-like fields exist
    grid_points = []
    for r in normed:
        grid_points.append(r)
    stab = neighborhood_stability(grid_points, metric_key=stability_metric)
    flags: List[str] = list(stab.get("flags") or [])
    if stab.get("is_spike"):
        flags.append("spike_risk")

    # best vs neighbors spike flag if ranking non-empty
    spike = {"spike_risk": False}
    if ranking:
        best = ranking[0]
        neighbors = ranking[1:6]
        spike = spike_risk_flag(best, neighbors, metric_key=stability_metric)
        if spike.get("spike_risk"):
            flags.append("best_spike_risk")

    # optional in/out if present on trials
    io_scores = []
    for r in normed:
        if "in_metrics" in r and "out_metrics" in r:
            io_scores.append(
                {
                    "id": r.get("id") or r.get("trial_id"),
                    **score_in_out(r["in_metrics"], r["out_metrics"]),
                }
            )

    # regime / yearly slices when equity or yearly_metrics provided
    regimes_out: Dict[str, Any] = {}
    yearly_out: Dict[str, Any] = {}
    for r in normed:
        tid = r.get("id") or r.get("trial_id") or r.get("param_hash") or "trial"
        if r.get("equity_curve") and r.get("dates"):
            try:
                reg_series = assign_regime(r["dates"], r["equity_curve"], method="simple")
                trade_rows = r.get("regime_rows") or [
                    {"date": x["date"], "ret": x.get("rolling_return", 0.0)} for x in reg_series
                ]
                regimes_out[str(tid)] = {
                    "series": reg_series,
                    "by_regime": slice_metrics_by_regime(trade_rows, reg_series),
                }
            except Exception as e:  # noqa: BLE001
                regimes_out[str(tid)] = {"error": str(e)[:200]}
        if isinstance(r.get("yearly_metrics"), dict):
            yearly_out[str(tid)] = r["yearly_metrics"]
        elif isinstance(r.get("yearly_metrics"), list):
            ym: Dict[str, Any] = {}
            for row in r["yearly_metrics"]:
                if isinstance(row, dict) and row.get("year") is not None:
                    ym[str(row["year"])] = row
            yearly_out[str(tid)] = ym

    return {
        "n_trials": len(normed),
        "ranking": ranking,
        "pareto": front,
        "gua_gains": gains,
        "heatmaps": heatmaps,
        "stability": stab,
        "spike": spike,
        "flags": sorted(set(flags)),
        "io_scores": io_scores,
        "hard_rules": default_rules,
        "regimes": regimes_out,
        "yearly": yearly_out,
    }


__all__ = ["evaluate_trials"]
