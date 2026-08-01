# -*- coding: utf-8 -*-
"""Phase2 free-axes estimate/create API smoke tests."""
from __future__ import annotations

from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.experiments import (
    HARD_MAX_VARIANTS,
    DEFAULT_MAX_VARIANTS,
    _resolve_hard_max_variants,
    estimate_grid_from_payload,
    expand_param_grid,
    create_experiment_from_grid,
    estimate_grid_size,
)


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "outputs"
    c.storage_root = tmp_path / "storage"
    c.output_root.mkdir(parents=True, exist_ok=True)
    c.storage_root.mkdir(parents=True, exist_ok=True)
    return c


def test_hard_max_is_configurable(monkeypatch):
    assert DEFAULT_MAX_VARIANTS == 50
    assert 500 <= HARD_MAX_VARIANTS <= 20000

    monkeypatch.delenv("ASTOCK_EXP_HARD_MAX_VARIANTS", raising=False)
    assert _resolve_hard_max_variants() == 2000
    monkeypatch.setenv("ASTOCK_EXP_HARD_MAX_VARIANTS", "100")
    assert _resolve_hard_max_variants() == 500
    monkeypatch.setenv("ASTOCK_EXP_HARD_MAX_VARIANTS", "50000")
    assert _resolve_hard_max_variants() == 20000


def test_free_axes_estimate_preview_shape():
    payload = {
        "rule_ids": ["735"],
        "signal_weekdays_options": [[5], None],
        "buy_options": [
            {"buy_weekday": 1, "buy_on": "open"},
            {"entry_lag": 1, "buy_on": "open"},
        ],
        "sell_options": [
            {"exit_weekday": 2, "sell_on": "open"},
            {"exit_weekday": 2, "sell_on": "close"},
        ],
        "gua_keys": ["none", "best3"],
        "stop_loss_list": [None, 0.03],
        "take_profit_list": [None],
        "holiday_policy": "next_trading_day",
        "max_variants": 50,
        "force": False,
        "preview_only": True,
    }
    est = estimate_grid_from_payload(payload)
    assert est["mode"] == "free_axes"
    # 1 * 2 sig * 2 buy * 2 sell * 2 gua * 2 sl * 1 tp = 32
    assert est["theoretical"] == 32
    assert est["actual"] == 32
    assert est["estimated_variants"] == 32
    assert est["n"] == 32 and est["count"] == 32
    assert est["ok"] is True
    assert isinstance(est["preview"], list) and len(est["preview"]) == 32
    assert est["hard_max"] == HARD_MAX_VARIANTS
    assert est["max_variants"] == 50
    assert "rejection_reasons" in est


def test_free_axes_ignore_weekday_templates():
    variants = expand_param_grid(
        rule_ids=["r1"],
        gua_keys=["none"],
        weekday_keys=["fri_signal_mon_buy_thu_exit"],  # should be ignored
        signal_weekdays_options=[None],
        buy_options=[{"entry_lag": 1, "buy_on": "open"}],
        sell_options=[{"sell_on": "close"}],
        stop_loss_list=[None],
        take_profit_list=[None],
        codes=["sh600000"],
    )
    assert len(variants) == 1
    assert variants[0].get("buy_weekday") is None
    assert variants[0]["_meta"].get("weekday_label") in (None, "free_axes") or variants[
        0
    ]["_meta"].get("weekday_key") in (None, "")


def test_create_free_axes_experiment(cfg: AStockConfig):
    exp = create_experiment_from_grid(
        cfg,
        name="free-axes-demo",
        rule_ids=["735"],
        gua_keys=["none", "best3"],
        weekday_keys=["all_signal_tn12"],  # ignored
        signal_weekdays_options=[[5]],
        buy_options=[{"buy_weekday": 1, "buy_on": "open"}],
        sell_options=[
            {"exit_weekday": 2, "sell_on": "open"},
            {"exit_weekday": 2, "sell_on": "close"},
        ],
        stop_loss_list=[None],
        take_profit_list=[None],
        max_variants=50,
        codes=["sh600000"],
        start=20240101,
        end=20240131,
    )
    assert exp["actual"] == 4  # 1*1*1*2*2*1*1
    assert exp["estimated_variants"] == 4
    assert len(exp["variants"]) == 4
    assert exp["config"]["mode"] == "free_axes"
    assert exp["config"]["weekday_keys"] == []


def test_legacy_estimate_size_still_24():
    n = estimate_grid_size(
        ["r1", "r2"],
        ["none", "best3", "bull"],
        ["fri_signal_mon_buy_thu_exit", "all_signal_tn12"],
        [None, 0.03],
    )
    assert n == 24
