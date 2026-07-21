# -*- coding: utf-8 -*-
"""Industry/board cross-section helpers (no external industry DB).

Boards inferred from stock code via limit_rules.infer_board when available
(main / chinext / star; bj if code prefix matches).
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence


def _infer_board_fallback(std_code: str) -> str:
    code = str(std_code or "").split(".")[-1]
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("8", "4")) and len(code) == 6:
        # Beijing Stock Exchange style codes (e.g. 83xxxx / 43xxxx)
        return "bj"
    return "main"


def board_of_symbol(std_code: str) -> str:
    """Infer board from std_code using limit_rules.infer_board if available."""
    try:
        from ..data.limit_rules import infer_board

        b = infer_board(std_code)
        if b and b != "main":
            return str(b)
        # extend bj if limit_rules only returns main for BSE
        code = str(std_code or "").split(".")[-1]
        if code.startswith(("8", "4")) and len(code) == 6 and not code.startswith(
            ("300", "301", "688", "689")
        ):
            # 60xxxx/00xxxx are main; 8xxxxx/4xxxxx often BJ
            if code[0] in ("8", "4"):
                return "bj"
        return str(b or "main")
    except Exception:  # noqa: BLE001
        return _infer_board_fallback(std_code)


def _return_of(row: Dict[str, Any]) -> float:
    for k in ("total_return", "return", "ret"):
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                pass
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    for k in ("total_return", "return", "ret"):
        if k in metrics and metrics[k] is not None:
            try:
                return float(metrics[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def _symbol_of(row: Dict[str, Any]) -> str:
    for k in ("std_code", "code", "symbol"):
        if row.get(k):
            return str(row[k])
    return ""


def slice_metrics_by_board(
    symbol_metrics: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Group symbol metrics by board; each value has n, returns, median_return, pct_profitable."""
    buckets: Dict[str, List[float]] = {}
    for row in symbol_metrics or []:
        if not isinstance(row, dict):
            continue
        sym = _symbol_of(row)
        board = str(row.get("board") or board_of_symbol(sym) or "main")
        buckets.setdefault(board, []).append(_return_of(row))

    out: Dict[str, Dict[str, Any]] = {}
    for board, rets in sorted(buckets.items()):
        n = len(rets)
        profitable = sum(1 for r in rets if r > 0)
        med = float(statistics.median(rets)) if rets else 0.0
        out[board] = {
            "n": n,
            "returns": rets,
            "median_return": med,
            "mean_return": float(sum(rets) / n) if n else 0.0,
            "pct_profitable": float(profitable / n) if n else 0.0,
        }
    return out


def cross_section_summary(
    symbol_metrics: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate cross-section stats including board slices and top-5 concentration.

    concentration_top5: share of sum of positive returns held by top-5 symbols
    (or of absolute contribution if all non-positive).
    """
    rows = [r for r in (symbol_metrics or []) if isinstance(r, dict)]
    rets = [_return_of(r) for r in rows]
    n = len(rets)
    profitable = sum(1 for r in rets if r > 0)
    med = float(statistics.median(rets)) if rets else 0.0
    board_slices = slice_metrics_by_board(rows)

    # concentration: top 5 by return contribution among positives
    sorted_rets = sorted(rets, reverse=True)
    top5 = sorted_rets[:5]
    pos = [r for r in rets if r > 0]
    if pos:
        denom = sum(pos)
        num = sum(r for r in top5 if r > 0)
        concentration = float(num / denom) if denom else 0.0
    elif rets:
        # all non-positive: concentration of magnitude in worst/best abs
        abs_sorted = sorted((abs(r) for r in rets), reverse=True)
        denom = sum(abs_sorted) or 1.0
        concentration = float(sum(abs_sorted[:5]) / denom)
    else:
        concentration = 0.0

    return {
        "n": n,
        "pct_profitable": float(profitable / n) if n else 0.0,
        "median_return": med,
        "mean_return": float(sum(rets) / n) if n else 0.0,
        "board_slices": board_slices,
        "concentration_top5": concentration,
    }


__all__ = [
    "board_of_symbol",
    "slice_metrics_by_board",
    "cross_section_summary",
]
