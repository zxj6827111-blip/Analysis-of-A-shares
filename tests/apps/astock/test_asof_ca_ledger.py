# -*- coding: utf-8 -*-
"""Tests: asof qfq, CA ledger gates, bagua adjust modes (P0/P1)."""
from __future__ import annotations

import numpy as np

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.ca_ledger import (
    ALLOW_FACTOR_JUMP_SHARE_APPLY,
    CA_FACTOR_JUMP_AUDIT,
    CorporateActionEvent,
    apply_events_to_position,
    build_events_by_code,
    events_from_factor_series,
    normalize_corporate_action_policy,
)
from wtpy.apps.astock.data.adjustments import (
    FactorSeries,
    asof_forward_adjusted_scale,
    standard_qfq_scale,
)
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.price_planes import (
    DEFAULT_CORPORATE_ACTION_POLICY,
    three_plane_repro_fields,
)
from wtpy.apps.astock.study import day_bars_for_signals


def _bar(d, o, h, l, c):
    return DayBar(date=d, open=o, high=h, low=l, close=c, amount=1e6, volume=1e4)


def test_asof_forward_differs_from_end_anchor():
    fac = np.array([0.5, 0.5, 1.0], dtype=float)
    s_mid = asof_forward_adjusted_scale(fac, asof_factor=0.5)
    s_end = standard_qfq_scale(fac)
    assert abs(s_mid[0] - 1.0) < 1e-9
    assert abs(s_mid[2] - 2.0) < 1e-9
    assert abs(s_end[0] - 0.5) < 1e-9
    assert abs(s_end[2] - 1.0) < 1e-9


def test_run_end_asof_matches_standard_qfq():
    """Batch BT anchor=run_end is numerically standard_qfq on fixed snapshot."""
    fac = np.array([0.5, 0.5, 1.0], dtype=float)
    s_asof_end = asof_forward_adjusted_scale(fac, asof_factor=1.0)
    s_qfq = standard_qfq_scale(fac)
    assert np.allclose(s_asof_end, s_qfq)


def test_day_bars_for_signals_asof_default():
    bars = [
        _bar(20240101, 10, 11, 9, 10),
        _bar(20240102, 10, 11, 9, 10),
        _bar(20240103, 20, 21, 19, 20),
    ]
    fac = np.array([0.5, 0.5, 1.0])
    out = day_bars_for_signals(bars, fac, asof_date=20240102)
    assert abs(out[0].close - 10.0) < 1e-6
    assert abs(out[2].close - 40.0) < 1e-6
    out_end = day_bars_for_signals(bars, fac, asof_date=20240103)
    assert abs(out_end[0].close - 5.0) < 1e-6


def test_events_from_factor_jump_are_audit_only():
    assert ALLOW_FACTOR_JUMP_SHARE_APPLY is False
    fs = FactorSeries(
        std_code="SZSE.STK.1",
        dates=[20240101, 20240102, 20240103],
        factors=[0.5, 0.5, 1.0],
        source="test",
        event_dates=[20240101, 20240103],
        event_factors=[0.5, 1.0],
    )
    evs = events_from_factor_series(fs)
    assert len(evs) == 1
    assert evs[0].date == 20240103
    assert abs(evs[0].share_multiplier - 2.0) < 1e-9
    assert evs[0].event_type == CA_FACTOR_JUMP_AUDIT
    assert evs[0].is_applyable is False
    # for_apply path must not feed engine share restatement
    assert events_from_factor_series(fs, for_apply=True) == []


def test_factor_jump_does_not_change_shares_when_applied():
    """Cash-div-like factor jump must not restate shares (P0 gate)."""
    res = apply_events_to_position(
        shares=1000,
        cash=0.0,
        entry_price=10.0,
        events=[
            CorporateActionEvent(
                std_code="X",
                date=20240103,
                event_type=CA_FACTOR_JUMP_AUDIT,
                share_multiplier=2.0,
                source="factor_jump",
            )
        ],
        lot_size=100,
    )
    assert res.shares == 1000
    assert res.events_applied == []
    assert abs(res.cost_basis_scale - 1.0) < 1e-15


def test_explicit_share_ratio_still_applies():
    res = apply_events_to_position(
        shares=1000,
        cash=0.0,
        entry_price=10.0,
        events=[
            CorporateActionEvent(
                std_code="X",
                date=20240103,
                event_type="share_ratio",
                share_multiplier=2.0,
                source="explicit",
            )
        ],
        lot_size=100,
    )
    assert res.shares == 2000
    assert abs(res.cost_basis_scale - 0.5) < 1e-9


def test_policy_defaults_and_aliases():
    p, notes, force = normalize_corporate_action_policy(None)
    assert p == "fail_closed"
    assert force is False
    p2, notes2, _ = normalize_corporate_action_policy("ledger_factor_ratio")
    assert p2 == "event_ledger"
    assert any("disabled" in n or "factor-jump" in n for n in notes2)
    assert DEFAULT_CORPORATE_ACTION_POLICY == "fail_closed"


def test_three_plane_repro_l3_not_implemented():
    fields = three_plane_repro_fields(signal_price_mode="asof_forward_qfq")
    l3 = fields["price_planes"]["L3_corporate_action_ledger"]
    assert l3["implemented"] is False
    assert l3["factor_jump_share_apply"] is False
    assert fields["price_planes"]["L1_signal_price"]["default_formal"] == "asof_forward_qfq"


def test_bagua_normalize_adjust():
    from wtpy.apps.astock.service.bagua_query import normalize_adjust_mode

    assert normalize_adjust_mode("前复权") == "standard_qfq"
    assert normalize_adjust_mode("未复权") == "raw"
    assert normalize_adjust_mode("时点前复权") == "asof_forward_qfq"


def test_build_events_by_code_audit_vs_apply():
    fs = FactorSeries(
        std_code="SSE.STK.1",
        dates=[1, 2],
        factors=[1.0, 2.0],
        source="t",
        event_dates=[1, 2],
        event_factors=[1.0, 2.0],
    )
    by = build_events_by_code([fs])
    assert "SSE.STK.1" in by
    assert by["SSE.STK.1"][0].share_multiplier == 2.0
    assert by["SSE.STK.1"][0].is_applyable is False
    by_apply = build_events_by_code([fs], for_apply=True)
    assert by_apply == {}
