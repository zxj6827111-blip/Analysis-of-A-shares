# -*- coding: utf-8 -*-
"""Phase-2: research parameter space expand / constraints / planner."""
from __future__ import annotations

import pytest

from wtpy.apps.astock.research.constraints import (
    filter_variants,
    summarize_rejections,
    validate_variant,
)
from wtpy.apps.astock.research.models import ParameterSpace
from wtpy.apps.astock.research.parameter_space import (
    PRESET_WEEKDAY_TEMPLATES,
    axes_from_legacy_templates,
    expand_axes,
    preset_735_hold_matrix,
)
from wtpy.apps.astock.research.planner import (
    DEFAULT_MAX_VARIANTS,
    plan_experiment,
)


def test_preset_735_hold_matrix_is_16():
    variants = preset_735_hold_matrix(rule_id="735", expand=True)
    assert isinstance(variants, list)
    assert len(variants) == 16

    exits = {(v["exit_weekday"], v["sell_on"]) for v in variants}
    assert exits == {
        (2, "open"),
        (2, "close"),
        (3, "open"),
        (3, "close"),
        (4, "open"),
        (4, "close"),
        (5, "open"),
        (5, "close"),
    }
    guas = {v["_meta"]["gua_key"] for v in variants}
    assert guas == {"none", "best3"}
    for v in variants:
        assert v["rule_ids"] == ["735"]
        assert v["signal_weekdays"] == [5]
        assert v["buy_weekday"] == 1
        assert v["buy_on"] == "open"
        assert v["stop_loss"] is None
        assert v["take_profit"] is None


def test_preset_735_space_without_expand():
    space = preset_735_hold_matrix(expand=False)
    assert isinstance(space, ParameterSpace)
    assert space.theoretical_count() == 16


def test_constraints_drop_entry_lag_0():
    raw = [
        {
            "rule_ids": ["735"],
            "entry_lag": 0,
            "buy_weekday": None,
            "buy_on": "open",
            "sell_on": "close",
            "hold": 1,
        }
    ]
    kept, rejected = filter_variants(raw)
    assert kept == []
    assert len(rejected) == 1
    assert "entry_lag_lt_1" in rejected[0]["_reject"]["codes"]
    summary = summarize_rejections(rejected)
    assert summary.get("entry_lag_lt_1") == 1


def test_constraints_invalid_buy_on():
    reasons = validate_variant(
        {
            "rule_ids": ["x"],
            "buy_on": "mid",
            "sell_on": "open",
            "entry_lag": 1,
        }
    )
    codes = {r["code"] for r in reasons}
    assert "invalid_buy_on" in codes


def test_constraints_stop_loss_range():
    reasons = validate_variant(
        {
            "rule_ids": ["x"],
            "entry_lag": 1,
            "buy_on": "open",
            "sell_on": "open",
            "stop_loss": 1.5,
        }
    )
    assert any(r["code"] == "invalid_stop_loss" for r in reasons)


def test_plan_preview_counts():
    space = ParameterSpace(
        rule_ids=["a", "b"],
        signal_weekdays=[None],
        buy_modes=[{"entry_lag": 1, "buy_on": "open"}],
        sell_modes=[{"hold": 1, "sell_on": "close"}],
        gua_keys=["none"],
        stop_loss_list=[None],
        take_profit_list=[None],
    )
    plan = plan_experiment(space, max_variants=50, force=False)
    assert plan["theoretical_count"] == 2
    assert plan["rejected_count"] == 0
    assert plan["actual_count"] == 2
    assert plan["error"] is None
    assert len(plan["preview"]) == 2
    assert len(plan["variants"]) == 2
    assert plan["truncated"] is False
    assert plan["max_variants"] == DEFAULT_MAX_VARIANTS or plan["max_variants"] == 50


def test_plan_truncates_without_force():
    space = ParameterSpace(
        rule_ids=[f"r{i}" for i in range(10)],
        buy_modes=[{"entry_lag": 1, "buy_on": "open"}],
        sell_modes=[{"hold": 1, "sell_on": "open"}],
        gua_keys=["none", "best3"],  # 10 * 2 = 20
    )
    plan = plan_experiment(space, max_variants=5, force=False)
    assert plan["actual_count"] == 20
    assert plan["truncated"] is True
    assert plan["error"]
    assert plan["variants"] == []
    assert len(plan["preview"]) == 20  # preview still filled up to 50

    plan_f = plan_experiment(space, max_variants=5, force=True)
    assert plan_f["error"] is None
    assert len(plan_f["variants"]) == 20


def test_legacy_axes_product_size():
    weekday_keys = list(PRESET_WEEKDAY_TEMPLATES.keys())
    gua_keys = ["none", "best3"]
    rule_ids = ["735", "x"]
    space = axes_from_legacy_templates(
        rule_ids=rule_ids,
        weekday_keys=weekday_keys,
        gua_keys=gua_keys,
        stop_loss_list=[None, 0.05],
    )
    variants = expand_axes(space)
    expected = len(rule_ids) * len(weekday_keys) * len(gua_keys) * 2
    assert len(variants) == expected
    # each has _meta weekday_key from template
    keys = {v["_meta"]["weekday_key"] for v in variants}
    assert keys == set(weekday_keys)


def test_missing_rule_ids_rejected():
    space = ParameterSpace(
        rule_ids=[],
        buy_modes=[{"entry_lag": 1, "buy_on": "open"}],
        sell_modes=[{"hold": 1, "sell_on": "open"}],
    )
    plan = plan_experiment(space)
    assert plan["actual_count"] == 0
    assert plan["rejected_count"] >= 1
    assert "missing_rule_ids" in plan["rejection_reasons"]


def test_fri_mon_weekday_path_kept():
    """Fri signal → Mon buy → Thu exit must not be over-filtered."""
    v = {
        "rule_ids": ["735"],
        "signal_weekdays": [5],
        "buy_weekday": 1,
        "exit_weekday": 4,
        "buy_on": "open",
        "sell_on": "open",
        "entry_lag": 1,
        "hold": 1,
    }
    assert validate_variant(v) == []
