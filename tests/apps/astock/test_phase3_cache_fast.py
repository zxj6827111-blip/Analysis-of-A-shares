# -*- coding: utf-8 -*-
"""Phase-3: signal/filter cache + fast engine unit tests."""
from __future__ import annotations

import tempfile
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.research.artifacts import apply_artifact_policy, normalize_artifact_level
from wtpy.apps.astock.research.executor import cached_signal_pipeline, run_engine
from wtpy.apps.astock.research.fast_engine import run_fast_backtest
from wtpy.apps.astock.research.filter_cache import filter_cache_key, get_or_compute_filtered
from wtpy.apps.astock.research.signal_cache import (
    get_or_compute_signals,
    load_signal_cache,
    signal_cache_key,
)
from wtpy.apps.astock.study import SignalEvent


def _cfg_tmp(tmp: Path) -> AStockConfig:
    cfg = AStockConfig()
    cfg.storage_root = tmp / "storage"
    cfg.output_root = tmp / "out"
    cfg.ensure_dirs()
    return cfg


def test_signal_cache_hit_second_call(tmp_path: Path):
    cfg = _cfg_tmp(tmp_path)
    key = signal_cache_key(
        indicator_ids=["735"],
        period="DAY",
        start=20240101,
        end=20240131,
        universe_hash="u1",
        adjust_mode="adjusted",
    )
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return [SignalEvent("SSE.STK.600000", 20240105, "DAY", "735")]

    e1, hit1 = get_or_compute_signals(key, compute, cfg=cfg, use_cache=True)
    e2, hit2 = get_or_compute_signals(key, compute, cfg=cfg, use_cache=True)
    assert hit1 is False and hit2 is True
    assert calls["n"] == 1
    assert len(e1) == 1 and len(e2) == 1
    assert e2[0].date == 20240105
    assert load_signal_cache(key, cfg=cfg) is not None



def test_signal_cache_key_includes_factor_manifest():
    """P1: factor change must not hit stale standard_qfq signal cache."""
    base = dict(
        indicator_ids=["735"],
        period="DAY",
        start=20240101,
        end=20240131,
        universe_hash="u1",
        adjust_mode="standard_qfq",
    )
    k1 = signal_cache_key(**base, factor_manifest_sha="aaa")
    k2 = signal_cache_key(**base, factor_manifest_sha="bbb")
    k3 = signal_cache_key(**base, factor_manifest_sha="aaa")
    k_empty = signal_cache_key(**base)
    k_empty2 = signal_cache_key(**base, factor_manifest_sha=None)
    assert k1 != k2
    assert k1 == k3
    assert k_empty == k_empty2
    assert k_empty != k1


def test_filter_cache_depends_on_signal_key(tmp_path: Path):
    cfg = _cfg_tmp(tmp_path)
    sk = signal_cache_key(
        indicator_ids=["x"],
        period="DAY",
        start=None,
        end=None,
        universe_hash="u",
        adjust_mode="adjusted",
    )
    fk = filter_cache_key(signal_cache_key=sk, signal_weekdays=[5], gua_filter={"enabled": False})
    n = {"c": 0}

    def filt():
        n["c"] += 1
        return [SignalEvent("SSE.STK.1", 20240112, "DAY", "x")]

    a, h1 = get_or_compute_filtered(fk, filt, cfg=cfg)
    b, h2 = get_or_compute_filtered(fk, filt, cfg=cfg)
    assert h1 is False and h2 is True and n["c"] == 1
    assert a[0].date == b[0].date


def test_cached_pipeline_two_layers(tmp_path: Path):
    cfg = _cfg_tmp(tmp_path)
    sk = "sig_test_key_aaaaaaaaaaaaaaaaaaaaaaaa"
    fk = "flt_test_key_bbbbbbbbbbbbbbbbbbbbbbbb"

    def raw():
        return [
            SignalEvent("SSE.STK.1", 20240105, "DAY", "r"),  # Fri
            SignalEvent("SSE.STK.1", 20240108, "DAY", "r"),  # Mon
        ]

    def apply(events):
        return [e for e in events if e.date == 20240105]

    r1 = cached_signal_pipeline(
        cfg=cfg,
        signal_key=sk,
        filter_key=fk,
        compute_raw_signals=raw,
        apply_filters=apply,
        use_cache=True,
    )
    r2 = cached_signal_pipeline(
        cfg=cfg,
        signal_key=sk,
        filter_key=fk,
        compute_raw_signals=raw,
        apply_filters=apply,
        use_cache=True,
    )
    assert r1["signal_cache_hit"] is False
    assert r2["signal_cache_hit"] is True
    assert r2["filter_cache_hit"] is True
    assert r1["n_raw"] == 2 and r1["n_filtered"] == 1


def test_fast_engine_basic_tn_path():
    code = "SSE.STK.600000"
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    bars = {
        code: [
            DayBar(20240102, 10, 11, 9, 10, 1, 1),
            DayBar(20240103, 10, 11, 9, 10.5, 1, 1),  # signal
            DayBar(20240104, 11, 12, 10, 11, 1, 1),  # entry open 11
            DayBar(20240105, 12, 13, 11, 12, 1, 1),  # exit open 12
            DayBar(20240108, 12, 13, 11, 12, 1, 1),
        ]
    }
    cal = TradeCalendar(dates)
    res = run_fast_backtest(
        [SignalEvent(code, 20240103, "DAY", "t")],
        bars,
        cal,
        hold=1,
        entry_lag=1,
        buy_on="open",
        sell_on="open",
    )
    assert res.n_trades == 1
    t = res.trades[0]
    assert t.entry_date == 20240104
    assert t.exit_date == 20240105
    assert abs(t.ret - (12 / 11 - 1)) < 1e-9
    assert res.metrics["n_trades"] == 1
    assert res.metrics["win_rate"] == 1.0


def test_fast_engine_weekday_exit():
    code = "SSE.STK.600000"
    # Wed signal 1/3, Mon buy 1/8, Fri exit 1/12
    dates = [20240103, 20240104, 20240105, 20240108, 20240109, 20240110, 20240111, 20240112]
    bars = {
        code: [DayBar(d, 10 + i * 0.1, 12, 9, 10.5, 1, 1) for i, d in enumerate(dates)]
    }
    bars[code][3] = DayBar(20240108, 12.0, 13, 11, 12.5, 1, 1)
    bars[code][7] = DayBar(20240112, 14.0, 15, 13, 14.5, 1, 1)
    cal = TradeCalendar(dates)
    res = run_fast_backtest(
        [SignalEvent(code, 20240103, "DAY", "t")],
        bars,
        cal,
        hold=1,
        entry_lag=1,
        buy_weekday=1,
        exit_weekday=5,
        buy_on="open",
        sell_on="open",
    )
    assert res.n_trades == 1
    assert res.trades[0].entry_date == 20240108
    assert res.trades[0].exit_date == 20240112
    assert res.trades[0].exit_reason == "weekday_exit"


def test_run_engine_dispatch_fast():
    code = "SSE.STK.600000"
    dates = [20240102, 20240103, 20240104, 20240105]
    bars = {code: [DayBar(d, 10, 11, 9, 10.5, 1, 1) for d in dates]}
    bars[code][2] = DayBar(20240104, 11, 12, 10, 11, 1, 1)
    bars[code][3] = DayBar(20240105, 12, 13, 11, 12, 1, 1)
    cal = TradeCalendar(dates)
    out = run_engine(
        "fast",
        [SignalEvent(code, 20240103, "DAY", "t")],
        bars_by_code=bars,
        calendar=cal,
        hold=1,
        entry_lag=1,
    )
    assert out["engine"] == "fast"
    assert out["metrics"]["n_trades"] == 1


def test_artifact_level_policy():
    assert normalize_artifact_level("summary") == "summary"
    f = apply_artifact_policy(level="summary")
    assert f["write_excel"] is False and f["write_meta"] is True
    f2 = apply_artifact_policy(level="full")
    assert f2["write_signals"] is True
