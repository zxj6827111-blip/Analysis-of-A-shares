# -*- coding: utf-8 -*-
"""Full / fast portfolio engine dispatch for dual-price backtests."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..corporate_action import build_factor_by_code, normalize_corporate_action_policy
from ..ca_ledger import build_events_by_code
from ..research.fast_engine import run_fast_backtest
from ..strategy import PortfolioBacktester


def run_fast_or_full_engine(
    *,
    engine: str,
    cfg: Any,
    cal: Any,
    events: Sequence[Any],
    execution_bars: Dict[str, Any],
    adj_map: Dict[str, Any],
    standard_qfq_map: Optional[Dict[str, Any]] = None,
    factor_series: Sequence[Any],
    research_unadj: bool,
    use_research: bool,
    use_formal_ok: bool,
    formal_ok: bool,
    corporate_action_policy: str,
    engine_result_version: str,
    run_id: str,
    hold: int,
    period: str,
    start: Optional[int],
    end: Optional[int],
    entry_lag: int,
    account_mode: str,
    signal_weekdays: Any,
    buy_on: str,
    sell_on: str,
    buy_weekday: Any,
    exit_weekday: Any,
    holiday_policy: str,
    stop_loss: Any,
    take_profit: Any,
    artifact_level: str,
    n_codes: int,
    n_events_raw_signals: int,
    n_events_after_weekday: int,
    bagua_enabled: bool,
    bagua_n_before: Any,
    bagua_n_after: Any,
) -> Any:
    """Run fast or full PortfolioBacktester; return BacktestResult-like object.

    Also returns updated corporate_action_policy string as attribute on result via metrics.
    """
    from ..strategy import BacktestResult as _BTR

    eng = (engine or "full").strip().lower()
    # Canonical policy for gates, repro, and PortfolioBacktester
    corporate_action_policy, _ca_pol_notes, _ca_pol_force = (
        normalize_corporate_action_policy(corporate_action_policy)
    )
    if eng in ("fast", "quick", "research_fast"):
        factor_by_code_fast, _factor_errs_fast = build_factor_by_code(factor_series)
        fast_res = run_fast_backtest(
            events,
            execution_bars,
            cal,
            hold=hold,
            entry_lag=entry_lag,
            buy_on=buy_on,
            sell_on=sell_on,
            buy_weekday=buy_weekday,
            exit_weekday=exit_weekday,
            holiday_policy=holiday_policy,
            signal_weekdays=signal_weekdays,
            start=start,
            end=end,
            adj_bars_by_code=adj_map if not research_unadj else None,
            standard_qfq_bars_by_code=(
                standard_qfq_map if not research_unadj else None
            ),
            factor_by_code=factor_by_code_fast or None,
            require_factor_map=True,
        )
        _fast_status = (
            "research_unadjusted"
            if use_research
            else str(
                (fast_res.metrics or {}).get("status")
                or (fast_res.config or {}).get("status")
                or "ok"
            )
        )
        result = _BTR(
            run_id=run_id,
            config=dict(fast_res.config),
            fills=[],
            equity_curve=[],
            metrics=dict(fast_res.metrics),
            notes=list(fast_res.notes)
            + [
                "engine=fast: screening only; no true cash simulation.",
            ],
            status=_fast_status,
        )
        _engine_ca = (
            (fast_res.metrics or {}).get("corporate_action_policy")
            or (fast_res.config or {}).get("corporate_action_policy")
            or corporate_action_policy
        )
        result.metrics["engine"] = "fast"
        result.metrics["supports_true_cash_simulation"] = False
        result.metrics["corporate_action_policy"] = _engine_ca
        result.metrics["n_signals_fast"] = fast_res.n_signals
        result.metrics["n_events"] = len(events)
        result.metrics["n_events_raw_signals"] = int(n_events_raw_signals)
        result.metrics["n_events_after_weekday"] = int(n_events_after_weekday)
        if bagua_enabled:
            result.metrics["n_signals_before_bagua"] = bagua_n_before
            result.metrics["n_signals_after_bagua"] = bagua_n_after
        result.config["engine"] = "fast"
        result.config["holiday_policy"] = holiday_policy
        result.config["artifact_level"] = artifact_level
        result.config["corporate_action_policy"] = _engine_ca
        result.config["supports_true_cash_simulation"] = False
        result.metrics["_resolved_corporate_action_policy"] = str(_engine_ca)
        return result

    factor_by_code, factor_map_errors = build_factor_by_code(factor_series)
    ca_events_by_code = build_events_by_code(factor_series, for_apply=True)
    if (
        not use_research
        and use_formal_ok
        and corporate_action_policy in ("fail_closed", "event_ledger")
        and not factor_by_code
    ):
        result = _BTR(
            run_id=run_id,
            config={
                "engine": "full",
                "artifact_level": artifact_level,
                "corporate_action_policy": corporate_action_policy,
                "engine_result_version": engine_result_version,
            },
            fills=[],
            equity_curve=[],
            metrics={
                "engine": "full",
                "corporate_action_policy": corporate_action_policy,
                "n_events": len(events),
            },
            notes=[
                "unsupported_corporate_action: formal full engine requires "
                "factor_by_code for corporate_action_policy=%s; maps empty or build failed."
                % corporate_action_policy,
            ]
            + factor_map_errors,
            status="unsupported_corporate_action",
        )
        result.metrics["_resolved_corporate_action_policy"] = corporate_action_policy
        return result

    bt = PortfolioBacktester(
        cfg,
        cal,
        execution_bars,
        adj_bars_by_code=adj_map if not research_unadj else None,
        standard_qfq_bars_by_code=(
            standard_qfq_map if not research_unadj else None
        ),
        factor_by_code=factor_by_code or None,
        ca_events_by_code=ca_events_by_code or None,
        corporate_action_policy=corporate_action_policy,
    )
    result = bt.run(
        events,
        hold=hold,
        period=period,
        run_id=run_id,
        start=start,
        end=end,
        research_unadjusted=use_research,
        formal_ok=use_formal_ok,
        stop_loss_pct=stop_loss,
        take_profit_pct=take_profit,
        entry_lag=entry_lag,
        account_mode=account_mode or "portfolio",
        signal_weekdays=signal_weekdays,
        buy_on=buy_on,
        sell_on=sell_on,
        buy_weekday=buy_weekday,
        exit_weekday=exit_weekday,
        holiday_policy=holiday_policy,
    )
    result.config["engine"] = "full"
    result.config["artifact_level"] = artifact_level
    result.config["corporate_action_policy"] = corporate_action_policy
    result.metrics["corporate_action_policy"] = corporate_action_policy
    result.metrics["n_events"] = len(events)
    result.metrics["n_events_raw_signals"] = int(n_events_raw_signals)
    result.metrics["n_events_after_weekday"] = int(n_events_after_weekday)
    if bagua_enabled:
        result.metrics["n_signals_before_bagua"] = bagua_n_before
        result.metrics["n_signals_after_bagua"] = bagua_n_after
    if factor_map_errors:
        result.notes = list(result.notes) + factor_map_errors
    result.metrics["_resolved_corporate_action_policy"] = corporate_action_policy
    return result
