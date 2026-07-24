# -*- coding: utf-8 -*-
"""Yao experiment center gaps: hold merge, custom gua_filters, universe, periods, manifest."""
from __future__ import annotations

from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.experiments import (
    HARD_MAX_VARIANTS,
    create_experiment_from_grid,
    estimate_grid_from_payload,
    expand_param_grid,
    expand_param_grid_unified,
)
from wtpy.apps.astock.service.yao_rules import (
    DEMO_CODES,
    HOLD_TEMPLATE_DAYS,
    hold_sell_options,
    load_yao_manifest,
    manifest_rules,
    single_state_gua_option,
)


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "outputs"
    c.storage_root = tmp_path / "storage"
    c.output_root.mkdir(parents=True, exist_ok=True)
    c.storage_root.mkdir(parents=True, exist_ok=True)
    return c


def test_hold_merge_prefers_sell_hold():
    plan = expand_param_grid_unified(
        rule_ids=["r1"],
        gua_keys=["none"],
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"hold": h, "sell_on": "close"} for h in [3, 5, 20, 60]],
        codes=DEMO_CODES,
        start=20160104,
        end=20161231,
    )
    holds = [v["hold"] for v in plan["variants"]]
    assert holds == [3, 5, 20, 60]


def test_hold_sell_options_templates():
    opts = hold_sell_options(sell_on="close")
    assert len(opts) == len(HOLD_TEMPLATE_DAYS)
    assert {o["hold"] for o in opts} == set(HOLD_TEMPLATE_DAYS)
    assert all(o["sell_on"] == "close" for o in opts)


def test_custom_gua_filter_exact_line():
    gf = single_state_gua_option("24-1", label="复之坤")
    plan = expand_param_grid_unified(
        rule_ids=["r1"],
        gua_keys=[gf],
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"hold": 5, "sell_on": "close"}],
        codes=DEMO_CODES,
    )
    assert plan["actual"] == 1
    v = plan["variants"][0]
    assert v["hold"] == 5
    assert v["with_bagua"] is True
    assert v["gua_filter"]["selected_state_ids"] == ["24-1"]


def test_periods_axis_day_week():
    plan = expand_param_grid_unified(
        rule_ids=["r1"],
        gua_keys=["none"],
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"hold": 5, "sell_on": "close"}],
        periods=["DAY", "WEEK"],
        codes=DEMO_CODES,
    )
    assert plan["actual"] == 2
    assert sorted(v["period"] for v in plan["variants"]) == ["DAY", "WEEK"]


def test_estimate_with_gua_filters_and_periods():
    gf = single_state_gua_option("11-1", label="泰之升")
    est = estimate_grid_from_payload(
        {
            "rule_ids": ["r1"],
            "gua_keys": ["none"],
            "gua_filters": [gf],
            "signal_weekdays_options": [None],
            "buy_options": [{"entry_lag": 1, "buy_on": "open"}],
            "sell_options": hold_sell_options([5, 20], sell_on="close"),
            "periods": ["DAY"],
            "max_variants": 50,
            "force": True,
            "codes": DEMO_CODES,
        }
    )
    # 1 rule * (none + filter) * 1 * 1 * 2 holds * 1 period = 4
    assert est["actual"] == 4
    assert est["ok"] is True
    assert est["mode"] == "free_axes"


def test_create_experiment_demo_universe_and_filters(cfg: AStockConfig):
    gf = single_state_gua_option("24-1", label="复之坤")
    exp = create_experiment_from_grid(
        cfg,
        name="yao-demo",
        rule_ids=["r1"],
        gua_keys=["none"],
        gua_filters=[gf],
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"hold": 5, "sell_on": "close"}],
        universe="demo",
        periods=["DAY"],
        max_variants=50,
        force=True,
        start=20160104,
        end=20160301,
    )
    assert exp["actual"] == 2
    assert exp["config"]["universe"] == "demo"
    assert exp["config"]["n_codes"] == 2
    holds = []
    states = []
    for row in exp["variants"]:
        params = row.get("params") or row
        holds.append(params.get("hold"))
        gf0 = params.get("gua_filter") or {}
        states.append(tuple(gf0.get("selected_state_ids") or []))
    assert holds == [5, 5]
    assert ("24-1",) in states
    assert () in states or ("",) not in states


def test_create_unknown_gua_preset_rejected():
    with pytest.raises(ValueError, match="unknown gua"):
        expand_param_grid_unified(
            rule_ids=["r1"],
            gua_keys=["not_a_real_preset"],
            signal_weekdays_options=[None],
            buy_options=[{"entry_lag": 1, "buy_on": "open"}],
            sell_options=[{"hold": 1, "sell_on": "close"}],
        )


def test_manifest_has_confirmed_rules():
    man = load_yao_manifest()
    assert man.get("exists") is True
    confirmed = manifest_rules(status=["confirmed"])
    assert len(confirmed) >= 5
    assert all(r.get("state_id") for r in confirmed)


def test_ui_has_hold_and_universe_controls():
    html = (
        Path(__file__).resolve().parents[3]
        / "wtpy"
        / "apps"
        / "astock"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert "hold:3:close" in html
    assert "hold:60:close" in html
    assert "hold:120:close" in html
    assert 'name="expUniverse"' in html
    assert "expYaoRuleChips" in html
    assert "gua_filters" in html
    assert "btnExpHoldGrid" in html
    assert "/api/v1/yao/rules" in html


def test_expand_param_grid_passes_filters():
    gf = single_state_gua_option("24-1", label="复之坤")
    variants = expand_param_grid(
        rule_ids=["r1"],
        gua_keys=["none"],
        gua_filters=[gf],
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"hold": 5, "sell_on": "close"}],
        codes=DEMO_CODES,
    )
    assert len(variants) == 2
    assert HARD_MAX_VARIANTS == 500
