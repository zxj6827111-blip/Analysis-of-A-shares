# -*- coding: utf-8 -*-
"""Regression tests for event-anchored factor snap (Tushare micro-drift).

The fail-closed CA gate compares open-hold factors with ~1e-9 tolerance.
Raw Tushare adj_factor wanders in the 4th decimal after real ex-dates on
days with NO corporate action, which used to fail every run touching such
a stock ("unsupported_corporate_action"). The snap must absorb phantom
steps, keep ledger-anchored real steps (aligned to the event date), and
stay fail-closed on unexplained >=1% jumps.
"""

from wtpy.apps.astock.corporate_action import (
    check_open_hold_factor_change,
    factor_on_or_before,
)
from wtpy.apps.astock.data.adjustments import (
    FACTOR_SNAP_REL_TOL,
    snap_factor_event_steps,
)


def test_micro_drift_absorbed_with_ledger():
    # 002892 real pattern: ex-date 20240529 big step, then phantom drift
    dates = [20240528, 20240529, 20240530, 20240624, 20240625, 20240626, 20240627]
    vals = [5.007, 7.0595, 7.0595, 7.0595, 7.0594, 7.059, 7.059]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[20240529])
    assert ed == [20240528, 20240529]
    assert ef == [5.007, 7.0595]


def test_micro_drift_absorbed_without_ledger():
    dates = [20240603, 20240604, 20240605, 20240606]
    vals = [1.0, 1.00001, 0.99999, 1.00002]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=None)
    assert ed == [20240603]
    assert ef == [1.0]


def test_unexplained_big_jump_stays_fail_closed():
    dates = [20240603, 20240604, 20240605]
    vals = [1.0, 1.0, 1.109]  # +10.9% with no ledger event
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[])
    assert ed == [20240603, 20240605]
    assert ef == [1.0, 1.109]


def test_step_anchored_to_event_date_when_misaligned():
    # Factor takes effect 2 days after the recorded ex-date.
    dates = [20240603, 20240604, 20240605, 20240606, 20240607]
    vals = [1.0, 1.0, 1.0, 1.05, 1.05]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[20240604])
    assert ed == [20240603, 20240604]
    assert ef == [1.0, 1.05]


def test_step_anchored_across_holiday_lag():
    # 600717 pattern: ex_date 20260618 (Thu before 端午 holiday), adj_factor
    # step on 20260623 (Tue, 5 cal-day lag). ±10-day window must anchor it.
    dates = [20260617, 20260618, 20260622, 20260623, 20260624]
    vals = [34.8547, 34.8547, 34.8547, 35.6826, 35.6826]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[20260618])
    assert ed == [20260617, 20260618]
    assert ef == [34.8547, 35.6826]


def test_mid_size_unexplained_step_absorbed():
    # 0.05% step, no ledger event: settling noise, must not register.
    dates = [20240603, 20240604, 20240605]
    vals = [1.0, 1.0, 1.0005]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[])
    assert ed == [20240603]


def test_gate_passes_on_snapped_series_for_phantom_window():
    # Reproduces run bt_1785439514_38d88e failure triples (entry 20240624,
    # phantom steps 20240625/26): gate must stay silent on snapped series.
    dates = [20240528, 20240529, 20240624, 20240625, 20240626, 20240627, 20240628]
    vals = [5.007, 7.0595, 7.0595, 7.0594, 7.059, 7.059, 7.059]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[20240529])
    code = "SZSE.STK.002892"
    fbc = {code: dict(zip(ed, ef))}
    entry = factor_on_or_before(fbc, code, 20240624)
    for day in (20240625, 20240626, 20240627, 20240628):
        now = factor_on_or_before(fbc, code, day)
        msg = check_open_hold_factor_change(
            code=code,
            entry_date=20240624,
            entry_factor=entry,
            day=day,
            fac_now=now,
        )
        assert msg is None, msg


def test_gate_still_fires_on_unexplained_big_jump():
    dates = [20240603, 20240624, 20240625]
    vals = [1.0, 1.0, 1.109]
    ed, ef = snap_factor_event_steps(dates, vals, known_event_dates=[])
    code = "SZSE.STK.000004"
    fbc = {code: dict(zip(ed, ef))}
    entry = factor_on_or_before(fbc, code, 20240624)
    now = factor_on_or_before(fbc, code, 20240625)
    msg = check_open_hold_factor_change(
        code=code,
        entry_date=20240624,
        entry_factor=entry,
        day=20240625,
        fac_now=now,
    )
    assert msg is not None and "unsupported_corporate_action" in msg


def test_snap_tolerance_constant_below_min_real_action():
    # Market sample: smallest ledger-anchored real step was 3.02e-4.
    assert FACTOR_SNAP_REL_TOL < 3.0e-4
