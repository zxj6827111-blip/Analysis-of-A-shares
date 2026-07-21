# -*- coding: utf-8 -*-
"""Phase-3: experiment defaults + signal cache wiring checks."""
from __future__ import annotations

import inspect
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.backtest import BacktestRequest
from wtpy.apps.astock.service.experiments import create_experiment_from_grid
from wtpy.apps.astock.study import SignalEvent
from wtpy.apps.astock.research.signal_cache import (
    get_or_compute_signals,
    signal_cache_key,
)


def test_create_experiment_defaults_fast_summary_cache():
    sig = inspect.signature(create_experiment_from_grid)
    assert sig.parameters["engine"].default == "fast"
    assert sig.parameters["artifact_level"].default == "summary"
    assert sig.parameters["use_signal_cache"].default is True


def test_backtest_request_phase3_fields():
    r = BacktestRequest(rule_ids=["735"])
    assert r.engine == "full"  # single backtest default remains full
    assert r.use_signal_cache is False
    assert r.artifact_level == "full"
    r2 = BacktestRequest(
        rule_ids=["735"], engine="fast", artifact_level="summary", use_signal_cache=True
    )
    assert r2.engine == "fast" and r2.use_signal_cache is True


def test_create_experiment_stores_engine_in_config(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.output_root = tmp_path / "out"
    cfg.ensure_dirs()
    exp = create_experiment_from_grid(
        cfg,
        name="p3-defaults",
        rule_ids=["dummy_rule"],
        gua_keys=["none"],
        weekday_keys=["all_signal_tn12"],
        codes=["sh600000"],
        start=20240101,
        end=20240131,
        max_variants=10,
    )
    conf = exp.get("config") or {}
    assert conf.get("engine") == "fast"
    assert conf.get("artifact_level") == "summary"
    assert conf.get("use_signal_cache") is True


def test_signal_cache_roundtrip_still(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.ensure_dirs()
    key = signal_cache_key(
        indicator_ids=["a"],
        period="DAY",
        start=1,
        end=2,
        universe_hash="u",
        adjust_mode="adjusted",
    )
    n = {"c": 0}

    def compute():
        n["c"] += 1
        return [SignalEvent("SSE.STK.1", 20240105, "DAY", "a")]

    get_or_compute_signals(key, compute, cfg=cfg)
    get_or_compute_signals(key, compute, cfg=cfg)
    assert n["c"] == 1
