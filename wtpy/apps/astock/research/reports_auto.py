# -*- coding: utf-8 -*-
"""Auto research summaries and paper-trading candidate flags (Phase 6).

Named reports_auto to avoid clashing with any reports package.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def build_research_summary(
    experiment_id: Optional[str],
    trials: Sequence[Dict[str, Any]],
    evaluate_result: Optional[Dict[str, Any]] = None,
    drift_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build markdown + structured summary for an experiment batch."""
    trials = list(trials or [])
    evaluate_result = evaluate_result or {}
    n = len(trials)
    ranking = evaluate_result.get("ranking") or []
    flags = list(evaluate_result.get("flags") or [])
    pareto_n = len(evaluate_result.get("pareto") or [])

    top_lines: List[str] = []
    for i, row in enumerate(ranking[:5]):
        rid = row.get("id") or row.get("trial_id") or f"#{i}"
        tr = row.get("total_return", row.get("metrics", {}).get("total_return") if isinstance(row.get("metrics"), dict) else None)
        top_lines.append(f"  {i + 1}. {rid} total_return={tr}")

    drift_block = ""
    if drift_result:
        drift_block = (
            f"\n## Drift\n"
            f"- drift: {drift_result.get('drift')}\n"
            f"- severity: {drift_result.get('severity')}\n"
            f"- reasons: {', '.join(drift_result.get('reasons') or []) or 'none'}\n"
        )

    exp = experiment_id or "adhoc"
    md = (
        f"# Research summary: {exp}\n\n"
        f"- trials: {n}\n"
        f"- ranking size: {len(ranking)}\n"
        f"- pareto size: {pareto_n}\n"
        f"- flags: {', '.join(flags) if flags else 'none'}\n"
        f"\n## Top candidates\n"
        + ("\n".join(top_lines) if top_lines else "  (empty)")
        + "\n"
        + drift_block
    )

    return {
        "experiment_id": exp,
        "n_trials": n,
        "n_ranked": len(ranking),
        "n_pareto": pareto_n,
        "flags": flags,
        "top": ranking[:5],
        "drift": drift_result,
        "markdown": md,
        "summary": md.strip(),
    }


def mark_paper_candidates(
    ranked: Sequence[Dict[str, Any]],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Set ``paper_trading_observe=True`` on top_k candidates (not live).

    Returns new list of dicts (does not mutate originals).
    """
    top_k = max(0, int(top_k))
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(ranked or []):
        item = dict(row)
        item["paper_trading_observe"] = bool(i < top_k)
        out.append(item)
    return out


__all__ = ["build_research_summary", "mark_paper_candidates"]
