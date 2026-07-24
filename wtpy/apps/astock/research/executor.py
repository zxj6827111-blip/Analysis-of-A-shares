# -*- coding: utf-8 -*-
"""Research executor: optional signal/filter cache + fast/full engine selection."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import AStockConfig
from ..data.calendar import TradeCalendar
from ..data.tdx_reader import DayBar
from ..study import SignalEvent
from .fast_engine import FastBacktestResult, run_fast_backtest
from .filter_cache import filter_cache_key, get_or_compute_filtered
from .signal_cache import get_or_compute_signals, signal_cache_key


def build_signal_key_from_request(
    req: Any,
    *,
    universe_hash: Optional[str] = None,
    indicator_source_hash: Optional[str] = None,
) -> str:
    period = (getattr(req, "period", None) or "DAY").upper()
    adjust = (
        "research_unadjusted"
        if getattr(req, "research_unadjusted", False)
        else "adjusted"
    )
    return signal_cache_key(
        indicator_ids=list(getattr(req, "rule_ids", None) or []),
        indicator_source_hash=indicator_source_hash,
        period=period,
        start=getattr(req, "start", None),
        end=getattr(req, "end", None),
        universe_hash=universe_hash,
        adjust_mode=adjust,
        combine=getattr(req, "combine", None),
    )


def build_filter_key_from_request(
    req: Any,
    *,
    signal_key: str,
    gua_rule_version: Optional[str] = None,
) -> str:
    return filter_cache_key(
        signal_cache_key=signal_key,
        signal_weekdays=getattr(req, "signal_weekdays", None),
        gua_rule_version=gua_rule_version,
        gua_filter=getattr(req, "gua_filter", None),
        with_bagua=getattr(req, "with_bagua", None),
        bagua_filter_mode=getattr(req, "bagua_filter_mode", None),
    )


def cached_signal_pipeline(
    *,
    cfg: AStockConfig,
    signal_key: str,
    filter_key: str,
    compute_raw_signals: Callable[[], List[SignalEvent]],
    apply_filters: Callable[[List[SignalEvent]], List[SignalEvent]],
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Two-layer cache: raw signals → filtered signals."""
    raw, sig_hit = get_or_compute_signals(
        signal_key,
        compute_raw_signals,
        cfg=cfg,
        use_cache=use_cache,
        meta={"layer": "signal"},
    )

    def _filt() -> List[SignalEvent]:
        return apply_filters(list(raw))

    filtered, filt_hit = get_or_compute_filtered(
        filter_key,
        _filt,
        cfg=cfg,
        use_cache=use_cache,
        meta={"layer": "filter", "signal_key": signal_key},
    )
    return {
        "events": filtered,
        "raw_events": raw,
        "signal_cache_hit": sig_hit,
        "filter_cache_hit": filt_hit,
        "n_raw": len(raw),
        "n_filtered": len(filtered),
        "signal_key": signal_key,
        "filter_key": filter_key,
    }


def run_engine(
    engine: str,
    events: Sequence[SignalEvent],
    *,
    full_runner: Optional[Callable[..., Any]] = None,
    full_kwargs: Optional[Dict[str, Any]] = None,
    bars_by_code: Optional[Dict[str, Sequence[DayBar]]] = None,
    calendar: Optional[TradeCalendar] = None,
    hold: int = 1,
    entry_lag: int = 1,
    buy_on: str = "open",
    sell_on: str = "open",
    buy_weekday: Optional[int] = None,
    exit_weekday: Optional[int] = None,
    holiday_policy: str = "next_trading_day",
    signal_weekdays: Optional[Sequence[int]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    adj_bars_by_code: Optional[Dict[str, Sequence[DayBar]]] = None,
    factor_by_code: Optional[Dict[str, Dict[int, float]]] = None,
    require_factor_map: bool = False,
) -> Dict[str, Any]:
    """Dispatch to fast or full engine."""
    eng = (engine or "full").strip().lower()
    if eng in ("fast", "quick", "research_fast"):
        if bars_by_code is None or calendar is None:
            raise ValueError("fast engine requires bars_by_code and calendar")
        res: FastBacktestResult = run_fast_backtest(
            events,
            bars_by_code,
            calendar,
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
            adj_bars_by_code=adj_bars_by_code,
            factor_by_code=factor_by_code,
            require_factor_map=require_factor_map,
        )
        return {
            "engine": "fast",
            "result": res,
            "metrics": res.metrics,
            "summary": res.to_dict(),
            "corporate_action_policy": (res.metrics or {}).get(
                "corporate_action_policy", "not_checked"
            ),
        }
    if full_runner is None:
        raise ValueError("full engine requires full_runner callable")
    out = full_runner(events, **dict(full_kwargs or {}))
    return {
        "engine": "full",
        "result": out,
        "metrics": getattr(out, "metrics", None),
    }
