"""Backtest service: multi-rule signals + PortfolioBacktester + reports."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..bagua.calculator import BaguaCalculator
from ..bagua.filter_rules import (
    DEFAULT_BAGUA_FILTER_MODE,
    DEFAULT_RULE_VERSION,
    GuaFilter,
    filter_events_by_bagua_mode,
    filter_events_by_gua_filter,
    gua_filter_history_summary,
    gua_filter_natural_language,
    mode_label as bagua_mode_label,
)
from ..config import AStockConfig, get_default_config
from ..data.adjustments import build_factor_series, factor_manifest_sha, formal_adjustment_ready
from ..data.calendar import TradeCalendar
from ..data.catalog import selected_universe_sha
from ..data.data_store import DataStore
from ..data.tdx_reader import TdxDayReader
from ..data.minline_reader import load_min60_daybars, min60_bars_to_arrays
from ..indicators.registry import IndicatorRegistry
from ..indicators.tn6_importer import load_source_map, resolve_formula_audit
from ..strategy import (
    PortfolioBacktester,
    filter_events_by_signal_weekdays,
    parse_price_session,
    parse_signal_weekdays,
    parse_single_weekday,
)
from ..study import (
    SignalEvent,
    attach_bagua,
    bars_dict_from_day,
    bars_dict_from_period,
    build_period_bars,
    combine_signals,
    compute_indicator_signal,
    compute_v5_dwm_resonance,
    day_bars_to_standard_qfq,
    day_bars_to_point_in_time_adjusted,
    day_bars_for_signals,
    signal_dates,
)
from .rules import RuleService

from .backtest_universe import (  # noqa: F401
    DEMO_CODES,
    FULL_MARKET_TOKENS,
    _astock_code_sha,
    _is_full_market_token,
    select_universe,
)
from .backtest_request import (  # noqa: F401
    BacktestRequest,
    research_fingerprint_fields_from_request,
)

class BacktestService:
    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self.cfg.ensure_dirs()
        self.rules = RuleService(self.cfg)

    def run(
        self,
        req: BacktestRequest,
        *,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return run_backtest(self.cfg, req, rules=self.rules, progress_cb=progress_cb)


def run_backtest(
    cfg: AStockConfig,
    req: BacktestRequest,
    *,
    rules: Optional[RuleService] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute portfolio backtest; returns summary dict (also writes outputs)."""
    cfg.ensure_dirs()
    rules = rules or RuleService(cfg)
    reg = rules.load_full_registry()
    store = DataStore(cfg.storage_root)
    period = (req.period or "DAY").upper()
    if req.dwm:
        period = "DWM"
    if period in ("M60", "60", "60M", "H1", "MIN_60"):
        period = "MIN60"
    hold = int(req.hold or 1)
    entry_lag = int(req.entry_lag or 1)
    if entry_lag < 1:
        raise ValueError("entry_lag must be >= 1")
    signal_weekdays = parse_signal_weekdays(getattr(req, "signal_weekdays", None))
    buy_on = parse_price_session(getattr(req, "buy_on", None), default="open")
    sell_on = parse_price_session(getattr(req, "sell_on", None), default="open")
    buy_weekday = parse_single_weekday(getattr(req, "buy_weekday", None))
    exit_weekday = parse_single_weekday(getattr(req, "exit_weekday", None))
    codes = select_universe(cfg, req.codes)

    def _progress(payload: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except Exception:
            pass

    n_codes = len(codes)
    _progress({
        "phase": "prepare",
        "pct": 2.0,
        "current": 0,
        "total": n_codes,
        "message": "准备回测，股票池 %d 只" % n_codes,
        "code": None,
    })

    try:
        specs = [reg.get(i) for i in req.rule_ids]
    except KeyError as e:
        raise ValueError(f"unknown rule: {e}") from e
    research_unadj = bool(req.research_unadjusted)
    min60_research_note = False
    if period == "MIN60":
        min60_research_note = True

    start = int(req.start) if req.start else None
    end = int(req.end) if req.end else None

    trade_specs = [s for s in specs if s.id != "bagua_ohlc" and s.output_type == "signal"]
    if not trade_specs:
        raise ValueError("No tradeable signal indicators selected.")

    for s in trade_specs:
        if s.compile_status == "source_required":
            raise ValueError(f"blocked source_required: {s.id}")
        if s.compile_status == "invalid":
            raise ValueError(f"blocked invalid: {s.id}: {s.failure_reason}")
        if s.compile_status == "unsupported":
            raise ValueError(f"blocked unsupported: {s.id}: {s.failure_reason}")
        if "MIN1" in (s.dependencies or []):
            raise ValueError(f"blocked MIN1 (no minute history): {s.id}")
        if "MIN60" in (s.dependencies or []) and not (
            getattr(s, "uses_min60_day_proxy", False)
            or (s.parameters or {}).get("min60_day_proxy")
        ):
            raise ValueError(
                f"blocked MIN60 without day proxy: {s.id}: {s.failure_reason}"
            )

    events: List[SignalEvent] = []
    raw_map: Dict[str, Any] = {}  # execution + valuation
    adj_map: Dict[str, Any] = {}  # point_in_time_adjusted research refs (legacy name)
    standard_qfq_map: Dict[str, Any] = {}  # default signal bars
    period_raw_map: Dict[str, Any] = {}  # L2 period bars (audit / future use)
    period_signal_map: Dict[str, Any] = {}  # L1 period bars for indicators + bagua
    factor_series = []
    errors: List[dict] = []
    combine = req.combine
    signal_cache_hit = False
    filter_cache_hit = False
    execution_cache_hit = False
    use_signal_cache = bool(getattr(req, "use_signal_cache", False))

    def _load_maps_and_maybe_signals(*, compute_signals: bool) -> List[SignalEvent]:
        """Always fill raw/adj/period maps + factor_series; optionally emit events."""
        local_events: List[SignalEvent] = []
        for idx, code in enumerate(codes):
            # signal phase occupies 5% ~ 85%
            if n_codes > 0:
                pct = 5.0 + 80.0 * (idx / float(n_codes))
            else:
                pct = 5.0
            if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == n_codes:
                _progress({
                    "phase": "signals" if compute_signals else "bars",
                    "pct": round(pct, 2),
                    "current": idx + 1,
                    "total": n_codes,
                    "message": (
                        "计算信号 %d/%d" % (idx + 1, n_codes)
                        if compute_signals
                        else "加载行情 %d/%d" % (idx + 1, n_codes)
                    ),
                    "code": code,
                })
            try:
                day_raw = store.load_symbol(code)
            except FileNotFoundError:
                reader = TdxDayReader(cfg.tdx_root)
                raw = ("sh" if code.startswith("SSE") else "sz") + code.split(".")[-1]
                day_raw, _ = reader.read(raw)
            raw_map[code] = day_raw
            dates = [b.date for b in day_raw]
            series = build_factor_series(code, dates, adj_root=cfg.adj_root, prefer_baostock=True)
            factor_series.append(series)
            import numpy as np

            fac = np.array(series.factors, dtype=float)
            # research/audit: 起点锚定复权 (factor_t / base_factor)
            day_pit = day_bars_to_point_in_time_adjusted(day_raw, fac)
            adj_map[code] = day_pit
            # signal bars: formal = standard_qfq; research_unadj = raw (single build)
            # L1 formal: asof_forward_qfq (anchor = run end or last bar; no CA after asof)
            _asof_sig = end if end else (day_raw[-1].date if day_raw else None)
            day_for_ind = day_bars_for_signals(
                day_raw,
                fac,
                research_unadjusted=research_unadj,
                signal_adjust="asof_forward_qfq",
                asof_date=_asof_sig,
                dates=dates,
            )
            # audit map: ordinary snapshot-end qfq (column 普通前复权参考)
            # signals use day_for_ind (asof_forward_qfq formal default)
            standard_qfq_map[code] = day_bars_to_standard_qfq(day_raw, fac)
            asof = day_raw[-1].date if day_raw else None

            if not compute_signals:
                # Fill period maps for bagua (L1 signal bars) when signal cache hits.
                if period == "DWM":
                    period_raw_map[code] = day_raw
                    period_signal_map[code] = day_for_ind
                elif period == "MIN60":
                    m60 = load_min60_daybars(cfg.tdx_root, code, start=start, end=end)
                    period_raw_map[code] = m60 or []
                    period_signal_map[code] = m60 or []
                else:
                    period_raw_map[code] = build_period_bars(day_raw, period, asof=asof)
                    period_signal_map[code] = build_period_bars(
                        day_for_ind, period, asof=asof
                    )
                continue

            local_events.extend(
                _events_for_code(code, day_raw, day_for_ind, asof)
            )
        return local_events

    def _events_for_code(code, day_raw, day_for_ind, asof) -> List[SignalEvent]:
        """Indicator signals for one code from already-built bar lanes."""
        local_events: List[SignalEvent] = []
        if period == "DWM":
            base = trade_specs[0]
            w_bars = build_period_bars(day_for_ind, "WEEK", asof=asof)
            m_bars = build_period_bars(day_for_ind, "MONTH", asof=asof)
            d_dict = bars_dict_from_day(day_for_ind)
            w_dict = bars_dict_from_period(w_bars)
            m_dict = bars_dict_from_period(m_bars)
            ds, e1 = compute_indicator_signal(base, d_dict)
            ws, e2 = compute_indicator_signal(base, w_dict)
            ms, e3 = compute_indicator_signal(base, m_dict)
            if ds is None or ws is None or ms is None:
                errors.append({"code": code, "dwm_errors": [e1, e2, e3]})
                return local_events
            res = compute_v5_dwm_resonance(day_for_ind, ds, w_bars, ws, m_bars, ms)
            period_raw_map[code] = day_raw
            period_signal_map[code] = day_for_ind
            for d in signal_dates(d_dict["date"], res):
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                local_events.append(SignalEvent(code, d, "DWM", f"{base.id}_dwm", is_dwm=True))
            return local_events

        if period == "MIN60":
            m60 = period_raw_map.get(code)
            if m60 is None:
                m60 = load_min60_daybars(
                    cfg.tdx_root,
                    code,
                    start=start,
                    end=end,
                )
                period_raw_map[code] = m60 or []
                period_signal_map[code] = m60 or []
            elif code not in period_signal_map:
                period_signal_map[code] = m60 or []
            if not m60:
                errors.append({"code": code, "indicator": "*", "error": "无60分钟线数据(.lc1)"})
                return local_events
            bars = min60_bars_to_arrays(m60)
            trade_dates = bars.get("trade_date")
        else:
            p_bars_ind = build_period_bars(day_for_ind, period, asof=asof)
            p_bars_raw = build_period_bars(day_raw, period, asof=asof)
            period_raw_map[code] = p_bars_raw
            period_signal_map[code] = p_bars_ind
            bars = bars_dict_from_day(p_bars_ind) if period == "DAY" else bars_dict_from_period(p_bars_ind)
            trade_dates = None
        sigs = []
        for spec in trade_specs:
            sig, err = compute_indicator_signal(spec, bars)
            if err:
                errors.append({"code": code, "indicator": spec.id, "error": err})
                continue
            sigs.append(sig)
            if not combine:
                date_arr = bars["date"]
                for i, d in enumerate(date_arr):
                    try:
                        on = int(sig[i]) != 0 and not (
                            isinstance(sig[i], float) and __import__("math").isnan(sig[i])
                        )
                    except Exception:
                        on = bool(sig[i])
                    if not on:
                        continue
                    if trade_dates is not None and i < len(trade_dates):
                        d_out = int(trade_dates[i])
                    else:
                        d_out = int(d)
                    if start and d_out < start:
                        continue
                    if end and d_out > end:
                        continue
                    local_events.append(SignalEvent(code, d_out, period, spec.id))
        if combine and sigs:
            combined = sigs[0] if len(sigs) == 1 else combine_signals(sigs, mode=combine)
            date_arr = bars["date"]
            for i, d in enumerate(date_arr):
                try:
                    on = int(combined[i]) != 0
                except Exception:
                    on = bool(combined[i])
                if not on:
                    continue
                if trade_dates is not None and i < len(trade_dates):
                    d_out = int(trade_dates[i])
                else:
                    d_out = int(d)
                if start and d_out < start:
                    continue
                if end and d_out > end:
                    continue
                local_events.append(SignalEvent(code, d_out, period, f"combine_{combine}"))
        return local_events

    def _compute_events_from_loaded_maps() -> List[SignalEvent]:
        """Compute signals using maps already filled by a prior bars/factors load."""
        local_events: List[SignalEvent] = []
        for idx, code in enumerate(codes):
            if n_codes > 0:
                pct = 5.0 + 80.0 * (idx / float(n_codes))
            else:
                pct = 5.0
            if idx == 0 or (idx + 1) % 5 == 0 or (idx + 1) == n_codes:
                _progress({
                    "phase": "signals",
                    "pct": round(pct, 2),
                    "current": idx + 1,
                    "total": n_codes,
                    "message": "计算信号 %d/%d" % (idx + 1, n_codes),
                    "code": code,
                })
            day_raw = raw_map.get(code) or []
            if research_unadj:
                day_for_ind = day_raw
            else:
                day_for_ind = standard_qfq_map.get(code) or day_raw
            asof = day_raw[-1].date if day_raw else None
            local_events.extend(_events_for_code(code, day_raw, day_for_ind, asof))
        return local_events

    def _make_signal_cache_key(factor_manifest: str) -> str:
        from ..research.signal_cache import signal_cache_key

        _ind_src = "|".join(
            sorted(
                "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                for s in trade_specs
            )
        )
        return signal_cache_key(
            indicator_ids=[s.id for s in trade_specs],
            indicator_source_hash=_ind_src,
            period=period,
            start=start,
            end=end,
            universe_hash=selected_universe_sha(codes),
            adjust_mode=("research_unadjusted" if research_unadj else "asof_forward_qfq"),
            factor_manifest_sha=factor_manifest or "",
            combine=combine,
        )

    if use_signal_cache:
        try:
            from ..research.signal_cache import get_or_compute_signals

            # Load bars/factors first so the cache key pins factor_manifest_sha.
            # Without this, standard_qfq signals can change after CA updates while
            # the disk cache still returns stale events.
            _load_maps_and_maybe_signals(compute_signals=False)
            _factor_manifest = factor_manifest_sha(factor_series)
            _sig_key = _make_signal_cache_key(_factor_manifest)

            events, signal_cache_hit = get_or_compute_signals(
                _sig_key,
                _compute_events_from_loaded_maps,
                cfg=cfg,
                use_cache=True,
                meta={
                    "period": period,
                    "n_codes": n_codes,
                    "rule_ids": [s.id for s in trade_specs],
                    "factor_manifest_sha": _factor_manifest,
                },
            )
            if signal_cache_hit:
                _progress({
                    "phase": "signals",
                    "pct": 85.0,
                    "current": n_codes,
                    "total": n_codes,
                    "message": "信号缓存命中 %d 条" % len(events),
                    "code": None,
                    "signal_cache_hit": True,
                })
        except Exception as _cache_err:
            # Fail open: recompute without cache
            signal_cache_hit = False
            if not raw_map:
                events = _load_maps_and_maybe_signals(compute_signals=True)
            else:
                events = _compute_events_from_loaded_maps()
            errors.append({"code": "*", "indicator": "signal_cache", "error": str(_cache_err)[:200]})
    else:
        events = _load_maps_and_maybe_signals(compute_signals=True)

    # Weekday filter BEFORE bagua: "周五信号 + 最佳3爻" = Friday tech signals that pass gua.
    n_events_raw_signals = len(events)
    n_events_after_weekday = n_events_raw_signals
    if signal_weekdays:
        events = filter_events_by_signal_weekdays(events, signal_weekdays)
        n_events_after_weekday = len(events)

    _progress({
        "phase": "factors",
        "pct": 86.0,
        "current": n_codes,
        "total": n_codes,
        "message": "校验复权因子",
        "code": None,
    })

    formal_ok, adj_msg = formal_adjustment_ready(factor_series)
    if not formal_ok and not research_unadj:
        # Persist a failed run so the UI history is not empty and the message
        # is inspectable later. Still No-Go (no portfolio metrics).
        run_id = req.run_id or f"bt_nogo_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        out_dir = Path(cfg.output_root) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": run_id,
            "status": "no_go",
            "reason": adj_msg,
            "error": adj_msg,
            "indicator_ids": [s.id for s in trade_specs],
            "hold": hold,
            "entry_lag": entry_lag,
            "signal_weekdays": signal_weekdays,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "schedule_mode": (
                "weekday"
                if (buy_weekday is not None or exit_weekday is not None)
                else "tn"
            ),
            "period": period,
            "start": start,
            "end": end,
            "selected_codes_count": len(codes),
            "request": req.to_dict(),
            "n_signals": len(events),
            **(research_fingerprint_fields_from_request(req) or {}),
            "hint": (
                "复权因子不完整，正式回测已拒绝。"
                "可勾选「研究未复权」重跑，或先修复 adjustments 缓存后重试正式模式。"
            ),
            "metrics": {},
        }
        (out_dir / "run_meta.json").write_text(
            __import__("json").dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            from .runs import append_run_index

            append_run_index(
                cfg,
                {
                    "run_id": run_id,
                    "status": "no_go",
                    "created_at": int(time.time()),
                    "indicator_ids": [s.id for s in trade_specs],
                    "hold": hold,
                    "entry_lag": entry_lag,
                    "buy_weekday": buy_weekday,
                    "exit_weekday": exit_weekday,
                    "buy_on": buy_on,
                    "sell_on": sell_on,
                    "signal_weekdays": signal_weekdays,
                    "schedule_mode": meta["schedule_mode"],
                    "period": period,
                    "metrics": None,
                    "error": adj_msg[:500],
                },
            )
        except Exception:
            pass
        _progress({
            "phase": "failed",
            "pct": 86.0,
            "current": n_codes,
            "total": n_codes,
            "message": (adj_msg or "")[:200],
            "code": None,
            "run_id": run_id,
        })
        return {
            "status": "no_go",
            "reason": adj_msg,
            "run_id": run_id,
            "error": adj_msg,
            "hint": meta["hint"],
        }

    mapping = load_source_map(cfg.mapping_path)
    formula_audits: Dict[str, Any] = {}
    allow_unconfirmed = bool(req.research_unconfirmed_formula)
    for s in trade_specs:
        entry = None
        if s.package_sha256 and s.package_sha256 in mapping:
            entry = dict(mapping[s.package_sha256])
            if s.package_file:
                entry["package_file"] = str(Path(s.package_file).resolve())
        elif s.source_sha256:
            entry = {
                "package_sha256": None,
                "source_sha256": s.source_sha256,
                "source_file": s.source_file,
                "formula_provenance": "txt_self_source",
                "source_pair_status": "txt_only",
                "formal_backtest_allowed": True,
                "research_backtest_allowed": True,
                "confirmation": {
                    "package_sha256": None,
                    "source_sha256": s.source_sha256,
                    "confirmed_by": "txt_self",
                    "confirmation_method": "txt_only",
                    "note": "standalone txt formula",
                    "schema_version": 1,
                },
            }
        audit = resolve_formula_audit(entry, package_sha256=s.package_sha256)
        if entry and entry.get("source_pair_status") == "txt_only" and s.compile_status == "ready":
            audit = {
                "formula_provenance": "txt_self_source",
                "source_pair_status": "txt_only",
                "formal_backtest_allowed": True,
                "research_backtest_allowed": True,
            }
        formula_audits[s.id] = audit
        if s.package_sha256 and not audit.get("formal_backtest_allowed", False):
            if not allow_unconfirmed:
                return {
                    "status": "rejected_unconfirmed_formula",
                    "indicator": s.id,
                    "audit": audit,
                    "error": "unconfirmed formula; use research_unconfirmed_formula or confirm source",
                    "run_id": None,
                }

    gf = GuaFilter.from_dict(getattr(req, "gua_filter", None))
    try:
        from .gua import rule_version as _gua_rule_ver, hexagram_name_map, state_label_map

        if not gf.rule_version:
            gf.rule_version = _gua_rule_ver(cfg)
    except Exception:
        if not gf.rule_version:
            gf.rule_version = DEFAULT_RULE_VERSION

    bagua_enabled = (
        any(s.id == "bagua_ohlc" for s in specs)
        or bool(req.with_bagua)
        or gf.is_active()
    )
    bagua_filter_mode = None
    bagua_n_before = len(events)
    bagua_n_after = len(events)
    gua_filter_meta = None
    filter_cache_hit = False
    _filter_key = None
    # Phase-3: filter-layer cache (gua / bagua mode). Signal weekdays still applied in engine.
    if bagua_enabled:
        def _apply_bagua_filters(src_events):
            nonlocal bagua_filter_mode, gua_filter_meta, bagua_n_before, bagua_n_after
            evs = list(src_events)
            calc = BaguaCalculator.from_json(cfg.bagua_json)
            # L1: bagua OHLC must match technical signal bars (not L2 raw).
            attach_bagua(evs, period_signal_map, calc)
            bagua_n_before = len(evs)
            if gf.is_active():
                try:
                    from ..bagua.filter_rules import compute_bagua_metrics
                    bagua_metrics_pre = compute_bagua_metrics(evs, gf)
                except Exception:
                    bagua_metrics_pre = None
                evs = filter_events_by_gua_filter(evs, gf)
                bagua_n_after = len(evs)
                bagua_filter_mode = f"gua_filter:{gf.selection_mode}"
                try:
                    names = hexagram_name_map(cfg)
                    labels = state_label_map(cfg)
                except Exception:
                    names, labels = {}, {}
                gua_filter_meta = {
                    **gf.to_dict(),
                    "natural_language": gua_filter_natural_language(gf, hexagram_names=names),
                    "history_summary": gua_filter_history_summary(
                        gf, hexagram_names=names, state_labels=labels
                    ),
                    "n_signals_before": bagua_n_before,
                    "n_signals_after": bagua_n_after,
                    "retention_rate": (
                        (bagua_n_after / bagua_n_before) if bagua_n_before else 0.0
                    ),
                }
                if bagua_metrics_pre is not None:
                    gua_filter_meta["bagua_metrics"] = bagua_metrics_pre
            else:
                bagua_filter_mode = (req.bagua_filter_mode or DEFAULT_BAGUA_FILTER_MODE).strip()
                evs = filter_events_by_bagua_mode(evs, bagua_filter_mode)
                bagua_n_after = len(evs)
            return evs

        if use_signal_cache:
            try:
                from ..research.filter_cache import filter_cache_key, get_or_compute_filtered

                # Prefer raw signal cache key if available
                _sk = locals().get("_sig_key")
                if not _sk:
                    from ..research.signal_cache import signal_cache_key as _sck
                    _ind_src2 = "|".join(
                        sorted(
                            "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                            for s in trade_specs
                        )
                    )
                    _sk = _sck(
                        indicator_ids=[s.id for s in trade_specs],
                        indicator_source_hash=_ind_src2,
                        period=period,
                        start=start,
                        end=end,
                        universe_hash=selected_universe_sha(codes),
                        adjust_mode=("research_unadjusted" if research_unadj else "asof_forward_qfq"),
                        factor_manifest_sha=factor_manifest_sha(factor_series),
                        combine=combine,
                    )
                _filter_key = filter_cache_key(
                    signal_cache_key=str(_sk),
                    signal_weekdays=signal_weekdays,
                    gua_rule_version=getattr(gf, "rule_version", None),
                    gua_filter=gf.to_dict() if gf else None,
                    with_bagua=bool(req.with_bagua) or bagua_enabled,
                    bagua_filter_mode=getattr(req, "bagua_filter_mode", None),
                    extra={"bagua_ohlc_plane": "L1_signal_price", "bagua_ohlc_source": "signal_period_bars"},
                )
                # Cache stores post-filter events; bagua attach needs period_signal_map on miss.
                # On hit, bagua fields are already on events from prior save.
                # Filter keys inherit signal_cache_key (adjust_mode + factor_manifest).
                events, filter_cache_hit = get_or_compute_filtered(
                    _filter_key,
                    lambda: _apply_bagua_filters(events),
                    cfg=cfg,
                    use_cache=True,
                    meta={"layer": "bagua_filter"},
                )
                bagua_n_after = len(events)
                if filter_cache_hit:
                    bagua_n_before = bagua_n_before or bagua_n_after
                    if gf.is_active():
                        bagua_filter_mode = bagua_filter_mode or f"gua_filter:{gf.selection_mode}"
                        if gua_filter_meta is None:
                            gua_filter_meta = {
                                **gf.to_dict(),
                                "n_signals_before": bagua_n_before,
                                "n_signals_after": bagua_n_after,
                                "retention_rate": (
                                    (bagua_n_after / bagua_n_before) if bagua_n_before else 0.0
                                ),
                                "from_filter_cache": True,
                            }
                    else:
                        bagua_filter_mode = (
                            bagua_filter_mode
                            or (req.bagua_filter_mode or DEFAULT_BAGUA_FILTER_MODE).strip()
                        )
                msg = (
                    ("过滤缓存命中 · " if filter_cache_hit else "")
                    + (
                        f"卦象过滤：{bagua_n_before} → {bagua_n_after} 条信号"
                        if gf.is_active()
                        else f"八卦过滤：{bagua_n_before} → {bagua_n_after} 条信号"
                    )
                )
            except Exception as _fc_err:
                filter_cache_hit = False
                events = _apply_bagua_filters(events)
                errors.append(
                    {"code": "*", "indicator": "filter_cache", "error": str(_fc_err)[:200]}
                )
                msg = f"卦象/八卦过滤：{bagua_n_before} → {bagua_n_after} 条信号"
        else:
            events = _apply_bagua_filters(events)
            msg = (
                f"卦象过滤·{gf.selection_mode}：{bagua_n_before} → {bagua_n_after} 条信号"
                if gf.is_active()
                else (
                    f"八卦过滤·{bagua_mode_label(bagua_filter_mode)}："
                    f"{bagua_n_before} → {bagua_n_after} 条信号"
                )
            )
        _progress({
            "phase": "bagua_filter",
            "pct": 88.0,
            "current": n_codes,
            "total": n_codes,
            "message": msg,
            "code": None,
            "n_signals": bagua_n_after,
            "n_signals_before_bagua": bagua_n_before,
            "filter_cache_hit": filter_cache_hit,
        })

    try:
        cal = TradeCalendar.load(cfg.calendar_path)
    except Exception:
        cal = TradeCalendar.from_tdx(cfg.tdx_root)

    run_id = req.run_id or f"bt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    unconfirmed_run = bool(
        allow_unconfirmed
        and any(
            a.get("source_pair_status")
            in (
                "paired_unconfirmed",
                "stale_source_hash",
                "source_missing",
                "package_missing",
                "stale_package_hash",
            )
            for a in formula_audits.values()
        )
    )
    # four-lane portfolio + artifacts (schedule/price/bagua/cache via context)
    engine = (getattr(req, "engine", None) or "full").strip().lower()
    holiday_policy = getattr(req, "holiday_policy", None) or "next_trading_day"
    artifact_level = getattr(req, "artifact_level", None) or "full"
    try:
        from ..research.artifacts import apply_artifact_policy, normalize_artifact_level

        artifact_level = normalize_artifact_level(artifact_level, default="full")
        art_flags = apply_artifact_policy(level=artifact_level)
    except Exception:
        art_flags = {
            "write_signals": True,
            "write_fills": True,
            "write_excel": True,
            "write_equity": True,
            "write_meta": True,
        }

    from .backtest_context import (
        BacktestRunContext,
        BaguaState,
        CacheState,
        PriceModes,
        ScheduleParams,
        run_portfolio_and_finalize,
    )

    ctx = BacktestRunContext(
        cfg=cfg,
        req=req,
        codes=codes,
        schedule=ScheduleParams(
            period=period,
            hold=hold,
            entry_lag=entry_lag,
            buy_on=buy_on,
            sell_on=sell_on,
            buy_weekday=buy_weekday,
            exit_weekday=exit_weekday,
            signal_weekdays=signal_weekdays,
            holiday_policy=holiday_policy,
            start=start,
            end=end,
        ),
        price=PriceModes(
            research_unadj=research_unadj,
            formal_ok=formal_ok,
            adj_msg=adj_msg,
        ),
        bagua=BaguaState(
            enabled=bagua_enabled,
            filter_mode=bagua_filter_mode,
            gua_filter_meta=gua_filter_meta,
            gf=gf,
            n_before=bagua_n_before,
            n_after=bagua_n_after,
        ),
        cache=CacheState(
            signal_hit=signal_cache_hit,
            filter_hit=filter_cache_hit,
            execution_hit=execution_cache_hit,
            use_signal_cache=use_signal_cache,
        ),
        combine=combine,
        engine=engine,
        artifact_level=artifact_level,
        art_flags=art_flags,
        run_id=run_id,
        trade_specs=trade_specs,
        formula_audits=formula_audits,
        factor_series=factor_series,
        unconfirmed_run=unconfirmed_run,
        n_events_raw_signals=n_events_raw_signals,
        n_events_after_weekday=n_events_after_weekday,
        errors=errors,
    )
    return run_portfolio_and_finalize(
        ctx,
        cal=cal,
        events=events,
        raw_map=raw_map,
        adj_map=adj_map,
        standard_qfq_map=standard_qfq_map,
        progress=_progress,
    )
