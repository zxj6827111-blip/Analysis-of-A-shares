# -*- coding: utf-8 -*-
"""Phase-3 closeout tests: filter/execution cache, promote config, parquet fallback."""
from __future__ import annotations

import inspect
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.research.execution_cache import (
    execution_cache_key,
    load_execution_cache,
    save_execution_cache,
)
from wtpy.apps.astock.research.filter_cache import filter_cache_key, get_or_compute_filtered
from wtpy.apps.astock.research.parquet_io import write_events_parquet, write_records_parquet
from wtpy.apps.astock.research.signal_cache import get_or_compute_signals, signal_cache_key
from wtpy.apps.astock.service.experiments import ExperimentRunner, create_experiment_from_grid
from wtpy.apps.astock.study import SignalEvent


def _cfg(tmp: Path) -> AStockConfig:
    cfg = AStockConfig()
    cfg.storage_root = tmp / "st"
    cfg.output_root = tmp / "out"
    cfg.ensure_dirs()
    return cfg


def test_execution_cache_roundtrip(tmp_path: Path):
    cfg = _cfg(tmp_path)
    key = execution_cache_key({"a": 1, "engine": "fast"})
    save_execution_cache(key, metrics={"total_return": 0.12, "n_trades": 3}, cfg=cfg)
    hit = load_execution_cache(key, cfg=cfg)
    assert hit is not None
    assert hit["metrics"]["total_return"] == 0.12


def test_filter_cache_roundtrip(tmp_path: Path):
    cfg = _cfg(tmp_path)
    sk = signal_cache_key(
        indicator_ids=["r"],
        period="DAY",
        start=1,
        end=2,
        universe_hash="u",
        adjust_mode="adjusted",
    )
    fk = filter_cache_key(signal_cache_key=sk, gua_filter={"enabled": True, "selection_mode": "none"})
    n = {"c": 0}

    def compute():
        n["c"] += 1
        return [SignalEvent("SSE.STK.1", 20240105, "DAY", "r")]

    get_or_compute_filtered(fk, compute, cfg=cfg)
    get_or_compute_filtered(fk, compute, cfg=cfg)
    assert n["c"] == 1


def test_parquet_fallback_jsonl(tmp_path: Path):
    path = tmp_path / "events.parquet"
    out = write_records_parquet(path, [{"a": 1}, {"a": 2}])
    assert out.exists()
    # either parquet or jsonl
    assert out.suffix in (".parquet", ".jsonl")


def test_write_events_parquet(tmp_path: Path):
    out = write_events_parquet(
        tmp_path / "s.parquet",
        [SignalEvent("SSE.STK.1", 20240105, "DAY", "x")],
    )
    assert out.exists()


def test_create_experiment_promote_defaults(tmp_path: Path):
    sig = inspect.signature(create_experiment_from_grid)
    assert sig.parameters["promote_top_n"].default == 3
    assert sig.parameters["promote_metric"].default == "total_return"
    cfg = _cfg(tmp_path)
    exp = create_experiment_from_grid(
        cfg,
        name="p3-promote",
        rule_ids=["dummy"],
        gua_keys=["none"],
        weekday_keys=["all_signal_tn12"],
        codes=["sh600000"],
        start=20240101,
        end=20240110,
        max_variants=5,
        promote_top_n=2,
    )
    conf = exp.get("config") or {}
    assert conf.get("promote_top_n") == 2
    assert conf.get("engine") == "fast"
    assert hasattr(ExperimentRunner, "_promote_top_n_full")


def test_layered_cache_pipeline(tmp_path: Path):
    cfg = _cfg(tmp_path)
    sk = signal_cache_key(
        indicator_ids=["L"],
        period="DAY",
        start=None,
        end=None,
        universe_hash="u",
        adjust_mode="adjusted",
    )
    fk = filter_cache_key(signal_cache_key=sk, with_bagua=True, bagua_filter_mode="best3")
    c = {"s": 0, "f": 0}

    def raw():
        c["s"] += 1
        return [
            SignalEvent("SSE.STK.1", 20240105, "DAY", "L"),
            SignalEvent("SSE.STK.1", 20240108, "DAY", "L"),
        ]

    def filt():
        c["f"] += 1
        ev, _ = get_or_compute_signals(sk, raw, cfg=cfg)
        return [e for e in ev if e.date == 20240105]

    get_or_compute_filtered(fk, filt, cfg=cfg)
    get_or_compute_filtered(fk, filt, cfg=cfg)
    assert c["s"] == 1
    assert c["f"] == 1
