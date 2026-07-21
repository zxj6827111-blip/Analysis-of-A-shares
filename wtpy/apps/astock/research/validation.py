# -*- coding: utf-8 -*-
"""Train/test validation splits and OOS scoring (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def fixed_split(
    dates: Sequence[Any],
    train_end: Any,
    test_start: Any,
) -> Dict[str, List[Any]]:
    """Split date sequence into train / test by inclusive train_end and test_start.

    Dates equal to or before train_end go to train; dates on or after test_start
    go to test. Overlap/gap windows are allowed (caller controls boundary).
    """
    train: List[Any] = []
    test: List[Any] = []
    for d in dates:
        if d <= train_end:
            train.append(d)
        if d >= test_start:
            test.append(d)
    return {"train": train, "test": test, "train_end": train_end, "test_start": test_start}


def walk_forward_folds(
    start: int,
    end: int,
    train_years: int,
    test_years: int,
    step_years: int = 1,
) -> List[Dict[str, int]]:
    """Generate walk-forward year folds.

    ``start``/``end`` are calendar years (inclusive end meaning last usable year).
    Each fold: train [train_start, train_end], test [test_start, test_end]
    with lengths train_years / test_years, advancing by step_years.
    """
    if train_years < 1 or test_years < 1:
        raise ValueError("train_years and test_years must be >= 1")
    step = max(1, int(step_years))
    folds: List[Dict[str, int]] = []
    train_start = int(start)
    while True:
        train_end = train_start + int(train_years) - 1
        test_start = train_end + 1
        test_end = test_start + int(test_years) - 1
        if test_end > int(end):
            break
        folds.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        train_start += step
    return folds


DEFAULT_HARD_GATES: Dict[str, Any] = {
    "min_trades": 10,
    "max_drawdown": 0.35,  # fraction, e.g. 0.35 = 35%
    "min_out_windows_profit_frac": 0.5,
}


def score_in_out(
    in_metrics: Dict[str, Any],
    out_metrics: Dict[str, Any],
    *,
    hard_gates: Optional[Dict[str, Any]] = None,
    prefer_oos: bool = True,
    oos_weight: float = 0.7,
) -> Dict[str, Any]:
    """Score in-sample vs out-of-sample metrics with optional hard gates.

    Returns ``{in_score, out_score, decay, pass_hard_gate, gate_reasons}``.
    When prefer_oos, composite leans on out_score (higher oos_weight).
    """
    gates = dict(DEFAULT_HARD_GATES)
    if hard_gates:
        gates.update(hard_gates)

    def _score(m: Dict[str, Any]) -> float:
        tr = float(m.get("total_return") or m.get("return") or 0.0)
        sharpe = float(m.get("sharpe") or m.get("sharpe_like") or 0.0)
        wr = float(m.get("win_rate") or 0.0)
        dd = abs(float(m.get("max_drawdown") or m.get("drawdown") or 0.0))
        # higher better; penalize drawdown
        return tr + 0.5 * sharpe + 0.2 * wr - 0.8 * dd

    in_score = _score(in_metrics or {})
    out_score = _score(out_metrics or {})
    # decay: how much OOS underperforms IS (positive = decay / overfit signal)
    if abs(in_score) < 1e-12:
        decay = 0.0 if abs(out_score) < 1e-12 else (1.0 if out_score < in_score else -1.0)
    else:
        decay = (in_score - out_score) / max(abs(in_score), 1e-12)

    reasons: List[str] = []
    ok = True

    n_trades = int(
        (out_metrics or {}).get("n_trades")
        or (out_metrics or {}).get("trade_count")
        or (out_metrics or {}).get("trades")
        or 0
    )
    min_trades = int(gates.get("min_trades", 0))
    if n_trades < min_trades:
        ok = False
        reasons.append(f"too_few_trades:{n_trades}<{min_trades}")

    dd = abs(
        float(
            (out_metrics or {}).get("max_drawdown")
            or (out_metrics or {}).get("drawdown")
            or 0.0
        )
    )
    max_dd = float(gates.get("max_drawdown", 1.0))
    if dd > max_dd:
        ok = False
        reasons.append(f"excessive_drawdown:{dd}>{max_dd}")

    profit_frac = (out_metrics or {}).get("out_windows_profit_frac")
    if profit_frac is None:
        profit_frac = (out_metrics or {}).get("windows_profit_frac")
    if profit_frac is not None:
        pf = float(profit_frac)
        min_pf = float(gates.get("min_out_windows_profit_frac", 0.0))
        if pf < min_pf:
            ok = False
            reasons.append(f"low_out_windows_profit_frac:{pf}<{min_pf}")

    combined = (
        oos_weight * out_score + (1.0 - oos_weight) * in_score
        if prefer_oos
        else 0.5 * out_score + 0.5 * in_score
    )

    return {
        "in_score": in_score,
        "out_score": out_score,
        "decay": decay,
        "pass_hard_gate": ok,
        "gate_reasons": reasons,
        "combined_score": combined,
        "prefer_oos": prefer_oos,
    }


__all__ = [
    "fixed_split",
    "walk_forward_folds",
    "score_in_out",
    "DEFAULT_HARD_GATES",
]
