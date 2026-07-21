# -*- coding: utf-8 -*-
"""Phase-2 (P2.7): 735 hold matrix acceptance — 16 unique cells + result matrix."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.research.matrix import build_result_matrix, matrix_table_to_rows
from wtpy.apps.astock.research.parameter_space import (
    PRESET_735_EXIT_WEEKDAYS,
    PRESET_735_GUA_KEYS,
    PRESET_735_SELL_ONS,
    preset_735_hold_matrix,
)
from wtpy.apps.astock.research.planner import plan_experiment


def _triple(v: dict) -> tuple:
    meta = v.get("_meta") or {}
    gua = meta.get("gua_key") or (
        "best3" if v.get("with_bagua") else "none"
    )
    return (int(v["exit_weekday"]), str(v["sell_on"]), str(gua))


def test_preset_735_hold_matrix_exactly_16_unique_triples():
    cells = preset_735_hold_matrix()
    assert len(cells) == 16

    triples = [_triple(c) for c in cells]
    assert len(set(triples)) == 16

    expected = {
        (ew, so, gk)
        for ew in PRESET_735_EXIT_WEEKDAYS
        for so in PRESET_735_SELL_ONS
        for gk in PRESET_735_GUA_KEYS
    }
    assert set(triples) == expected

    for c in cells:
        assert c["signal_weekdays"] == [5]
        assert c["buy_weekday"] == 1
        assert c["buy_on"] == "open"


def test_plan_experiment_735_actual_count_16_rejected_0():
    plan = plan_experiment(preset_735_hold_matrix(expand=False))
    assert plan["actual_count"] == 16
    assert plan["rejected_count"] == 0
    assert plan["theoretical_count"] == 16
    assert plan.get("error") is None
    triples = {_triple(v) for v in (plan.get("variants") or [])}
    assert len(triples) == 16


def test_plan_experiment_rejects_invalid_entry_lag():
    from wtpy.apps.astock.research.models import ParameterSpace

    space = ParameterSpace(
        rule_ids=["735"],
        signal_weekdays=[[5]],
        buy_modes=[{"entry_lag": 0, "buy_on": "open"}],  # invalid
        sell_modes=[{"hold": 1, "sell_on": "open"}],
        gua_keys=["none"],
        stop_loss_list=[None],
        take_profit_list=[None],
    )
    plan = plan_experiment(space)
    assert plan["theoretical_count"] >= 1
    assert plan["rejected_count"] >= 1
    assert plan["actual_count"] == 0
    assert plan["rejection_reasons"]


def test_build_result_matrix_groups_none_vs_best3():
    rows = []
    for ew in (2, 3, 4, 5):
        for so in ("open", "close"):
            for gk, val in (("none", ew * 0.01), ("best3", ew * 0.01 + 0.5)):
                rows.append(
                    {
                        "exit_weekday": ew,
                        "sell_on": so,
                        "gua_key": gk,
                        "total_return": val,
                    }
                )
    assert len(rows) == 16

    m = build_result_matrix(rows, metric_key="total_return")
    assert m["metric_key"] == "total_return"
    assert "none" in m["columns"] and "best3" in m["columns"]
    assert len(m["row_order"]) == 8  # 4 exit × 2 sell_on
    assert len(m["table"]) == 8
    assert m["missing"] == []

    cell = m["cells"][(3, "open")]
    assert abs(cell["none"] - 0.03) < 1e-12
    assert abs(cell["best3"] - 0.53) < 1e-12

    table_row = next(
        r for r in m["table"] if r["exit_weekday"] == 3 and r["sell_on"] == "open"
    )
    assert abs(table_row["none"] - 0.03) < 1e-12
    assert abs(table_row["best3"] - 0.53) < 1e-12

    long_rows = matrix_table_to_rows(m)
    assert len(long_rows) == 16
