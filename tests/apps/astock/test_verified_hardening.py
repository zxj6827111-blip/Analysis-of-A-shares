# -*- coding: utf-8 -*-
"""Regression tests for verified hardening fixes (api tmp, matrix, trial json, cache)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wtpy.apps.astock.research.matrix import build_result_matrix
from wtpy.apps.astock.research.signal_cache import records_to_events


def test_matrix_skips_bad_exit_weekday_instead_of_crashing():
    rows = [
        {"exit_weekday": 1, "sell_on": "open", "gua_key": "none", "total_return": 0.1},
        {"exit_weekday": "not-a-day", "sell_on": "open", "gua_key": "none", "total_return": 0.2},
    ]
    m = build_result_matrix(rows)
    assert len(m["table"]) == 1
    assert m["table"][0]["exit_weekday"] == 1
    assert any("exit_weekday" in str(x.get("reason", "")) for x in m["missing"])


def test_records_to_events_skips_missing_or_bad_date():
    rows = [
        {"std_code": "SSE.STK.600000", "date": 20240102, "period": "DAY", "source": "x"},
        {"std_code": "SSE.STK.600000", "period": "DAY"},  # missing date
        {"std_code": "SSE.STK.600000", "date": "xx", "period": "DAY"},
    ]
    evs = records_to_events(rows)
    assert len(evs) == 1
    assert evs[0].date == 20240102


def test_trial_params_json_accepts_path_via_default_str():
    """json.dumps(..., default=str) used for params must not TypeError on Path."""
    params = {"root": Path("."), "n": 1}
    s = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    assert "root" in s
    # without default=str this would raise
    with pytest.raises(TypeError):
        json.dumps(params, ensure_ascii=False, sort_keys=True)
