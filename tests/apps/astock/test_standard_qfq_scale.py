# -*- coding: utf-8 -*-
"""Unit tests for standard ordinary qfq vs point-in-time research scale."""
from __future__ import annotations

import numpy as np

from wtpy.apps.astock.data.adjustments import (
    causal_qfq_scale,
    point_in_time_adjusted_scale,
    standard_qfq_scale,
)
from wtpy.apps.astock.study import (
    day_bars_to_point_in_time_adjusted,
    day_bars_to_standard_qfq,
)
from wtpy.apps.astock.data.tdx_reader import DayBar


def test_standard_qfq_equals_raw_times_factor_over_snapshot_end():
    fac = np.array([0.5, 0.8, 1.0], dtype=float)
    scale = standard_qfq_scale(fac)
    np.testing.assert_allclose(scale, fac / 1.0)
    # explicit snapshot end
    scale2 = standard_qfq_scale(fac, snapshot_end_factor=0.8)
    np.testing.assert_allclose(scale2, fac / 0.8)


def test_standard_qfq_last_bar_equals_raw_level():
    raw_close = np.array([10.0, 12.0, 11.0])
    fac = np.array([0.66, 0.90, 0.99688])
    scale = standard_qfq_scale(fac)
    qfq = raw_close * scale
    assert abs(qfq[-1] - raw_close[-1]) < 1e-9


def test_point_in_time_uses_first_base_not_end():
    fac = np.array([0.660503, 0.8, 0.99688])
    pit = causal_qfq_scale(fac)
    std = standard_qfq_scale(fac)
    np.testing.assert_allclose(pit, fac / 0.660503)
    np.testing.assert_allclose(std, fac / 0.99688)
    assert abs(pit[0] - 1.0) < 1e-9
    assert abs(std[-1] - 1.0) < 1e-9
    # alias
    np.testing.assert_allclose(point_in_time_adjusted_scale(fac), pit)


def test_snapshot_end_change_changes_standard_not_pit_base():
    fac1 = np.array([0.5, 0.8, 1.0])
    fac2 = np.array([0.5, 0.8, 1.0, 1.2])  # new CA at end
    s1 = standard_qfq_scale(fac1)
    s2 = standard_qfq_scale(fac2)
    # early levels re-anchor under standard_qfq
    assert abs(s1[0] - 0.5) < 1e-9
    assert abs(s2[0] - 0.5 / 1.2) < 1e-9
    # PIT base remains first finite
    p1 = causal_qfq_scale(fac1)
    p2 = causal_qfq_scale(fac2[:3])  # same first base on overlapping prefix
    np.testing.assert_allclose(p1, p2)


def test_day_bars_helpers_apply_scales():
    bars = [
        DayBar(20200101, 10, 11, 9, 10.5, 1e6, 1000, 0),
        DayBar(20200102, 10, 11, 9, 10.5, 1e6, 1000, 0),
    ]
    fac = np.array([0.5, 1.0])
    qfq = day_bars_to_standard_qfq(bars, fac)
    pit = day_bars_to_point_in_time_adjusted(bars, fac)
    assert abs(qfq[-1].close - 10.5) < 1e-6
    assert abs(pit[0].close - 10.5) < 1e-6  # scale 1 at base
    assert abs(qfq[0].close - 5.25) < 1e-6


def test_ratio_indicator_invariant_across_anchors():
    """Percent return on scaled series is invariant to positive scale re-anchor."""
    raw = np.array([7.79, 7.98])
    fac = np.array([0.99688, 0.99688])
    # both modes constant factor => same ratio
    for scale_fn in (standard_qfq_scale, causal_qfq_scale):
        s = scale_fn(fac)
        adj = raw * s
        assert abs((adj[1] / adj[0] - 1) - (raw[1] / raw[0] - 1)) < 1e-12
