# -*- coding: utf-8 -*-
"""Composite scoring, hard filters, Pareto ranking (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_WEIGHTS: Dict[str, float] = {
    "total_return": 0.25,
    "sharpe_like": 0.20,
    "calmar_like": 0.15,
    "win_rate": 0.10,
    "oos_pref": 0.20,
    "drawdown_penalty": 0.15,
    "overfit_penalty": 0.15,
}


def _f(m: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in m and m[k] is not None:
            try:
                return float(m[k])
            except (TypeError, ValueError):
                continue
    return default


def composite_score(
    metrics: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """Multi-metric composite score (higher is better).

    Blends out-of-sample preference, sharpe-like, calmar-like, win_rate,
    drawdown penalty, and overfit penalty. Intentionally not equal to
    total_return alone.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    m = metrics or {}

    tr = _f(m, "total_return", "return")
    sharpe = _f(m, "sharpe_like", "sharpe")
    max_dd = abs(_f(m, "max_drawdown", "drawdown"))
    # calmar-like: return / max_dd
    if max_dd > 1e-12:
        calmar = tr / max_dd
    else:
        calmar = tr * 10.0 if tr > 0 else tr
    if "calmar_like" in m or "calmar" in m:
        calmar = _f(m, "calmar_like", "calmar", default=calmar)

    wr = _f(m, "win_rate")
    # OOS preference: prefer metrics marked oos or out_score
    oos = _f(m, "out_score", "oos_score", "oos_return")
    if "out_score" not in m and "oos_score" not in m and "oos_return" not in m:
        # soft: use total_return as proxy but weight separately
        oos = tr * 0.5 + _f(m, "stability", default=0.0) * 0.5

    overfit = _f(m, "overfit", "decay", "overfit_penalty")
    if overfit == 0.0 and "in_score" in m and "out_score" in m:
        overfit = max(0.0, _f(m, "in_score") - _f(m, "out_score"))

    score = (
        w.get("total_return", 0.0) * tr
        + w.get("sharpe_like", 0.0) * sharpe
        + w.get("calmar_like", 0.0) * calmar
        + w.get("win_rate", 0.0) * wr
        + w.get("oos_pref", 0.0) * oos
        - w.get("drawdown_penalty", 0.0) * max_dd
        - w.get("overfit_penalty", 0.0) * overfit
    )
    # optional explicit stability boost
    score += 0.05 * _f(m, "stability")
    return float(score)


def hard_filter(
    metrics: Dict[str, Any],
    rules: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """Return (ok, reasons). Rejects too-few trades / excessive drawdown etc."""
    rules = dict(rules or {})
    m = metrics or {}
    reasons: List[str] = []

    min_trades = rules.get("min_trades")
    if min_trades is not None:
        n = int(_f(m, "n_trades", "trade_count", "trades", default=0))
        if n < int(min_trades):
            reasons.append(f"too_few_trades:{n}<{min_trades}")

    max_dd_rule = rules.get("max_drawdown")
    if max_dd_rule is not None:
        dd = abs(_f(m, "max_drawdown", "drawdown"))
        if dd > float(max_dd_rule):
            reasons.append(f"excessive_drawdown:{dd}>{max_dd_rule}")

    min_wr = rules.get("min_win_rate")
    if min_wr is not None:
        wr = _f(m, "win_rate")
        if wr < float(min_wr):
            reasons.append(f"low_win_rate:{wr}<{min_wr}")

    min_ret = rules.get("min_total_return")
    if min_ret is not None:
        tr = _f(m, "total_return", "return")
        if tr < float(min_ret):
            reasons.append(f"low_total_return:{tr}<{min_ret}")

    min_pf = rules.get("min_out_windows_profit_frac")
    if min_pf is not None:
        pf = m.get("out_windows_profit_frac")
        if pf is None:
            pf = m.get("windows_profit_frac")
        if pf is not None and float(pf) < float(min_pf):
            reasons.append(f"low_out_windows_profit_frac:{pf}<{min_pf}")

    return (len(reasons) == 0, reasons)


def _dominates(a: Dict[str, Any], b: Dict[str, Any], objectives: Sequence[Tuple[str, str]]) -> bool:
    """True if a Pareto-dominates b (strictly better on at least one, not worse on all)."""
    better = False
    for key, direction in objectives:
        av = _f(a, key)
        bv = _f(b, key)
        if direction == "max":
            if av < bv:
                return False
            if av > bv:
                better = True
        else:  # min
            if av > bv:
                return False
            if av < bv:
                better = True
    return better


def pareto_front(
    candidates: List[Dict[str, Any]],
    objectives: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Return non-dominated candidates for multi-objective selection."""
    if objectives is None:
        objectives = [
            ("total_return", "max"),
            ("max_drawdown", "min"),
            ("stability", "max"),
        ]
    objs = list(objectives)
    front: List[Dict[str, Any]] = []
    for c in candidates:
        dominated = False
        for other in candidates:
            if other is c:
                continue
            if _dominates(other, c, objs):
                dominated = True
                break
        if not dominated:
            front.append(c)
    return front


def rank_candidates(
    candidates: List[Dict[str, Any]],
    mode: str = "composite",
    *,
    weights: Optional[Dict[str, float]] = None,
    objectives: Optional[Sequence[Tuple[str, str]]] = None,
    rules: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Rank candidates by composite score or Pareto membership.

    Composite mode must NOT sort solely by total_return — it uses
    ``composite_score`` which blends multiple metrics.
    """
    rows = [dict(c) for c in (candidates or [])]
    # annotate scores / filter
    annotated: List[Dict[str, Any]] = []
    for c in rows:
        metrics = c.get("metrics") if isinstance(c.get("metrics"), dict) else c
        ok, reasons = hard_filter(metrics, rules) if rules else (True, [])
        sc = composite_score(metrics, weights=weights)
        item = dict(c)
        item["_composite"] = sc
        item["_hard_ok"] = ok
        item["_hard_reasons"] = reasons
        item["_total_return"] = _f(metrics, "total_return", "return")
        annotated.append(item)

    if mode == "pareto":
        # rank: front first, then by composite within / outside
        front = pareto_front(annotated, objectives=objectives)
        front_ids = {id(x) for x in front}
        # mark
        for a in annotated:
            a["_on_pareto"] = id(a) in front_ids or any(
                a is f or a.get("id") == f.get("id") for f in front
            )
        # simpler mark by index
        front_set = set()
        for f in front:
            front_set.add(id(f))
        for a in annotated:
            a["_on_pareto"] = id(a) in front_set
        annotated.sort(
            key=lambda x: (
                0 if x.get("_on_pareto") else 1,
                -float(x.get("_composite") or 0.0),
            )
        )
        return annotated

    # composite (default)
    annotated.sort(key=lambda x: (-float(x.get("_composite") or 0.0), -float(x.get("_total_return") or 0.0)))
    for i, a in enumerate(annotated):
        a["_rank"] = i + 1
    return annotated


def neighborhood_stability(
    grid_points: Sequence[Dict[str, Any]],
    *,
    metric_key: str = "total_return",
    param_keys: Optional[Sequence[str]] = None,
    spike_ratio: float = 1.5,
    plateau_tol: float = 0.15,
) -> Dict[str, Any]:
    """Detect isolated spikes vs plateaus on a parameter grid.

    Each point: ``params`` dict + metric (or top-level metric_key).
    """
    pts = list(grid_points or [])
    if not pts:
        return {"flags": [], "best": None, "is_spike": False, "plateau": False}

    def _metric(p: Dict[str, Any]) -> float:
        if metric_key in p:
            return float(p[metric_key])
        m = p.get("metrics") or {}
        return float(m.get(metric_key) or p.get("metric") or 0.0)

    def _params(p: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(p.get("params"), dict):
            return p["params"]
        return {k: p[k] for k in (param_keys or []) if k in p} if param_keys else {
            k: v for k, v in p.items() if k not in (metric_key, "metrics", "metric", "id")
        }

    scored = [(p, _metric(p), _params(p)) for p in pts]
    scored.sort(key=lambda t: -t[1])
    best_p, best_m, best_params = scored[0]

    # neighbors: Hamming distance 1 on scalar params (or equal on all-but-one)
    neighbors: List[Tuple[Dict[str, Any], float]] = []
    for p, m, pr in scored[1:]:
        keys = set(best_params) | set(pr)
        if not keys:
            neighbors.append((p, m))
            continue
        diffs = sum(1 for k in keys if best_params.get(k) != pr.get(k))
        if diffs <= 1:
            neighbors.append((p, m))

    if not neighbors:
        # no neighbors → treat as isolated if only one point or sparse
        is_spike = len(scored) > 1
        return {
            "best": best_p,
            "best_metric": best_m,
            "neighbors": [],
            "neighbor_mean": None,
            "is_spike": is_spike,
            "plateau": False,
            "flags": ["isolated_spike"] if is_spike else [],
        }

    n_vals = [m for _, m in neighbors]
    n_mean = sum(n_vals) / len(n_vals)
    is_spike = best_m > n_mean * spike_ratio and best_m - n_mean > abs(n_mean) * plateau_tol + 1e-12
    # plateau: best close to neighbor mean
    plateau = (not is_spike) and (
        abs(best_m - n_mean) <= max(abs(n_mean) * plateau_tol, 1e-9)
        or all(abs(best_m - m) <= max(abs(best_m) * plateau_tol, 1e-9) for m in n_vals)
    )
    flags: List[str] = []
    if is_spike:
        flags.append("isolated_spike")
    if plateau:
        flags.append("plateau")
    return {
        "best": best_p,
        "best_metric": best_m,
        "neighbors": [{"point": p, "metric": m} for p, m in neighbors],
        "neighbor_mean": n_mean,
        "is_spike": is_spike,
        "plateau": plateau,
        "flags": flags,
    }


def spike_risk_flag(
    best: Dict[str, Any],
    neighbors: Sequence[Dict[str, Any]],
    *,
    metric_key: str = "total_return",
    spike_ratio: float = 1.5,
) -> Dict[str, Any]:
    """Flag if best metric is an isolated spike vs neighbors."""
    def _m(p: Dict[str, Any]) -> float:
        if metric_key in p:
            return float(p[metric_key])
        mm = p.get("metrics") or {}
        return float(mm.get(metric_key) or p.get("metric") or 0.0)

    bm = _m(best) if best else 0.0
    n_vals = [_m(n) for n in (neighbors or [])]
    if not n_vals:
        return {"spike_risk": True, "reason": "no_neighbors", "best_metric": bm, "neighbor_mean": None}
    n_mean = sum(n_vals) / len(n_vals)
    risk = bm > n_mean * spike_ratio
    return {
        "spike_risk": bool(risk),
        "reason": "isolated_spike" if risk else "ok",
        "best_metric": bm,
        "neighbor_mean": n_mean,
        "spike_ratio": spike_ratio,
    }


__all__ = [
    "DEFAULT_WEIGHTS",
    "composite_score",
    "hard_filter",
    "pareto_front",
    "rank_candidates",
    "neighborhood_stability",
    "spike_risk_flag",
]
