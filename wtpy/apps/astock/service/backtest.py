"""Backtest service: multi-rule signals + PortfolioBacktester + reports."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..bagua.calculator import BaguaCalculator
from ..bagua.filter_rules import (
    DEFAULT_BAGUA_FILTER_MODE,
    DEFAULT_RULE_VERSION,
    GuaFilter,
    best3_display_pairs,
    filter_events_by_bagua_mode,
    filter_events_by_gua_filter,
    gua_filter_history_summary,
    gua_filter_natural_language,
    mode_label as bagua_mode_label,
)
from ..config import AStockConfig, get_default_config
from ..data.adjustments import build_factor_series, factor_manifest_sha, formal_adjustment_ready
from ..data.calendar import TradeCalendar
from ..data.catalog import file_sha_or_empty, selected_universe_sha
from ..data.data_store import DataStore
from ..data.tdx_reader import TdxDayReader
from ..data.minline_reader import load_min60_daybars, min60_bars_to_arrays
from ..data.universe import AShareUniverse
from ..indicators.registry import IndicatorRegistry
from ..indicators.tn6_importer import load_source_map, resolve_formula_audit
from ..reports import write_backtest_csv, write_signals_csv
from ..strategy import (
    PortfolioBacktester,
    filter_events_by_signal_weekdays,
    format_signal_weekdays,
    parse_price_session,
    parse_signal_weekdays,
    parse_single_weekday,
    format_single_weekday,
    session_label_cn,
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
    day_bars_to_adj,
    signal_dates,
)
from .rules import RuleService


def _astock_code_sha() -> str:
    import hashlib

    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    files = sorted(
        [
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix in {".py", ".json"}
            and "__pycache__" not in p.parts
        ],
        key=lambda p: str(p.relative_to(root)).replace("\\", "/"),
    )
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


DEMO_CODES = ["SSE.STK.600000", "SZSE.STK.000001"]

FULL_MARKET_TOKENS = frozenset(
    {
        "*",
        "ALL",
        "ALL_A",
        "ALL_MARKET",
        "FULL",
        "FULL_MARKET",
        "全市场",
        "全部A股",
        "全部",
    }
)


def _is_full_market_token(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if t in FULL_MARKET_TOKENS:
        return True
    return t.upper() in {x.upper() for x in FULL_MARKET_TOKENS if x.isascii()}


def select_universe(cfg: AStockConfig, codes: Optional[Union[Sequence[str], str]]) -> List[str]:
    """Resolve stock universe.

    - None / empty / full-market token -> entire universe.json (all A-shares)
    - otherwise parse comma list or sequence of codes
    """
    from ..data.universe import to_std_code

    def _full() -> List[str]:
        if cfg.universe_path.exists():
            return AShareUniverse.load(cfg.universe_path).codes()
        return list(DEMO_CODES)

    if codes is None:
        return _full()
    if isinstance(codes, str):
        parts = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        parts = [str(c).strip() for c in codes if str(c).strip()]
    if not parts:
        return _full()
    if any(_is_full_market_token(c) for c in parts):
        return _full()
    out: List[str] = []
    for c in parts:
        if c.startswith("SSE.") or c.startswith("SZSE."):
            out.append(c)
        else:
            out.append(to_std_code(c))
    return out if out else _full()


@dataclass
class BacktestRequest:
    rule_ids: List[str]
    period: str = "DAY"
    hold: int = 1
    entry_lag: int = 1
    # ISO weekdays 1=Mon..7=Sun; empty/None = all. Example: [5] = Friday only.
    signal_weekdays: Optional[List] = None
    # open | close — default open (T+N open buy / open sell)
    buy_on: str = "open"
    sell_on: str = "open"
    # Preferred UI: ISO weekday 1=Mon..7=Sun; overrides entry_lag / hold when set
    buy_weekday: Optional[int] = None
    exit_weekday: Optional[int] = None
    combine: Optional[str] = None  # all | any | None
    codes: Optional[List[str]] = None
    start: Optional[int] = None
    end: Optional[int] = None
    dwm: bool = False
    with_bagua: bool = False
    # When with_bagua / bagua_ohlc is on: default best3 (最佳3爻) filter.
    bagua_filter_mode: Optional[str] = None
    # Flexible gua filter (UI); when active, takes precedence over bagua_filter_mode.
    gua_filter: Optional[dict] = None
    research_unadjusted: bool = False
    research_unconfirmed_formula: bool = False
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # portfolio = shared cash; per_symbol = TDX-style independent capital per stock
    account_mode: str = "portfolio"
    run_id: Optional[str] = None
    # Phase-3 research options
    engine: str = "full"  # full | fast
    use_signal_cache: bool = False
    artifact_level: str = "full"  # summary | candidate | full
    holiday_policy: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)





def research_fingerprint_fields_from_request(
    req: "BacktestRequest",
    *,
    costs: Optional[Dict[str, Any]] = None,
    universe_hash: Optional[str] = None,
    indicator_source_hash: Optional[str] = None,
    engine_code_hash: Optional[str] = None,
    market_data_version: Optional[str] = None,
    calendar_version: Optional[str] = None,
    bagua_json_hash: Optional[str] = None,
) -> Dict[str, str]:
    """Compute research fingerprint metadata for run summary / repro / index.

    Does not replace experiment ``param_hash`` (request-params only). Returns
    16-char ``research_fingerprint`` plus cheap component hashes.
    """
    from ..research.fingerprint import research_fingerprint_from_params

    params = req.to_dict() if hasattr(req, "to_dict") else dict(req or {})
    # Align with fingerprint param keys (indicator_ids preferred over rule_ids)
    if params.get("rule_ids") and not params.get("indicator_ids"):
        params = dict(params)
        params["indicator_ids"] = params.get("rule_ids")
    fp = research_fingerprint_from_params(
        params,
        costs=costs,
        engine_code_hash=engine_code_hash,
        universe_hash=universe_hash,
        indicator_source_hash=indicator_source_hash,
        market_data_version=market_data_version,
        calendar_version=calendar_version,
        bagua_json_hash=bagua_json_hash,
    )
    return {
        "research_fingerprint": fp.full_hex(16),
        "signal_fp": fp.signal_hex(16),
        "filter_fp": fp.filter_hex(16),
        "execution_fp": fp.execution_hex(16),
    }


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
    raw_map: Dict[str, Any] = {}
    adj_map: Dict[str, Any] = {}
    period_raw_map: Dict[str, Any] = {}
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
            day_adj = day_bars_to_adj(day_raw, fac)
            adj_map[code] = day_adj
            day_for_ind = day_raw if research_unadj else day_adj
            asof = day_raw[-1].date if day_raw else None

            if not compute_signals:
                # Still need period_raw_map for bagua attach when cache hit
                if period == "DWM":
                    period_raw_map[code] = day_raw
                elif period == "MIN60":
                    m60 = load_min60_daybars(cfg.tdx_root, code, start=start, end=end)
                    period_raw_map[code] = m60 or []
                else:
                    period_raw_map[code] = build_period_bars(day_raw, period, asof=asof)
                continue

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
                    continue
                res = compute_v5_dwm_resonance(day_for_ind, ds, w_bars, ws, m_bars, ms)
                period_raw_map[code] = day_raw
                for d in signal_dates(d_dict["date"], res):
                    if start and d < start:
                        continue
                    if end and d > end:
                        continue
                    local_events.append(SignalEvent(code, d, "DWM", f"{base.id}_dwm", is_dwm=True))
            else:
                if period == "MIN60":
                    m60 = load_min60_daybars(
                        cfg.tdx_root,
                        code,
                        start=start,
                        end=end,
                    )
                    if not m60:
                        errors.append({"code": code, "indicator": "*", "error": "无60分钟线数据(.lc1)"})
                        continue
                    period_raw_map[code] = m60
                    bars = min60_bars_to_arrays(m60)
                    trade_dates = bars.get("trade_date")
                else:
                    p_bars_ind = build_period_bars(day_for_ind, period, asof=asof)
                    p_bars_raw = build_period_bars(day_raw, period, asof=asof)
                    period_raw_map[code] = p_bars_raw
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

    if use_signal_cache:
        try:
            from ..research.signal_cache import get_or_compute_signals, signal_cache_key

            _ind_src = "|".join(
                sorted(
                    "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                    for s in trade_specs
                )
            )
            _sig_key = signal_cache_key(
                indicator_ids=[s.id for s in trade_specs],
                indicator_source_hash=_ind_src,
                period=period,
                start=start,
                end=end,
                universe_hash=selected_universe_sha(codes),
                adjust_mode=("research_unadjusted" if research_unadj else "adjusted"),
                combine=combine,
            )

            def _compute_signals():
                return _load_maps_and_maybe_signals(compute_signals=True)

            events, signal_cache_hit = get_or_compute_signals(
                _sig_key,
                _compute_signals,
                cfg=cfg,
                use_cache=True,
                meta={
                    "period": period,
                    "n_codes": n_codes,
                    "rule_ids": [s.id for s in trade_specs],
                },
            )
            if signal_cache_hit:
                # Cache stores events only — still need bars/factors for portfolio
                _load_maps_and_maybe_signals(compute_signals=False)
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
            events = _load_maps_and_maybe_signals(compute_signals=True)
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
            attach_bagua(evs, period_raw_map, calc)
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
                        adjust_mode=("research_unadjusted" if research_unadj else "adjusted"),
                        combine=combine,
                    )
                _filter_key = filter_cache_key(
                    signal_cache_key=str(_sk),
                    signal_weekdays=signal_weekdays,
                    gua_rule_version=getattr(gf, "rule_version", None),
                    gua_filter=gf.to_dict() if gf else None,
                    with_bagua=bool(req.with_bagua) or bagua_enabled,
                    bagua_filter_mode=getattr(req, "bagua_filter_mode", None),
                )
                # Cache stores post-filter events; bagua attach needs period_raw_map on miss.
                # On hit, bagua fields are already on events from prior save.
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
    # dual_price_v1:
    # - signal: causal_qfq (adj_map) unless research_unadjusted → raw
    # - execution + valuation: ALWAYS raw_map (true tape / cash ledger)
    # - adj_map is audit/reference only for fills — never trade_map for PnL
    signal_price_mode = "raw" if research_unadj else "causal_qfq"
    execution_price_mode = "raw"
    valuation_price_mode = "raw"
    corporate_action_policy = "fail_closed"
    engine_result_version = "dual_price_v1"
    # Legacy sole price_mode: do not mean "fills are adjusted" anymore.
    price_mode = "dual_price_v1"
    execution_bars = raw_map
    signal_bars_ref = raw_map if research_unadj else adj_map
    if research_unadj:
        use_research = True
        use_formal_ok = True
    else:
        use_research = False
        use_formal_ok = formal_ok

    _progress({
        "phase": "portfolio",
        "pct": 90.0,
        "current": n_codes,
        "total": n_codes,
        "message": "组合回测（信号 %d 条）" % len(events),
        "code": None,
        "n_signals": len(events),
    })

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

    if engine in ("fast", "quick", "research_fast"):
        from ..research.fast_engine import run_fast_backtest

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
        )
        # Adapt to BacktestResult-like surface for writers / history
        from ..strategy import BacktestResult as _BTR

        result = _BTR(
            run_id=run_id,
            config=dict(fast_res.config),
            fills=[],
            equity_curve=[],
            metrics=dict(fast_res.metrics),
            notes=list(fast_res.notes),
            status="ok" if not use_research else "research_unadjusted",
        )
        result.metrics["engine"] = "fast"
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
    else:
        # factor maps for corporate-action fail-closed (entry factor vs later factor)
        factor_by_code = {}
        try:
            for _fs in factor_series or []:
                code_k = getattr(_fs, "std_code", None) or getattr(_fs, "code", None)
                if not code_k:
                    continue
                dates_f = list(getattr(_fs, "dates", None) or [])
                facs_f = list(getattr(_fs, "factors", None) or [])
                if dates_f and facs_f and len(dates_f) == len(facs_f):
                    factor_by_code[str(code_k)] = {
                        int(d): float(f) for d, f in zip(dates_f, facs_f)
                    }
        except Exception:
            factor_by_code = {}
        bt = PortfolioBacktester(
            cfg,
            cal,
            execution_bars,
            adj_bars_by_code=adj_map if not research_unadj else None,
            factor_by_code=factor_by_code or None,
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
            stop_loss_pct=req.stop_loss,
            take_profit_pct=req.take_profit,
            entry_lag=entry_lag,
            account_mode=getattr(req, "account_mode", None) or "portfolio",
            signal_weekdays=signal_weekdays,
            buy_on=buy_on,
            sell_on=sell_on,
            buy_weekday=buy_weekday,
            exit_weekday=exit_weekday,
            holiday_policy=holiday_policy,
        )
        result.config["engine"] = "full"
        result.config["artifact_level"] = artifact_level
        result.metrics["n_events"] = len(events)
        result.metrics["n_events_raw_signals"] = int(n_events_raw_signals)
        result.metrics["n_events_after_weekday"] = int(n_events_after_weekday)
        if bagua_enabled:
            result.metrics["n_signals_before_bagua"] = bagua_n_before
            result.metrics["n_signals_after_bagua"] = bagua_n_after
    if unconfirmed_run:
        result.status = "research_unconfirmed_formula"
        result.notes = list(result.notes) + [
            "RESEARCH_UNCONFIRMED_FORMULA: paired source not user-confirmed; not formal.",
        ]

    # Phase-3 execution cache: store metrics for identical screen runs (fast/summary)
    try:
        from ..research.execution_cache import (
            execution_cache_key,
            load_execution_cache,
            save_execution_cache,
        )
        from dataclasses import asdict as _asdict_c

        _ex_payload = {
            "engine": "fast" if engine in ("fast", "quick", "research_fast") else "full",
            "engine_result_version": engine_result_version,
            "signal_price_mode": signal_price_mode,
            "execution_price_mode": execution_price_mode,
            "valuation_price_mode": valuation_price_mode,
            "corporate_action_policy": corporate_action_policy,
            "artifact_level": artifact_level,
            "rule_ids": [s.id for s in trade_specs],
            "period": period,
            "hold": hold,
            "entry_lag": entry_lag,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "signal_weekdays": signal_weekdays,
            "holiday_policy": holiday_policy,
            "stop_loss": req.stop_loss,
            "take_profit": req.take_profit,
            "account_mode": getattr(req, "account_mode", None) or "portfolio",
            "start": start,
            "end": end,
            "universe": selected_universe_sha(codes),
            # legacy key kept but dual modes are authoritative for cache isolation
            "adjust": "research_unadjusted" if research_unadj else "signal_causal_qfq_exec_raw",
            "gua": (gf.to_dict() if gf else None),
            "with_bagua": bagua_enabled,
            "bagua_filter_mode": bagua_filter_mode,
            "n_events": len(events),
            "costs": _asdict_c(cfg.costs),
        }
        _ex_key = execution_cache_key(_ex_payload)
        # Only auto-load for fast+summary screening to avoid stale formal full results
        if (
            use_signal_cache
            and engine in ("fast", "quick", "research_fast")
            and artifact_level == "summary"
        ):
            _ex_hit = load_execution_cache(_ex_key, cfg=cfg)
            if _ex_hit and isinstance(_ex_hit.get("metrics"), dict):
                execution_cache_hit = True
                result.metrics = dict(_ex_hit["metrics"])
                result.metrics["execution_cache_hit"] = True
                result.notes = list(result.notes) + ["execution_cache_hit"]
        if not execution_cache_hit and use_signal_cache:
            try:
                save_execution_cache(
                    _ex_key,
                    metrics=dict(result.metrics or {}),
                    meta={"run_id": run_id, "engine": engine},
                    cfg=cfg,
                )
            except Exception:
                pass
    except Exception:
        pass

    out_dir = cfg.output_root / run_id
    bagua_sha = ""
    try:
        bagua_sha = __import__("hashlib").sha256(Path(cfg.bagua_json).read_bytes()).hexdigest()
    except Exception:
        pass
    global_manifest_sha = file_sha_or_empty(cfg.manifest_path)
    global_universe_sha = file_sha_or_empty(cfg.universe_path)
    global_universe_count = 0
    if cfg.universe_path.exists():
        try:
            global_universe_count = len(AShareUniverse.load(cfg.universe_path))
        except Exception:
            global_universe_count = 0
    sel_sha = selected_universe_sha(codes)
    primary_audit: Dict[str, Any] = {}
    for s in trade_specs:
        primary_audit = formula_audits.get(s.id, {})
        break
    repro = {
        "price_mode": price_mode,
        "signal_price_mode": signal_price_mode,
        "execution_price_mode": execution_price_mode,
        "valuation_price_mode": valuation_price_mode,
        "corporate_action_policy": corporate_action_policy,
        "engine_result_version": engine_result_version,
        "price_mode_note": (
            "dual_price_v1: signals use causal_qfq (or raw if research_unadjusted); "
            "fills/equity use unadjusted market prices. "
            "Legacy price_mode=adjusted meant fills on adj bars — obsolete."
        ),
        "risk_conflict_policy": "stop_first",
        "risk_trigger_policy": "daily_high_low",
        "risk_execution_policy": "next_trading_day_open",
        "entry_lag": entry_lag,
        "signal_weekdays": signal_weekdays,
        "buy_on": buy_on,
        "sell_on": sell_on,
        "buy_weekday": buy_weekday,
        "exit_weekday": exit_weekday,
        "schedule_mode": (
            "weekday" if (buy_weekday is not None or exit_weekday is not None) else "tn"
        ),
        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
        "stop_loss_pct": req.stop_loss,
        "take_profit_pct": req.take_profit,
        "formula_provenance": primary_audit.get("formula_provenance"),
        "source_pair_status": primary_audit.get("source_pair_status"),
        "formal_backtest_allowed": primary_audit.get("formal_backtest_allowed"),
        "confirmed_by": primary_audit.get("confirmed_by"),
        "confirmed_at": primary_audit.get("confirmed_at"),
        "confirmation_method": primary_audit.get("confirmation_method"),
        "confirmation_note": primary_audit.get("confirmation_note"),
        "package_file": primary_audit.get("package_file"),
        "package_sha256": primary_audit.get("package_sha256"),
        "source_file": primary_audit.get("source_file"),
        "source_sha256": primary_audit.get("source_sha256"),
        "formula_audits": formula_audits,
        "indicator_ids": [s.id for s in trade_specs],
        "indicator_source_sha": {s.id: s.source_sha256 for s in trade_specs},
        "indicator_package_sha": {s.id: s.package_sha256 for s in trade_specs},
        "package_sha_note": "package_sha is null for pure .txt indicators (no .tn6 package); only tn6_* entries carry package_sha256.",
        "period": period,
        "hold": hold,
        "combine": combine,
        "selected_codes": codes,
        "selected_codes_count": len(codes),
        "selected_universe_sha": sel_sha,
        "global_manifest_sha": global_manifest_sha,
        "global_universe_sha": global_universe_sha,
        "global_universe_count": global_universe_count,
        "start": start,
        "end": end,
        "config": cfg.to_dict(),
        "factor_manifest_sha": factor_manifest_sha(factor_series),
        "adjustment_status": adj_msg,
        "research_unadjusted": research_unadj or not formal_ok,
        "research_unconfirmed_formula": unconfirmed_run,
        "bagua_sha": bagua_sha,
        "with_bagua": bagua_enabled,
        "bagua_filter_mode": bagua_filter_mode,
        "bagua_filter_label": (
            (gua_filter_meta or {}).get("natural_language")
            if gua_filter_meta
            else (bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None)
        ),
        "bagua_allowlist": (
            best3_display_pairs()
            if bagua_filter_mode and not gua_filter_meta
            else None
        ),
        "gua_filter": gua_filter_meta or (gf.to_dict() if gf else None),
        "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
        "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
        "code_version": "astock-0.5.0",
        "astock_code_sha": _astock_code_sha(),
        "n_signals": len(events),
        "signal_cache_hit": signal_cache_hit,
        "filter_cache_hit": filter_cache_hit,
        "execution_cache_hit": execution_cache_hit,
        "use_signal_cache": use_signal_cache,
        "request": req.to_dict(),
    }
    try:
        _ind_src = None
        if trade_specs:
            _ind_src = "|".join(
                sorted(
                    "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                    for s in trade_specs
                )
            )
        _fp_fields = research_fingerprint_fields_from_request(
            req,
            universe_hash=sel_sha,
            indicator_source_hash=_ind_src,
            bagua_json_hash=bagua_sha if bagua_enabled else None,
        )
        repro.update(_fp_fields)
    except Exception:
        _fp_fields = {}
    _progress({
        "phase": "writing",
        "pct": 96.0,
        "current": n_codes,
        "total": n_codes,
        "message": f"写入结果（信号 {len(events)} / 成交 {len(getattr(result, 'fills', []) or [])}）…",
        "code": None,
        "run_id": run_id,
    })

    if art_flags.get("write_signals", True):
        write_signals_csv(out_dir / "signals.csv", events)
        try:
            from ..research.parquet_io import write_events_parquet
            write_events_parquet(out_dir / "signals.parquet", events)
        except Exception:
            pass
    _progress({
        "phase": "writing_excel",
        "pct": 97.0,
        "current": n_codes,
        "total": n_codes,
        "message": (
            "写入摘要…"
            if not art_flags.get("write_excel", True)
            else "写入 CSV / Excel 汇总（大明细可能仍需片刻）…"
        ),
        "code": None,
        "run_id": run_id,
        "n_fills": len(getattr(result, "fills", []) or []),
    })
    rule_names = [s.name for s in trade_specs]
    period_label = {
        "DAY": "日线",
        "WEEK": "周线",
        "MONTH": "月线",
        "DWM": "日周月共振",
        "MIN60": "60分钟",
    }.get(period, period)
    title = "、".join(rule_names) if rule_names else "回测"
    if gua_filter_meta:
        hs = (gua_filter_meta.get("history_summary") or {}).get("short") or "卦象过滤"
        # Fingerprint classic best-3 preset for human-readable history titles
        try:
            _sids = sorted(str(x) for x in (gua_filter_meta.get("selected_state_ids") or []))
            if _sids == ["11-1", "24-1", "46-1"]:
                hs = "卦象·最佳3爻"
            elif gua_filter_meta.get("selection_mode") == "action_signal":
                acts = list(gua_filter_meta.get("selected_action_signals") or [])
                if set(acts) == {"新开仓", "加仓"} or set(acts) == {"加仓", "新开仓"}:
                    hs = "卦象·偏多信号"
        except Exception:
            pass
        # Prefer compact but explicit: 卦象·最佳3爻 / 卦象信号：…
        title = f"{title} + {hs}"
    elif bagua_enabled and bagua_filter_mode:
        title = f"{title} + {bagua_mode_label(bagua_filter_mode)}"
    elif bagua_enabled:
        title = f"{title} + 八卦"
    am = (getattr(req, "account_mode", None) or "portfolio").strip().lower()
    if am in ("tdx", "per_stock", "independent", "通达信", "单票"):
        am = "per_symbol"
    if am == "per_symbol":
        title = f"{title} · 通达信对照(单票独立资金)"
    else:
        title = f"{title} · 组合账户"
    schedule_mode = (
        "weekday" if (buy_weekday is not None or exit_weekday is not None) else "tn"
    )
    title = f"{title} · {period_label}"
    if schedule_mode == "tn":
        title = f"{title} · 持有{hold}"
    if signal_weekdays:
        title = f"{title} · 仅{format_signal_weekdays(signal_weekdays)}信号"
    title = f"{title} · {session_label_cn(buy_on)}买/{session_label_cn(sell_on)}卖"
    if buy_weekday is not None:
        title = f"{title} · {format_single_weekday(buy_weekday)}买"
    if exit_weekday is not None:
        title = f"{title} · {format_single_weekday(exit_weekday)}平"
    if start or end:
        title += f" · {start or ''}~{end or ''}"

    repro["title"] = title
    repro["schedule_mode"] = schedule_mode
    repro["indicator_names"] = rule_names
    # CostConfig snapshot for run_meta / Excel (P1.7)
    try:
        from dataclasses import asdict as _asdict_costs
        repro["costs"] = _asdict_costs(cfg.costs)
        if isinstance(repro.get("config"), dict):
            repro["config"].setdefault("costs", repro["costs"])
    except Exception:
        pass
    if gua_filter_meta is not None:
        repro["gua_filter"] = gua_filter_meta
    elif "gua_filter" not in repro:
        repro["gua_filter"] = gf.to_dict() if gf else None

    repro["engine"] = engine if engine in ("fast", "quick", "research_fast") else "full"
    if repro["engine"] != "full":
        repro["engine"] = "fast"
    repro["artifact_level"] = artifact_level
    repro["holiday_policy"] = holiday_policy

    # Always write compact meta/metrics; heavy CSV/Excel respect artifact_level
    out_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    (out_dir / "run_meta.json").write_text(
        _json.dumps(repro, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "metrics.json").write_text(
        _json.dumps(result.metrics or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    paths: dict = {
        "run_meta": out_dir / "run_meta.json",
        "metrics": out_dir / "metrics.json",
    }
    if art_flags.get("write_fills", True) or art_flags.get("write_excel", True) or art_flags.get(
        "write_equity", True
    ):
        # full/candidate writers
        if artifact_level == "summary":
            pass
        else:
            paths.update(
                write_backtest_csv(
                    out_dir,
                    result,
                    meta=repro,
                    events=events if art_flags.get("write_signals") else None,
                )
            )
    else:
        # summary: skip heavy write_backtest_csv
        pass


    # index for history UI
    try:
        from .runs import append_run_index

        append_run_index(

            cfg,
            {
                "run_id": run_id,
                "title": title,
                "status": result.status,
                "created_at": int(time.time()),
                "indicator_ids": [s.id for s in trade_specs],
                "indicator_names": rule_names,
                "hold": hold,
                "entry_lag": entry_lag,
                "buy_weekday": buy_weekday,
                "exit_weekday": exit_weekday,
                "buy_on": buy_on,
                "sell_on": sell_on,
                "signal_weekdays": signal_weekdays,
                "schedule_mode": schedule_mode,
                "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
                "gua_filter": gua_filter_meta,
                "with_bagua": bagua_enabled,
                "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
                "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
                "period": period,
                "period_label": period_label,
                "start": start,
                "end": end,
                "selected_codes_count": len(codes),
                "metrics": result.metrics,
                **(_fp_fields if isinstance(_fp_fields, dict) else {}),
            },
        )
    except Exception:
        pass

    _progress({
        "phase": "done",
        "pct": 100.0,
        "current": n_codes,
        "total": n_codes,
        "message": "完成",
        "code": None,
        "run_id": run_id,
    })

    # P1.7: expose CostConfig on service summary for API/CLI traceability
    _costs = None
    if isinstance(getattr(result, "config", None), dict):
        _costs = result.config.get("costs")
    if _costs is None:
        try:
            from dataclasses import asdict as _asdict
            _costs = _asdict(cfg.costs)
        except Exception:
            _costs = None
    if isinstance(repro.get("config"), dict) and isinstance(repro["config"].get("costs"), dict):
        # prefer cfg snapshot already in repro
        _costs = repro["config"]["costs"]
    elif _costs is not None:
        repro.setdefault("costs", _costs)
        if isinstance(repro.get("config"), dict):
            repro["config"].setdefault("costs", _costs)

    summary = {
        "run_id": run_id,
        "title": title,
        "status": result.status,
        "metrics": result.metrics,
        "costs": _costs,
        "n_events": len(events),
        "n_events_raw_signals": int(n_events_raw_signals),
        "n_events_after_weekday": int(n_events_after_weekday),
        "with_bagua": bagua_enabled,
        "bagua_filter_mode": bagua_filter_mode,
        "bagua_filter_label": (
            (gua_filter_meta or {}).get("natural_language")
            if gua_filter_meta
            else (bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None)
        ),
        "gua_filter": gua_filter_meta or (gf.to_dict() if gf else None),
        "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
        "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
        "n_fills": len(getattr(result, "fills", None) or []),
        "n_buys": result.metrics.get("n_buys"),
        "n_sells": result.metrics.get("n_sells"),
        "n_round_trips": result.metrics.get("n_round_trips"),
        "paths": {k: str(v) for k, v in paths.items()},
        "errors_sample": errors[:20],
        "notes": result.notes,
        "entry_lag": entry_lag,
        "signal_weekdays": signal_weekdays,
        "buy_on": buy_on,
        "sell_on": sell_on,
        "buy_weekday": buy_weekday,
        "exit_weekday": exit_weekday,
        "schedule_mode": schedule_mode,
        "hold": hold,
        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
        "period": period,
        "engine": repro.get("engine") or "full",
        "artifact_level": artifact_level,
        "holiday_policy": holiday_policy,
        "signal_cache_hit": signal_cache_hit,
        "filter_cache_hit": filter_cache_hit,
        "execution_cache_hit": execution_cache_hit,
        "use_signal_cache": use_signal_cache,
        "repro": {
            "factor_manifest_sha": repro["factor_manifest_sha"],
            "entry_lag": entry_lag,
            "hold": hold,
            "period": period,
            "signal_weekdays": signal_weekdays,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "schedule_mode": schedule_mode,
            "indicator_ids": repro["indicator_ids"],
            "astock_code_sha": repro.get("astock_code_sha"),
            "research_fingerprint": repro.get("research_fingerprint"),
            "signal_fp": repro.get("signal_fp"),
            "filter_fp": repro.get("filter_fp"),
            "execution_fp": repro.get("execution_fp"),
        },
        "research_fingerprint": repro.get("research_fingerprint"),
        "signal_fp": repro.get("signal_fp"),
        "filter_fp": repro.get("filter_fp"),
        "execution_fp": repro.get("execution_fp"),
    }
    return summary
