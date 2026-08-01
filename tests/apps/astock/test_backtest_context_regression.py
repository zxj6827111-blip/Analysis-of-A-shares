# -*- coding: utf-8 -*-
"""Regression tests for the shared backtest orchestration context."""

from __future__ import annotations

from types import SimpleNamespace

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import CostConfig
from wtpy.apps.astock.service.backtest_context import (
    BacktestRunContext,
    BaguaState,
    CacheState,
    PriceModes,
    ScheduleParams,
    apply_execution_cache,
    finalize_with_ctx,
    run_engine_with_ctx,
    run_portfolio_and_finalize,
)


def _context(tmp_path) -> BacktestRunContext:
    req = SimpleNamespace(
        account_mode="portfolio",
        stop_loss=None,
        take_profit=None,
        signal_data_source="internal",
        signal_adjustment="tushare_factor_qfq",
        dataset_id="signal-dataset",
        signal_raw_parent_dataset_id="raw-parent",
        signal_factor_parent_dataset_id="factor-parent",
        signal_formula_version="tsqfq_v1",
        signal_anchor_policy="last_factor_on_or_before_cutoff",
        ca_factor_dataset_id="ca-factor",
        signal_supplement_factor_dataset_id="supplement-factor",
        execution_data_source="local_vendor",
        execution_adjustment="none",
        execution_dataset_id="execution-dataset",
        execution_parent_dataset_ids=["execution-parent"],
        weekly_bar_mode="local_aggregate",
        universe_dataset_id="pit-universe",
        universe_rule_version="pit-v1",
        delist_exit_scenario="last_tradable_price",
        delist_recovery_discount=None,
        delist_exit_rule_version="delist-v1",
        baseline_generation="survivorship_safe",
        data_cutoff_date=20241231,
        corporate_action_policy=None,
    )
    return BacktestRunContext(
        cfg=SimpleNamespace(output_root=tmp_path, costs=CostConfig()),
        req=req,
        codes=["SSE.STK.600000"],
        schedule=ScheduleParams(
            period="DAY",
            hold=3,
            entry_lag=1,
            buy_on="open",
            sell_on="open",
            buy_weekday=1,
            exit_weekday=4,
            signal_weekdays=[5],
            holiday_policy="next_trading_day",
            start=20240101,
            end=20241231,
        ),
        price=PriceModes(),
        bagua=BaguaState(
            enabled=True,
            plane_requested="standard_qfq",
            plane_effective="raw",
            plane_missing_count=1,
            plane_missing_codes=["SSE.STK.600000"],
        ),
        cache=CacheState(),
        run_id="bt_context_regression",
        calendar_meta={"source": "dataset", "calendar_sha256": "calendar-sha"},
        pit_meta={"mode": "point_in_time", "excluded_count": 2},
        delist_policy=SimpleNamespace(scenario="last_tradable_price"),
        delist_terminal_dates={"SSE.STK.600000": 20241220},
        delist_meta={"delist_exit_scenario": "last_tradable_price"},
        ca_events_by_code={
            "SSE.STK.600000": [
                SimpleNamespace(
                    std_code="SSE.STK.600000",
                    date=20240603,
                    event_type="cash_dividend",
                )
            ]
        },
        ca_meta={
            "cache_root": str(tmp_path / "ca_events"),
            "event_manifest_sha256": "ca-manifest-sha",
            "event_count": 1,
            "event_symbol_count": 1,
            "sync_failed": 0,
        },
    )

def test_context_public_symbols_importable():
    assert BacktestRunContext
    assert ScheduleParams
    assert PriceModes
    assert BaguaState
    assert CacheState
    assert callable(run_engine_with_ctx)
    assert callable(apply_execution_cache)
    assert callable(finalize_with_ctx)
    assert callable(run_portfolio_and_finalize)


def test_context_auto_selects_event_ledger_only_with_explicit_events(tmp_path):
    ctx = _context(tmp_path)
    ctx.apply_standard_qfq_raw_execution_v2()
    assert ctx.price.corporate_action_policy == "event_ledger"

    ctx.ca_events_by_code = {}
    ctx.price.corporate_action_policy = "event_ledger"
    ctx.apply_standard_qfq_raw_execution_v2()
    assert ctx.price.corporate_action_policy == "fail_closed"

    ctx.req.corporate_action_policy = "not_checked"
    ctx.apply_standard_qfq_raw_execution_v2()
    assert ctx.price.corporate_action_policy == "not_checked"


def test_run_engine_forwards_delist_policy(tmp_path, monkeypatch):
    from wtpy.apps.astock.service import backtest_engines

    ctx = _context(tmp_path)
    captured = {}
    sentinel = object()

    def fake_run_fast_or_full_engine(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        backtest_engines,
        "run_fast_or_full_engine",
        fake_run_fast_or_full_engine,
    )

    result = run_engine_with_ctx(
        ctx,
        cal=object(),
        events=[],
        execution_bars={},
        adj_map={},
    )

    assert result is sentinel
    assert captured["delist_policy"] is ctx.delist_policy
    assert captured["delist_terminal_dates"] == ctx.delist_terminal_dates
    assert captured["explicit_ca_events"] == ctx.ca_events_by_code["SSE.STK.600000"]


def test_execution_cache_payload_includes_lineage_and_policies(tmp_path, monkeypatch):
    from wtpy.apps.astock.research import execution_cache

    ctx = _context(tmp_path)
    ctx.engine = "fast"
    ctx.artifact_level = "summary"
    ctx.cache.use_signal_cache = True
    captured = {}

    def fake_execution_cache_key(payload):
        captured["payload"] = payload
        return "cache-key"

    monkeypatch.setattr(
        execution_cache,
        "execution_cache_key",
        fake_execution_cache_key,
    )
    monkeypatch.setattr(execution_cache, "load_execution_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(execution_cache, "save_execution_cache", lambda *_a, **_k: None)

    result = SimpleNamespace(metrics={}, notes=[])
    assert apply_execution_cache(ctx, result, []) is result

    payload = captured["payload"]
    assert payload["signal_adjustment"] == "tushare_factor_qfq"
    assert payload["raw_parent_dataset_id"] == "raw-parent"
    assert payload["factor_parent_dataset_id"] == "factor-parent"
    assert payload["formula_version"] == "tsqfq_v1"
    assert payload["anchor_policy"] == "last_factor_on_or_before_cutoff"
    assert payload["ca_factor_dataset_id"] == "ca-factor"
    assert payload["ca_event_manifest_sha256"] == "ca-manifest-sha"
    assert payload["ca_event_count"] == 1
    assert payload["supplement_factor_dataset_id"] == "supplement-factor"
    assert payload["universe_dataset_id"] == "pit-universe"
    assert payload["universe_rule_version"] == "pit-v1"
    assert payload["delist_exit_scenario"] == "last_tradable_price"
    assert payload["delist_exit_rule_version"] == "delist-v1"
    assert payload["baseline_generation"] == "survivorship_safe"
    assert payload["data_cutoff_date"] == 20241231

def test_finalize_forwards_repro_provenance(tmp_path, monkeypatch):
    from wtpy.apps.astock.service import backtest_artifacts

    ctx = _context(tmp_path)
    captured = {}

    def fake_build_repro_meta(**kwargs):
        captured["repro"] = kwargs
        return {"marker": "repro"}, {"fingerprint": "value"}

    def fake_finalize_run_outputs(**kwargs):
        captured["finalize"] = kwargs
        return {"status": "ok"}

    monkeypatch.setattr(
        backtest_artifacts,
        "build_repro_meta",
        fake_build_repro_meta,
    )
    monkeypatch.setattr(
        backtest_artifacts,
        "finalize_run_outputs",
        fake_finalize_run_outputs,
    )

    result = finalize_with_ctx(
        ctx,
        result=SimpleNamespace(),
        events=[],
        progress=lambda _payload: None,
    )

    assert result == {"status": "ok"}
    repro = captured["repro"]
    assert repro["calendar_meta"] == ctx.calendar_meta
    assert repro["pit_meta"] == ctx.pit_meta
    assert repro["delist_meta"] == ctx.delist_meta
    assert repro["ca_meta"] == ctx.ca_meta
    assert repro["bagua_plane_effective"] == "raw"
    assert repro["bagua_plane_missing_count"] == 1
    assert repro["bagua_plane_missing_codes"] == ["SSE.STK.600000"]
    assert captured["finalize"]["repro"] == {"marker": "repro"}
    assert captured["finalize"]["_fp_fields"] == {"fingerprint": "value"}
