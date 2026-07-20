"""Adjustment factor continuity, causal scale, formal gate, no look-ahead."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

import numpy as np
import pytest

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.data.adjustments import (
    align_factors_to_dates,
    apply_qfq,
    build_factor_series,
    causal_qfq_scale,
    formal_adjustment_ready,
    seed_factor_for_dates,
    FactorSeries,
)
from wtpy.apps.astock.study import day_bars_to_adj
from wtpy.apps.astock.data.tdx_reader import DayBar


def test_align_no_future_leak():
    events = {20240110: 1.1, 20240120: 1.2}
    dates = [20240105, 20240110, 20240115, 20240120, 20240125]
    arr = align_factors_to_dates(events, dates, seed_factor=1.0)
    assert arr[0] == 1.0
    assert arr[1] == 1.1
    assert arr[3] == 1.2


def test_prehistory_seed_not_default_one():
    events = {20150623: 0.471008, 20160623: 0.533312}
    dates = [20160622, 20160623, 20160624]
    seed, seed_date = seed_factor_for_dates(events, dates[0])
    assert seed_date == 20150623
    assert abs(seed - 0.471008) < 1e-9
    arr = align_factors_to_dates(events, dates, seed_factor=seed)
    assert abs(arr[0] - 0.471008) < 1e-9
    assert abs(arr[1] - 0.533312) < 1e-9
    expected = (15.72 * 0.533312) / (17.89 * 0.471008) - 1.0
    adj_ret = (15.72 * arr[1]) / (17.89 * arr[0]) - 1.0
    assert abs(adj_ret - expected) < 1e-9
    assert abs(expected - (-0.0051)) < 0.01


def test_causal_scale_ignores_future_last_factor():
    """Appending a future factor must not rewrite earlier adjusted prices."""
    hist = np.array([1.0, 1.0], dtype=float)
    with_future = np.array([1.0, 1.0, 1.25], dtype=float)
    s_hist = causal_qfq_scale(hist)
    s_full = causal_qfq_scale(with_future)
    assert np.allclose(s_hist, [1.0, 1.0])
    assert np.allclose(s_full[:2], s_hist)
    # classic factor[-1] would make hist opens 10 -> 8; causal keeps 10
    raw = {"open": np.array([10.0, 10.0]), "high": np.array([10.0, 10.0]),
           "low": np.array([10.0, 10.0]), "close": np.array([10.0, 10.0])}
    adj_hist = apply_qfq(raw, hist)
    adj_full_prefix = apply_qfq(
        {k: v for k, v in {
            "open": np.array([10.0, 10.0, 12.0]),
            "high": np.array([10.0, 10.0, 12.0]),
            "low": np.array([10.0, 10.0, 12.0]),
            "close": np.array([10.0, 10.0, 12.0]),
        }.items()},
        with_future,
    )
    assert abs(adj_hist["open"][0] - 10.0) < 1e-12
    assert abs(adj_full_prefix["open"][0] - 10.0) < 1e-12  # not 8.0


def test_day_bars_to_adj_shares_causal_scale():
    bars = [DayBar(20240102, 10, 11, 9, 10, 1, 1), DayBar(20240103, 10, 11, 9, 10, 1, 1)]
    fac = np.array([0.5, 0.5, 2.0])  # third is "future" not used for first two if sliced
    adj2 = day_bars_to_adj(bars, fac[:2])
    adj3_prefix = day_bars_to_adj(
        bars + [DayBar(20240104, 12, 13, 11, 12, 1, 1)], fac
    )[:2]
    assert abs(adj2[0].open - adj3_prefix[0].open) < 1e-9
    assert abs(adj2[0].open - 10.0) < 1e-9  # base=0.5 => scale=1


def test_qfq_ratio_matches_raw_factor_ratio():
    raw = {
        "close": np.array([17.89, 15.72]),
        "open": np.array([17.89, 15.72]),
        "high": np.array([17.89, 15.72]),
        "low": np.array([17.89, 15.72]),
    }
    fac = np.array([0.471008, 0.533312])
    adj = apply_qfq(raw, fac)
    ratio = adj["close"][1] / adj["close"][0] - 1.0
    expected = (15.72 * 0.533312) / (17.89 * 0.471008) - 1.0
    assert abs(ratio - expected) < 1e-9


def test_formal_rejects_force_identity(tmp_path):
    s = build_factor_series(
        "SSE.STK.TEST",
        [20200101, 20200102],
        adj_root=tmp_path,
        force_identity=True,
    )
    assert s.quality == "forced_identity"
    ok, msg = formal_adjustment_ready([s])
    assert not ok
    assert "force" in msg.lower() or "unavailable" in msg.lower()


def test_formal_rejects_source_identity():
    s = FactorSeries(
        "SSE.STK.1",
        [1],
        [1.0],
        source="identity",
        source_detail="manual",
        quality="no_events_identity",
        sha256="x",
    )
    ok, _ = formal_adjustment_ready([s])
    assert not ok


def test_formal_accepts_true_baostock_empty_events():
    s = FactorSeries(
        "SSE.STK.1",
        [1, 2],
        [1.0, 1.0],
        source="identity_no_events",
        source_detail="baostock_empty:sh.1",
        quality="no_events_identity",
        sha256="x",
    )
    ok, msg = formal_adjustment_ready([s])
    assert ok, msg


def test_formal_accepts_complete_baostock():
    s = FactorSeries(
        "SSE.STK.1",
        [1, 2],
        [0.5, 0.6],
        source="baostock",
        quality="complete",
        sha256="x",
        prehistory_factor=0.5,
    )
    ok, msg = formal_adjustment_ready([s])
    assert ok, msg


def test_formal_ready_rejects_incomplete():
    s = FactorSeries(
        "SSE.STK.1",
        [1],
        [1.0],
        source="baostock_incomplete_prehistory",
        quality="incomplete",
        sha256="x",
    )
    ok, _ = formal_adjustment_ready([s])
    assert not ok


@pytest.mark.skipif(
    not Path(r"D:\通达信\vipdoc\sh\lday\sh600000.day").exists(),
    reason="local TDX required for live 600000 regression",
)
def test_real_600000_no_fake_53pct_drop(tmp_path):
    cfg = get_default_config(storage_root=tmp_path / "storage")
    cfg.ensure_dirs()
    from wtpy.apps.astock.data.tdx_reader import parse_day_file

    bars, _ = parse_day_file(Path(r"D:\通达信\vipdoc\sh\lday\sh600000.day"))
    dates = [b.date for b in bars]
    series = build_factor_series(
        "SSE.STK.600000",
        dates,
        adj_root=cfg.adj_root,
        prefer_baostock=True,
        refresh=True,
    )
    assert series.quality == "complete", series.source_detail
    fmap = {d: f for d, f in zip(series.dates, series.factors)}
    d0, d1 = 20160622, 20160623
    if d0 not in fmap or d1 not in fmap:
        pytest.skip("dates not in local history")
    f0, f1 = fmap[d0], fmap[d1]
    c0 = next(b.close for b in bars if b.date == d0)
    c1 = next(b.close for b in bars if b.date == d1)
    raw_ret = c1 / c0 - 1.0
    adj_ret = (c1 * f1) / (c0 * f0) - 1.0
    assert abs(f0 - 0.471008) < 1e-5 or f0 < 0.6
    assert abs(adj_ret) < 0.05
    assert raw_ret < -0.1
    assert adj_ret > raw_ret
    # causal day_bars_to_adj must preserve same return
    i0 = dates.index(d0)
    i1 = dates.index(d1)
    fac = np.array(series.factors, dtype=float)
    adj_bars = day_bars_to_adj(bars, fac)
    r2 = adj_bars[i1].close / adj_bars[i0].close - 1.0
    assert abs(r2 - adj_ret) < 1e-6
