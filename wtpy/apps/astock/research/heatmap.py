# -*- coding: utf-8 -*-
"""Heatmap builders for research evaluation UI (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def build_heatmap(
    rows: Sequence[Dict[str, Any]],
    x_key: str,
    y_key: str,
    metric_key: str,
    *,
    x_values: Optional[Sequence[Any]] = None,
    y_values: Optional[Sequence[Any]] = None,
    reduce: str = "last",
) -> Dict[str, Any]:
    """Build a 2D heatmap matrix structure for UI.

    Returns::

        {
          "x_key", "y_key", "metric_key",
          "x_labels": [...],
          "y_labels": [...],
          "matrix": [[val|None, ...], ...],  # row-major over y then x
          "cells": {(y, x): value, ...},
          "shape": (n_y, n_x),
          "table": [{"y": ..., "x": ..., metric_key: ...}, ...],
        }
    """
    # collect axes
    if x_values is None:
        xs: List[Any] = []
        seen_x = set()
        for r in rows:
            if x_key not in r:
                continue
            xv = r[x_key]
            if xv not in seen_x:
                seen_x.add(xv)
                xs.append(xv)
        try:
            xs = sorted(xs)
        except TypeError:
            pass
    else:
        xs = list(x_values)

    if y_values is None:
        ys: List[Any] = []
        seen_y = set()
        for r in rows:
            if y_key not in r:
                continue
            yv = r[y_key]
            if yv not in seen_y:
                seen_y.add(yv)
                ys.append(yv)
        try:
            ys = sorted(ys)
        except TypeError:
            pass
    else:
        ys = list(y_values)

    # accumulate
    buckets: Dict[Tuple[Any, Any], List[float]] = {}
    for r in rows:
        if x_key not in r or y_key not in r:
            continue
        xv, yv = r[x_key], r[y_key]
        if metric_key not in r or r[metric_key] is None:
            m = r.get("metrics") or {}
            val = m.get(metric_key)
        else:
            val = r[metric_key]
        if val is None:
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        buckets.setdefault((yv, xv), []).append(fv)

    def _reduce(vals: List[float]) -> float:
        if reduce == "mean":
            return sum(vals) / len(vals)
        if reduce == "max":
            return max(vals)
        if reduce == "min":
            return min(vals)
        return vals[-1]  # last

    cells: Dict[Tuple[Any, Any], Any] = {}
    matrix: List[List[Any]] = []
    for y in ys:
        row_vals: List[Any] = []
        for x in xs:
            key = (y, x)
            if key in buckets and buckets[key]:
                v = _reduce(buckets[key])
                cells[key] = v
                row_vals.append(v)
            else:
                cells[key] = None
                row_vals.append(None)
        matrix.append(row_vals)

    table: List[Dict[str, Any]] = []
    for y in ys:
        for x in xs:
            table.append({"y": y, "x": x, y_key: y, x_key: x, metric_key: cells.get((y, x))})

    return {
        "x_key": x_key,
        "y_key": y_key,
        "metric_key": metric_key,
        "x_labels": xs,
        "y_labels": ys,
        "matrix": matrix,
        "cells": cells,
        "shape": (len(ys), len(xs)),
        "table": table,
    }


__all__ = ["build_heatmap"]
