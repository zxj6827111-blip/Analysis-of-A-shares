# -*- coding: utf-8 -*-
"""Parameter search / optimizer helpers (Phase 6).

Pure search utilities: grid, random, staged. Optuna is optional.
"""
from __future__ import annotations

import itertools
import random
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


def _axes_items(space_axes: Dict[str, list]) -> List[Tuple[str, list]]:
    """Stable key order for deterministic expansion."""
    return sorted((str(k), list(v) if v is not None else []) for k, v in (space_axes or {}).items())


def expand_cartesian(space_axes: Dict[str, list]) -> List[Dict[str, Any]]:
    """Full cartesian product of axes (deterministic key order)."""
    items = _axes_items(space_axes)
    if not items:
        return [{}]
    keys = [k for k, _ in items]
    values = [vals if vals else [None] for _, vals in items]
    out: List[Dict[str, Any]] = []
    for combo in itertools.product(*values):
        out.append(dict(zip(keys, combo)))
    return out


def grid_search(
    space_axes: Dict[str, list],
    budget: Optional[int] = None,
    seed: int = 0,
) -> List[Dict[str, Any]]:
    """Expand cartesian product; truncate to budget with deterministic order when seed fixed.

    Order is always the sorted-key cartesian order. When ``budget`` is set and
    smaller than the full product, a seeded permutation of the first
    ``min(len, budget * 4 + 1)`` candidates is not used — instead we take a
    deterministic subsample: shuffle full list with ``seed`` then take ``budget``.
    When budget is None or >= len, return full product (order independent of seed
    for full product; for truncation seed controls which subset).
    """
    full = expand_cartesian(space_axes)
    if budget is None or budget >= len(full):
        return full
    if budget <= 0:
        return []
    rng = random.Random(int(seed))
    order = list(range(len(full)))
    rng.shuffle(order)
    return [full[i] for i in order[:budget]]


def random_search(
    space_axes: Dict[str, list],
    n: int,
    seed: int = 0,
) -> List[Dict[str, Any]]:
    """Sample ``n`` parameter dicts without requiring optuna.

    Samples independently per axis with replacement-style draws; dedupes
    while trying to reach ``n`` (up to a bounded number of attempts).
    """
    n = int(n)
    if n <= 0:
        return []
    items = _axes_items(space_axes)
    if not items:
        return [{}] * min(1, n) if n else []
    keys = [k for k, _ in items]
    values = [vals if vals else [None] for _, vals in items]
    rng = random.Random(int(seed))
    seen: set = set()
    out: List[Dict[str, Any]] = []
    max_attempts = max(n * 20, n + 10)
    attempts = 0
    while len(out) < n and attempts < max_attempts:
        attempts += 1
        combo = tuple(rng.choice(vals) for vals in values)
        if combo in seen:
            continue
        seen.add(combo)
        out.append(dict(zip(keys, combo)))
    # If space smaller than n, pad by cycling full product deterministically
    if len(out) < n:
        full = expand_cartesian(space_axes)
        if not full:
            return out
        i = 0
        while len(out) < n:
            out.append(dict(full[i % len(full)]))
            i += 1
    return out


def _neighbor(
    point: Dict[str, Any],
    space_axes: Dict[str, list],
    rng: random.Random,
) -> Dict[str, Any]:
    """Perturb one random axis to an adjacent discrete value when possible."""
    items = _axes_items(space_axes)
    if not items:
        return dict(point)
    axis_key, vals = rng.choice(items)
    if not vals:
        return dict(point)
    cur = point.get(axis_key)
    try:
        idx = vals.index(cur)
    except ValueError:
        idx = 0
    if len(vals) == 1:
        return dict(point)
    # step ±1 or jump
    step = rng.choice([-1, 1, rng.randint(0, len(vals) - 1) - idx])
    nidx = max(0, min(len(vals) - 1, idx + step))
    if nidx == idx:
        nidx = (idx + 1) % len(vals)
    out = dict(point)
    out[axis_key] = vals[nidx]
    return out


def staged_search(
    space_axes: Dict[str, list],
    coarse_budget: int,
    fine_budget: int,
    seed: int = 0,
    score_fn: Optional[Callable[[Dict[str, Any]], float]] = None,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """Coarse random search then refine neighbors of top-k.

    Returns list of param dicts with length <= coarse_budget + fine_budget.
    If ``score_fn`` is None, uses a placeholder score of 0 (still returns
    coarse + neighbor samples, budget capped).
    """
    coarse_budget = max(0, int(coarse_budget))
    fine_budget = max(0, int(fine_budget))
    total_cap = coarse_budget + fine_budget
    if total_cap <= 0:
        return []

    coarse = random_search(space_axes, coarse_budget, seed=seed)
    scorer = score_fn or (lambda _p: 0.0)
    scored = [(float(scorer(p)), i, p) for i, p in enumerate(coarse)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    k = max(1, min(int(top_k), len(scored))) if scored else 0
    elites = [p for _, _, p in scored[:k]] if k else []

    rng = random.Random(int(seed) + 17)
    refined: List[Dict[str, Any]] = []
    seen = {tuple(sorted((str(k), str(v)) for k, v in p.items())) for p in coarse}
    attempts = 0
    while len(refined) < fine_budget and elites and attempts < fine_budget * 30:
        attempts += 1
        base = elites[rng.randrange(len(elites))]
        nb = _neighbor(base, space_axes, rng)
        key = tuple(sorted((str(k), str(v)) for k, v in nb.items()))
        if key in seen:
            continue
        seen.add(key)
        refined.append(nb)

    out = list(coarse) + refined
    if len(out) > total_cap:
        out = out[:total_cap]
    return out


def optuna_search(
    space_axes: Dict[str, list],
    n_trials: int,
    seed: int = 0,
    objective: Optional[Callable[[Dict[str, Any]], float]] = None,
    direction: str = "maximize",
) -> List[Dict[str, Any]]:
    """Thin optuna wrapper. Raises ImportError if optuna is not installed."""
    try:
        import optuna
    except ImportError as e:
        raise ImportError(
            "optuna is not installed; install optuna to use optuna_search, "
            "or use grid_search / random_search / staged_search instead"
        ) from e

    items = _axes_items(space_axes)
    if objective is None:
        objective = lambda _p: 0.0

    def _obj(trial: "optuna.Trial") -> float:
        params: Dict[str, Any] = {}
        for key, vals in items:
            if not vals:
                params[key] = None
                continue
            # categorical over discrete axis values
            params[key] = trial.suggest_categorical(key, vals)
        return float(objective(params))

    sampler = optuna.samplers.TPESampler(seed=int(seed))
    study = optuna.create_study(direction=direction, sampler=sampler)
    study.optimize(_obj, n_trials=int(n_trials), show_progress_bar=False)
    results: List[Dict[str, Any]] = []
    for t in study.trials:
        if t.params:
            results.append(dict(t.params))
    return results


__all__ = [
    "expand_cartesian",
    "grid_search",
    "random_search",
    "staged_search",
    "optuna_search",
]
