# -*- coding: utf-8 -*-
"""Integration-style unit tests for research fingerprint wiring (no market data)."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.service.backtest import (
    BacktestRequest,
    research_fingerprint_fields_from_request,
)
from wtpy.apps.astock.service.db import param_hash, research_param_hash
from wtpy.apps.astock.research.fingerprint import research_fingerprint_from_params


def _sample_req(**overrides) -> BacktestRequest:
    base = dict(
        rule_ids=["735"],
        period="DAY",
        hold=1,
        entry_lag=1,
        buy_weekday=1,
        exit_weekday=4,
        buy_on="open",
        sell_on="open",
        with_bagua=False,
        research_unadjusted=True,
        stop_loss=0.05,
        account_mode="portfolio",
    )
    base.update(overrides)
    return BacktestRequest(**base)


def test_fields_from_request_has_16char_hashes():
    req = _sample_req()
    fields = research_fingerprint_fields_from_request(
        req, engine_code_hash="fixed_engine", universe_hash="uni123"
    )
    assert set(fields) >= {
        "research_fingerprint",
        "signal_fp",
        "filter_fp",
        "execution_fp",
    }
    for k, v in fields.items():
        assert isinstance(v, str) and len(v) == 16, (k, v)


def test_fields_from_request_stable_and_sensitive():
    a = research_fingerprint_fields_from_request(
        _sample_req(exit_weekday=4), engine_code_hash="e1"
    )
    b = research_fingerprint_fields_from_request(
        _sample_req(exit_weekday=4), engine_code_hash="e1"
    )
    c = research_fingerprint_fields_from_request(
        _sample_req(exit_weekday=5), engine_code_hash="e1"
    )
    assert a["research_fingerprint"] == b["research_fingerprint"]
    assert a["research_fingerprint"] != c["research_fingerprint"]
    assert a["execution_fp"] != c["execution_fp"]


def test_fields_match_research_fingerprint_from_params():
    req = _sample_req()
    fields = research_fingerprint_fields_from_request(
        req, engine_code_hash="eng", universe_hash="u1"
    )
    fp = research_fingerprint_from_params(
        {**req.to_dict(), "indicator_ids": req.rule_ids},
        engine_code_hash="eng",
        universe_hash="u1",
    )
    assert fields["research_fingerprint"] == fp.full_hex(16)
    assert fields["signal_fp"] == fp.signal_hex(16)


def test_param_hash_unchanged_by_research_helper():
    """Experiment de-dup must keep using naive param_hash."""
    params = {
        "rule_ids": ["735"],
        "period": "DAY",
        "hold": 1,
        "entry_lag": 1,
        "buy_weekday": 1,
        "exit_weekday": 4,
    }
    naive = param_hash(params)
    # same call twice — stable; research_param_hash is a separate API
    assert param_hash(params) == naive
    assert len(research_param_hash(params, include_engine=True)) == 16
