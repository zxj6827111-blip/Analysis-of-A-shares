# -*- coding: utf-8 -*-
"""Continuous research facade: budgeted search + scheduled enqueue (Phase 6)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .optimizer import grid_search, random_search, staged_search
from .schedules import ScheduleRunner, get_schedule


def run_budgeted_search(
    space: Dict[str, list],
    method: str = "random",
    budget: int = 20,
    seed: int = 0,
    evaluate_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> List[Dict[str, Any]]:
    """Run a budgeted parameter search; optionally score each trial with evaluate_fn.

    Returns list of trial dicts: ``{params, metrics?, score?}``.
    """
    method = (method or "random").strip().lower()
    budget = max(0, int(budget))
    if budget == 0:
        return []

    if method in ("grid", "grid_search"):
        params_list = grid_search(space, budget=budget, seed=seed)
    elif method in ("staged", "staged_search"):
        # split budget ~ 60/40 coarse/fine
        coarse = max(1, int(budget * 0.6)) if budget > 1 else budget
        fine = max(0, budget - coarse)
        score_fn = None
        if evaluate_fn is not None:

            def score_fn(p: Dict[str, Any]) -> float:
                r = evaluate_fn(p)
                if isinstance(r, dict):
                    if "score" in r:
                        return float(r["score"])
                    if "total_return" in r:
                        return float(r["total_return"])
                    m = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
                    return float(m.get("total_return") or 0.0)
                try:
                    return float(r)
                except (TypeError, ValueError):
                    return 0.0

        params_list = staged_search(
            space,
            coarse_budget=coarse,
            fine_budget=fine,
            seed=seed,
            score_fn=score_fn,
        )
        # ensure <= budget
        params_list = params_list[:budget]
    elif method in ("random", "random_search"):
        params_list = random_search(space, n=budget, seed=seed)
    else:
        raise ValueError(f"unknown search method: {method}")

    trials: List[Dict[str, Any]] = []
    for i, params in enumerate(params_list):
        trial: Dict[str, Any] = {"id": f"t{i}", "params": dict(params), **dict(params)}
        if evaluate_fn is not None:
            result = evaluate_fn(params)
            if isinstance(result, dict):
                trial["metrics"] = result.get("metrics", result)
                if "score" in result:
                    trial["score"] = result["score"]
                for k in ("total_return", "max_drawdown", "win_rate", "n_trades"):
                    if k in result and k not in trial:
                        trial[k] = result[k]
            else:
                trial["score"] = result
                trial["metrics"] = {"score": result}
        trials.append(trial)
    return trials


def run_scheduled_research(
    schedule_name: str,
    platform: Any,
    handler: Optional[Callable[[dict], Any]] = None,
    *,
    n: Optional[int] = None,
    experiment_id: Optional[str] = None,
    base_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Enqueue research tasks under the named schedule budget."""
    sched = get_schedule(schedule_name)
    runner = ScheduleRunner(platform, schedule_name)
    enqueued = runner.enqueue_dummy(
        n=n,
        experiment_id=experiment_id,
        base_params=base_params,
        handler=handler,
    )
    return {
        "ok": True,
        "schedule": sched,
        "enqueued": len(enqueued),
        "results": enqueued,
    }


__all__ = ["run_budgeted_search", "run_scheduled_research"]
