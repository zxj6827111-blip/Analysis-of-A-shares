# -*- coding: utf-8 -*-
"""Shared run context bundles for backtest orchestration.

Keeps schedule / price-mode / bagua / cache fields in one place so engine
dispatch and artifact writers do not repeat 40-keyword call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


@dataclass
class ScheduleParams:
    period: str
    hold: int
    entry_lag: int
    buy_on: str
    sell_on: str
    buy_weekday: Optional[int]
    exit_weekday: Optional[int]
    signal_weekdays: Any
    holiday_policy: str
    start: Optional[int]
    end: Optional[int]


@dataclass
class PriceModes:
    """Four-lane: signal standard_qfq (or raw), exec/valuation raw; PIT audit only."""

    price_mode: str = "asof_qfq_signal_raw_execution_v3"
    signal_price_mode: str = "asof_forward_qfq"
    execution_price_mode: str = "raw"
    valuation_price_mode: str = "raw"
    engine_result_version: str = "asof_qfq_signal_raw_execution_v3"
    research_unadj: bool = False
    formal_ok: bool = True
    use_research: bool = False
    use_formal_ok: bool = True
    corporate_action_policy: str = "fail_closed"
    adj_msg: Any = None


@dataclass
class BaguaState:
    enabled: bool = False
    filter_mode: Any = None
    gua_filter_meta: Any = None
    gf: Any = None
    n_before: Any = None
    n_after: Any = None
    plane_requested: str = "raw"
    plane_effective: str = "raw"
    plane_missing_count: int = 0
    plane_missing_codes: List[str] = field(default_factory=list)


@dataclass
class CacheState:
    signal_hit: bool = False
    filter_hit: bool = False
    execution_hit: bool = False
    use_signal_cache: bool = False


@dataclass
class BacktestRunContext:
    """Orchestrator state filled as the run progresses."""

    cfg: Any
    req: Any
    codes: Sequence[str]
    schedule: ScheduleParams
    price: PriceModes = field(default_factory=PriceModes)
    bagua: BaguaState = field(default_factory=BaguaState)
    cache: CacheState = field(default_factory=CacheState)
    combine: Any = None
    engine: str = "full"
    artifact_level: str = "full"
    art_flags: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    trade_specs: Sequence[Any] = field(default_factory=list)
    formula_audits: Dict[str, Any] = field(default_factory=dict)
    factor_series: Any = None
    ca_events_by_code: Dict[str, List[Any]] = field(default_factory=dict)
    ca_meta: Dict[str, Any] = field(default_factory=dict)
    unconfirmed_run: bool = False
    n_events_raw_signals: int = 0
    n_events_after_weekday: int = 0
    errors: List[dict] = field(default_factory=list)
    calendar_meta: Dict[str, Any] = field(default_factory=dict)
    pit_meta: Dict[str, Any] = field(default_factory=dict)
    delist_policy: Any = None
    delist_terminal_dates: Dict[str, int] = field(default_factory=dict)
    delist_meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def n_codes(self) -> int:
        return len(self.codes)

    def apply_dual_price_v1(self) -> None:
        """Alias: apply four-lane standard_qfq signal + raw execution modes."""
        self.apply_standard_qfq_raw_execution_v2()

    def apply_standard_qfq_raw_execution_v2(self) -> None:
        """Set four-lane / three-plane modes from research_unadj / formal_ok.

        L1 formal default: asof_forward_qfq (时点动态前复权, anchor=run end).
        L2: raw execution/valuation.
        L3: fail_closed (no factor-jump share restatement); req may override for research.
        """
        p = self.price
        p.signal_price_mode = "raw" if p.research_unadj else "asof_forward_qfq"
        p.execution_price_mode = "raw"
        p.valuation_price_mode = "raw"
        p.engine_result_version = "asof_qfq_signal_raw_execution_v3"
        p.price_mode = "asof_qfq_signal_raw_execution_v3"
        if p.research_unadj:
            p.use_research = True
            p.use_formal_ok = True
        else:
            p.use_research = False
            p.use_formal_ok = p.formal_ok
        # L3 formal default: fail_closed; request may override (aliases normalized)
        from ..corporate_action import normalize_corporate_action_policy

        req_ca = None
        try:
            req_ca = getattr(self.req, "corporate_action_policy", None)
        except Exception:
            req_ca = None
        if req_ca:
            pol, _notes, _force = normalize_corporate_action_policy(req_ca)
            p.corporate_action_policy = pol
        else:
            # Automatic formal path: use the explicit ledger when selected
            # cached events exist; otherwise retain the fail-closed gate.
            p.corporate_action_policy = (
                "event_ledger" if self.ca_events_by_code else "fail_closed"
            )


def run_engine_with_ctx(
    ctx: BacktestRunContext,
    *,
    cal: Any,
    events: Sequence[Any],
    execution_bars: Dict[str, Any],
    adj_map: Dict[str, Any],
    standard_qfq_map: Optional[Dict[str, Any]] = None,
) -> Any:
    """Dispatch fast/full engine using schedule + price bundles on ctx."""
    from .backtest_engines import run_fast_or_full_engine

    s, p, b = ctx.schedule, ctx.price, ctx.bagua
    return run_fast_or_full_engine(
        engine=ctx.engine,
        cfg=ctx.cfg,
        cal=cal,
        events=events,
        execution_bars=execution_bars,
        adj_map=adj_map,
        standard_qfq_map=standard_qfq_map,
        factor_series=ctx.factor_series or [],
        explicit_ca_events=[
            event
            for code in sorted(ctx.ca_events_by_code)
            for event in (ctx.ca_events_by_code.get(code) or [])
        ],
        research_unadj=p.research_unadj,
        use_research=p.use_research,
        use_formal_ok=p.use_formal_ok,
        formal_ok=p.formal_ok,
        corporate_action_policy=p.corporate_action_policy,
        engine_result_version=p.engine_result_version,
        run_id=ctx.run_id,
        hold=s.hold,
        period=s.period,
        start=s.start,
        end=s.end,
        entry_lag=s.entry_lag,
        account_mode=getattr(ctx.req, "account_mode", None) or "portfolio",
        signal_weekdays=s.signal_weekdays,
        buy_on=s.buy_on,
        sell_on=s.sell_on,
        buy_weekday=s.buy_weekday,
        exit_weekday=s.exit_weekday,
        holiday_policy=s.holiday_policy,
        stop_loss=ctx.req.stop_loss,
        take_profit=ctx.req.take_profit,
        artifact_level=ctx.artifact_level,
        n_codes=ctx.n_codes,
        n_events_raw_signals=ctx.n_events_raw_signals,
        n_events_after_weekday=ctx.n_events_after_weekday,
        bagua_enabled=b.enabled,
        bagua_n_before=b.n_before,
        bagua_n_after=b.n_after,
        delist_policy=ctx.delist_policy,
        delist_terminal_dates=ctx.delist_terminal_dates,
    )


def apply_execution_cache(
    ctx: BacktestRunContext,
    result: Any,
    events: Sequence[Any],
) -> Any:
    """Phase-3 execution cache for fast+summary screening; mutates ctx.cache."""
    try:
        from dataclasses import asdict as _asdict_c

        from ..data.catalog import selected_universe_sha
        from ..research.execution_cache import (
            execution_cache_key,
            load_execution_cache,
            save_execution_cache,
        )

        s, p, b, c = ctx.schedule, ctx.price, ctx.bagua, ctx.cache
        _ex_payload = {
            "engine": "fast" if ctx.engine in ("fast", "quick", "research_fast") else "full",
            "engine_result_version": p.engine_result_version,
            "signal_price_mode": p.signal_price_mode,
            "execution_price_mode": p.execution_price_mode,
            "valuation_price_mode": p.valuation_price_mode,
            "corporate_action_policy": p.corporate_action_policy,
            "artifact_level": ctx.artifact_level,
            "rule_ids": [spec.id for spec in ctx.trade_specs],
            "period": s.period,
            "hold": s.hold,
            "entry_lag": s.entry_lag,
            "buy_on": s.buy_on,
            "sell_on": s.sell_on,
            "buy_weekday": s.buy_weekday,
            "exit_weekday": s.exit_weekday,
            "signal_weekdays": s.signal_weekdays,
            "holiday_policy": s.holiday_policy,
            "stop_loss": ctx.req.stop_loss,
            "take_profit": ctx.req.take_profit,
            "account_mode": getattr(ctx.req, "account_mode", None) or "portfolio",
            "start": s.start,
            "end": s.end,
            "universe": selected_universe_sha(ctx.codes),
            "adjust": (
                "research_unadjusted"
                if p.research_unadj
                else "signal_standard_qfq_exec_raw"
            ),
            "gua": (b.gf.to_dict() if b.gf else None),
            "with_bagua": b.enabled,
            "bagua_filter_mode": b.filter_mode,
            "bagua_price_plane": b.plane_effective,
            "n_events": len(events),
            "costs": _asdict_c(ctx.cfg.costs),
            "signal_data_source": getattr(ctx.req, "signal_data_source", None) or "",
            "signal_adjustment": getattr(ctx.req, "signal_adjustment", None) or "",
            "signal_dataset_id": getattr(ctx.req, "dataset_id", None) or "",
            "raw_parent_dataset_id": getattr(
                ctx.req, "signal_raw_parent_dataset_id", None
            ) or "",
            "factor_parent_dataset_id": getattr(
                ctx.req, "signal_factor_parent_dataset_id", None
            ) or "",
            "formula_version": getattr(ctx.req, "signal_formula_version", None) or "",
            "anchor_policy": getattr(ctx.req, "signal_anchor_policy", None) or "",
            "ca_factor_dataset_id": getattr(ctx.req, "ca_factor_dataset_id", None) or "",
            "ca_event_manifest_sha256": ctx.ca_meta.get("event_manifest_sha256") or "",
            "ca_event_count": int(ctx.ca_meta.get("event_count") or 0),
            "ca_event_symbol_count": int(
                ctx.ca_meta.get("event_symbol_count") or 0
            ),
            "supplement_factor_dataset_id": getattr(
                ctx.req, "signal_supplement_factor_dataset_id", None
            ) or "",
            "execution_data_source": getattr(ctx.req, "execution_data_source", None) or "internal",
            "execution_adjustment": getattr(ctx.req, "execution_adjustment", None) or "none",
            "execution_dataset_id": getattr(ctx.req, "execution_dataset_id", None) or "",
            "execution_parent_dataset_ids": list(
                getattr(ctx.req, "execution_parent_dataset_ids", None) or []
            ),
            "weekly_bar_mode": getattr(ctx.req, "weekly_bar_mode", None) or "local_aggregate",
            "universe_dataset_id": getattr(ctx.req, "universe_dataset_id", None) or "",
            "universe_rule_version": getattr(ctx.req, "universe_rule_version", None) or "",
            "delist_exit_scenario": getattr(ctx.req, "delist_exit_scenario", None) or "",
            "delist_recovery_discount": getattr(
                ctx.req, "delist_recovery_discount", None
            ),
            "delist_exit_rule_version": getattr(
                ctx.req, "delist_exit_rule_version", None
            ) or "",
            "baseline_generation": getattr(ctx.req, "baseline_generation", None) or "",
            "data_cutoff_date": getattr(ctx.req, "data_cutoff_date", None),
        }
        _ex_key = execution_cache_key(_ex_payload)
        if (
            c.use_signal_cache
            and ctx.engine in ("fast", "quick", "research_fast")
            and ctx.artifact_level == "summary"
        ):
            _ex_hit = load_execution_cache(_ex_key, cfg=ctx.cfg)
            if _ex_hit and isinstance(_ex_hit.get("metrics"), dict):
                c.execution_hit = True
                result.metrics = dict(_ex_hit["metrics"])
                result.metrics["execution_cache_hit"] = True
                result.notes = list(result.notes) + ["execution_cache_hit"]
        if not c.execution_hit and c.use_signal_cache:
            try:
                save_execution_cache(
                    _ex_key,
                    metrics=dict(result.metrics or {}),
                    meta={"run_id": ctx.run_id, "engine": ctx.engine},
                    cfg=ctx.cfg,
                )
            except Exception:
                pass
    except Exception:
        pass
    return result


def finalize_with_ctx(
    ctx: BacktestRunContext,
    *,
    result: Any,
    events: Sequence[Any],
    progress: Callable[[dict], None],
) -> Dict[str, Any]:
    """Build repro meta and write outputs via existing artifact helpers."""
    from .backtest_artifacts import build_repro_meta, finalize_run_outputs

    s, p, b, c = ctx.schedule, ctx.price, ctx.bagua, ctx.cache
    out_dir = Path(ctx.cfg.output_root) / ctx.run_id
    repro, _fp_fields = build_repro_meta(
        cfg=ctx.cfg,
        req=ctx.req,
        events=events,
        trade_specs=ctx.trade_specs,
        codes=ctx.codes,
        formula_audits=ctx.formula_audits,
        factor_series=ctx.factor_series,
        adj_msg=p.adj_msg,
        research_unadj=p.research_unadj,
        formal_ok=p.formal_ok,
        unconfirmed_run=ctx.unconfirmed_run,
        price_mode=p.price_mode,
        signal_price_mode=p.signal_price_mode,
        execution_price_mode=p.execution_price_mode,
        valuation_price_mode=p.valuation_price_mode,
        corporate_action_policy=p.corporate_action_policy,
        engine_result_version=p.engine_result_version,
        entry_lag=s.entry_lag,
        signal_weekdays=s.signal_weekdays,
        buy_on=s.buy_on,
        sell_on=s.sell_on,
        buy_weekday=s.buy_weekday,
        exit_weekday=s.exit_weekday,
        period=s.period,
        hold=s.hold,
        combine=ctx.combine,
        start=s.start,
        end=s.end,
        bagua_enabled=b.enabled,
        bagua_filter_mode=b.filter_mode,
        gua_filter_meta=b.gua_filter_meta,
        gf=b.gf,
        bagua_n_before=b.n_before,
        bagua_n_after=b.n_after,
        signal_cache_hit=c.signal_hit,
        filter_cache_hit=c.filter_hit,
        execution_cache_hit=c.execution_hit,
        use_signal_cache=c.use_signal_cache,
        calendar_meta=ctx.calendar_meta,
        pit_meta=ctx.pit_meta,
        delist_meta=ctx.delist_meta,
        ca_meta=ctx.ca_meta,
        bagua_plane_effective=b.plane_effective,
        bagua_plane_missing_count=b.plane_missing_count,
        bagua_plane_missing_codes=b.plane_missing_codes,
    )
    return finalize_run_outputs(
        cfg=ctx.cfg,
        req=ctx.req,
        result=result,
        events=events,
        trade_specs=ctx.trade_specs,
        out_dir=out_dir,
        repro=repro,
        art_flags=ctx.art_flags,
        artifact_level=ctx.artifact_level,
        progress=progress,
        n_codes=ctx.n_codes,
        run_id=ctx.run_id,
        period=s.period,
        hold=s.hold,
        entry_lag=s.entry_lag,
        start=s.start,
        end=s.end,
        buy_on=s.buy_on,
        sell_on=s.sell_on,
        buy_weekday=s.buy_weekday,
        exit_weekday=s.exit_weekday,
        signal_weekdays=s.signal_weekdays,
        codes=ctx.codes,
        engine=ctx.engine,
        holiday_policy=s.holiday_policy,
        bagua_enabled=b.enabled,
        bagua_filter_mode=b.filter_mode,
        gua_filter_meta=b.gua_filter_meta,
        gf=b.gf,
        bagua_n_before=b.n_before,
        bagua_n_after=b.n_after,
        n_events_raw_signals=ctx.n_events_raw_signals,
        n_events_after_weekday=ctx.n_events_after_weekday,
        errors=ctx.errors,
        signal_cache_hit=c.signal_hit,
        filter_cache_hit=c.filter_hit,
        execution_cache_hit=c.execution_hit,
        use_signal_cache=c.use_signal_cache,
        _fp_fields=_fp_fields,
    )


def run_portfolio_and_finalize(
    ctx: BacktestRunContext,
    *,
    cal: Any,
    events: Sequence[Any],
    raw_map: Dict[str, Any],
    adj_map: Dict[str, Any],
    progress: Callable[[dict], None],
    standard_qfq_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Four-lane portfolio phase + execution cache + artifact write."""
    ctx.apply_standard_qfq_raw_execution_v2()
    execution_bars = raw_map  # always raw for cash ledger

    progress(
        {
            "phase": "portfolio",
            "pct": 90.0,
            "current": ctx.n_codes,
            "total": ctx.n_codes,
            "message": "组合回测（信号 %d 条）" % len(events),
            "code": None,
            "n_signals": len(events),
        }
    )

    result = run_engine_with_ctx(
        ctx,
        cal=cal,
        events=events,
        execution_bars=execution_bars,
        adj_map=adj_map,
        standard_qfq_map=standard_qfq_map,
    )
    # Keep service-level policy in sync with engine resolution (esp. fast not_checked)
    ctx.price.corporate_action_policy = str(
        (result.metrics or {}).get("_resolved_corporate_action_policy")
        or (result.metrics or {}).get("corporate_action_policy")
        or ctx.price.corporate_action_policy
    )

    if ctx.unconfirmed_run:
        result.status = "research_unconfirmed_formula"
        result.notes = list(result.notes) + [
            "RESEARCH_UNCONFIRMED_FORMULA: paired source not user-confirmed; not formal.",
        ]

    result = apply_execution_cache(ctx, result, events)
    # Surface the selected explicit-event ledger in both metrics and repro.
    if ctx.ca_meta:
        result.metrics["ca_cache_root"] = ctx.ca_meta.get("cache_root")
        result.metrics["ca_event_manifest_sha256"] = ctx.ca_meta.get(
            "event_manifest_sha256"
        )
        result.metrics["ca_event_count"] = int(ctx.ca_meta.get("event_count") or 0)
        result.metrics["ca_event_symbol_count"] = int(
            ctx.ca_meta.get("event_symbol_count") or 0
        )
        result.metrics["ca_sync_failed"] = ctx.ca_meta.get("sync_failed")
    return finalize_with_ctx(ctx, result=result, events=events, progress=progress)
