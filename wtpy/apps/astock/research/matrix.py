# -*- coding: utf-8 -*-
"""Result matrix views for multi-axis research experiments (P2.7)."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _cell_key(exit_weekday: Any, sell_on: Any) -> Tuple[int, str]:
    return (int(exit_weekday), str(sell_on))


def build_result_matrix(
    rows: Sequence[Dict[str, Any]],
    *,
    metric_key: str = "total_return",
    row_keys: Sequence[str] = ("exit_weekday", "sell_on"),
    column_key: str = "gua_key",
    column_values: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Group metric values into a matrix: rows = exit×sell_on, columns = gua.

    Each input row should include ``exit_weekday``, ``sell_on``, ``gua_key``
    (or the configured keys) and ``metric_key``.

    Returns a pure structure::

        {
          "metric_key": "...",
          "columns": ["none", "best3", ...],
          "row_order": [(exit_weekday, sell_on), ...],
          "cells": {
            (exit_weekday, sell_on): {"none": value, "best3": value, ...},
            ...
          },
          "table": [  # list form convenient for Excel / UI
            {"exit_weekday": ..., "sell_on": ..., "none": ..., "best3": ...},
            ...
          ],
          "missing": [{"exit_weekday", "sell_on", "gua_key"}, ...],
        }
    """
    if column_values is None:
        # Preserve first-seen order, then sort unknowns alphabetically at end
        seen_cols: List[str] = []
        for r in rows:
            ck = str(r.get(column_key) if r.get(column_key) is not None else "none")
            if ck not in seen_cols:
                seen_cols.append(ck)
        cols: List[str] = list(seen_cols) if seen_cols else ["none", "best3"]
    else:
        cols = [str(c) for c in column_values]

    rk0, rk1 = row_keys[0], row_keys[1]
    cells: Dict[Tuple[int, str], Dict[str, Any]] = {}
    row_order: List[Tuple[int, str]] = []
    missing: List[Dict[str, Any]] = []

    for r in rows:
        if rk0 not in r or rk1 not in r:
            missing.append({"row": dict(r), "reason": "missing_row_keys"})
            continue
        key = _cell_key(r[rk0], r[rk1])
        if key not in cells:
            cells[key] = {c: None for c in cols}
            row_order.append(key)
        gua = str(r.get(column_key) if r.get(column_key) is not None else "none")
        if gua not in cells[key]:
            cells[key][gua] = None
            if gua not in cols:
                cols.append(gua)
        if metric_key in r:
            cells[key][gua] = r[metric_key]
        else:
            missing.append(
                {
                    "exit_weekday": key[0],
                    "sell_on": key[1],
                    "gua_key": gua,
                    "reason": "missing_metric",
                }
            )

    # Stable sort by exit_weekday then sell_on (open before close if same day)
    sell_rank = {"open": 0, "close": 1}

    def _sort_key(t: Tuple[int, str]) -> Tuple[int, int, str]:
        return (t[0], sell_rank.get(t[1], 99), t[1])

    row_order = sorted(set(row_order), key=_sort_key)

    table: List[Dict[str, Any]] = []
    for ew, so in row_order:
        entry: Dict[str, Any] = {"exit_weekday": ew, "sell_on": so}
        for c in cols:
            entry[c] = cells.get((ew, so), {}).get(c)
        table.append(entry)

    return {
        "metric_key": metric_key,
        "columns": cols,
        "row_order": row_order,
        "cells": cells,
        "table": table,
        "missing": missing,
    }


def matrix_table_to_rows(
    matrix: Dict[str, Any],
    *,
    column_values: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Flatten matrix table back to long-form rows (for tests / export)."""
    cols = list(column_values) if column_values is not None else list(matrix.get("columns") or [])
    metric = matrix.get("metric_key") or "total_return"
    out: List[Dict[str, Any]] = []
    for row in matrix.get("table") or []:
        for c in cols:
            out.append(
                {
                    "exit_weekday": row.get("exit_weekday"),
                    "sell_on": row.get("sell_on"),
                    "gua_key": c,
                    metric: row.get(c),
                }
            )
    return out
