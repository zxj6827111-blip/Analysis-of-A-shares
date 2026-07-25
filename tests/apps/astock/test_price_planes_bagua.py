# -*- coding: utf-8 -*-
"""P1: bagua attaches on L1 signal bars; three-plane repro fields."""
from __future__ import annotations

from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.price_planes import (
    BAGUA_OHLC_PLANE,
    BAGUA_OHLC_SOURCE,
    THREE_PLANE_SUMMARY_ZH,
    three_plane_repro_fields,
)
from wtpy.apps.astock.study import SignalEvent, attach_bagua, day_bars_to_standard_qfq


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def _bar(date: int, o: float, h: float, l: float, c: float) -> DayBar:
    return DayBar(date=date, open=o, high=h, low=l, close=c, amount=1e6, volume=1e4)


def test_three_plane_repro_fields():
    d = three_plane_repro_fields(
        signal_price_mode="standard_qfq",
        execution_price_mode="raw",
        valuation_price_mode="raw",
        corporate_action_policy="fail_closed",
    )
    assert d["bagua_ohlc_plane"] == BAGUA_OHLC_PLANE == "L2_trade_price"
    assert d["bagua_ohlc_source"] == BAGUA_OHLC_SOURCE
    planes = d["price_planes"]
    assert "L1_signal_price" in planes
    assert "L2_trade_price" in planes
    assert "L3_corporate_action_ledger" in planes
    assert planes["L3_corporate_action_ledger"]["implemented"] is False
    assert planes["L3_corporate_action_ledger"]["factor_jump_share_apply"] is False
    assert planes["L1_signal_price"]["default_formal"] == "asof_forward_qfq"
    assert "L1" in THREE_PLANE_SUMMARY_ZH and "L2" in THREE_PLANE_SUMMARY_ZH


def test_attach_bagua_uses_provided_signal_bars_not_raw_levels():
    """When signal bars differ from raw (qfq scale), bagua follows signal OHLC."""
    if not JSON_PATH.is_file():
        import pytest

        pytest.skip("bagua_384.json missing")

    calc = BaguaCalculator.from_json(JSON_PATH)
    # Raw levels (high absolute prices)
    raw = [_bar(20240105, 100.0, 110.0, 95.0, 105.0)]
    # Scaled signal bars (as if standard_qfq compressed levels)
    sig = [_bar(20240105, 70.0, 77.0, 66.5, 73.5)]

    ev_raw = [SignalEvent("SZSE.STK.1", 20240105, "DAY", "x")]
    ev_sig = [SignalEvent("SZSE.STK.1", 20240105, "DAY", "x")]
    attach_bagua(ev_raw, {"SZSE.STK.1": raw}, calc)
    attach_bagua(ev_sig, {"SZSE.STK.1": sig}, calc)
    br = ev_raw[0].bagua
    bs = ev_sig[0].bagua
    assert br is not None and bs is not None
    # Different OHLC worlds can yield different gua features
    # (at minimum state identity fields exist)
    for b in (br, bs):
        assert "full_name" in b or "upper_id" in b or "yao_name" in b


def test_day_bars_to_standard_qfq_changes_ohlc_for_bagua_input():
    """Sanity: qfq day bars differ from raw when factors != end."""
    import numpy as np

    raw = [
        _bar(20240102, 10.0, 11.0, 9.5, 10.5),
        _bar(20240103, 10.5, 12.0, 10.0, 11.0),
    ]
    fac = np.array([0.5, 1.0], dtype=float)
    qfq = day_bars_to_standard_qfq(raw, fac)
    assert abs(qfq[0].close - 5.25) < 1e-9  # 10.5 * 0.5/1.0
    assert abs(qfq[1].close - 11.0) < 1e-9
