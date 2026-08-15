"""Signal building, multi-period resonance, and condition studies."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .bagua.calculator import BaguaCalculator
from .data.periods import (
    PeriodBar,
    aggregate_month,
    aggregate_week,
    align_closed_state,
    period_bars_to_arrays,
)
from .data.tdx_reader import DayBar, bars_to_arrays
from .indicators.models import IndicatorSpec
from .indicators.runtime import run_formula


@dataclass
class SignalEvent:
    std_code: str
    date: int
    period: str
    indicator_id: str
    value: int = 1
    bagua: Optional[dict] = None
    is_dwm: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def bars_dict_from_day(bars: Sequence[DayBar]) -> Dict[str, np.ndarray]:
    return bars_to_arrays(bars)


def bars_dict_from_period(bars: Sequence[PeriodBar]) -> Dict[str, np.ndarray]:
    return period_bars_to_arrays(bars)


def _scale_day_bars(
    bars: Sequence[DayBar],
    scale: np.ndarray,
) -> List[DayBar]:
    """Apply per-bar scale to OHLC; leave volume/amount unchanged."""
    if not bars:
        return []
    out = []
    for i, b in enumerate(bars):
        s = float(scale[i]) if i < len(scale) else 1.0
        out.append(
            DayBar(
                date=b.date,
                open=round(b.open * s, 4),
                high=round(b.high * s, 4),
                low=round(b.low * s, 4),
                close=round(b.close * s, 4),
                amount=b.amount,
                volume=b.volume,
                reserved=b.reserved,
            )
        )
    return out


def _affine_day_bars(
    bars: Sequence[DayBar],
    cum_a: np.ndarray,
    cum_b: np.ndarray,
) -> List[DayBar]:
    """Apply per-bar affine transform: adj = a*raw + b; leave volume/amount unchanged."""
    if not bars:
        return []
    out = []
    for i, b in enumerate(bars):
        a = float(cum_a[i]) if i < len(cum_a) else 1.0
        bb = float(cum_b[i]) if i < len(cum_b) else 0.0
        out.append(
            DayBar(
                date=b.date,
                open=round(b.open * a + bb, 4),
                high=round(b.high * a + bb, 4),
                low=round(b.low * a + bb, 4),
                close=round(b.close * a + bb, 4),
                amount=b.amount,
                volume=b.volume,
                reserved=b.reserved,
            )
        )
    return out


def day_bars_to_standard_qfq(
    bars: Sequence[DayBar],
    factor: np.ndarray,
    *,
    snapshot_end_factor=None,
) -> List[DayBar]:
    """Standard ordinary qfq DayBars: scale_t = factor_t / factor_snapshot_end.

    For technical signals / chart display only — not execution.
    """
    if not bars:
        return []
    from .data.adjustments import standard_qfq_scale

    factor = np.asarray(factor, dtype=np.float64)
    scale = standard_qfq_scale(factor, snapshot_end_factor=snapshot_end_factor)
    return _scale_day_bars(bars, scale)


def day_bars_to_point_in_time_adjusted(
    bars: Sequence[DayBar],
    factor: np.ndarray,
    *,
    base_factor=None,
) -> List[DayBar]:
    """起点锚定复权研究价 DayBars: scale_t = factor_t / base_factor.

    Research / audit reference only — never cash or shares.
    """
    if not bars:
        return []
    from .data.adjustments import causal_qfq_scale

    factor = np.asarray(factor, dtype=np.float64)
    scale = causal_qfq_scale(factor, base_factor=base_factor)
    return _scale_day_bars(bars, scale)


def day_bars_to_adj(
    bars: Sequence[DayBar],
    factor: np.ndarray,
    *,
    base_factor=None,
) -> List[DayBar]:
    """Backward-compat alias for point_in_time_adjusted (legacy causal_qfq).

    Prefer ``day_bars_to_standard_qfq`` for signals and
    ``day_bars_to_point_in_time_adjusted`` for research references.
    """
    return day_bars_to_point_in_time_adjusted(
        bars, factor, base_factor=base_factor
    )


def day_bars_to_asof_forward_adjusted(
    bars: Sequence[DayBar],
    factor: np.ndarray,
    *,
    asof_factor=None,
    asof_index=None,
) -> List[DayBar]:
    """时点动态前复权 DayBars: scale_t = factor_t / factor_asof (L1 signal only)."""
    if not bars:
        return []
    from .data.adjustments import asof_forward_adjusted_scale

    factor = np.asarray(factor, dtype=np.float64)
    scale = asof_forward_adjusted_scale(
        factor, asof_factor=asof_factor, asof_index=asof_index
    )
    return _scale_day_bars(bars, scale)


def day_bars_to_affine_standard_qfq(
    bars: Sequence[DayBar],
    cum_a: np.ndarray,
    cum_b: np.ndarray,
) -> List[DayBar]:
    """Standard qfq via affine model: adj = a*raw + b (anchor = snapshot end)."""
    return _affine_day_bars(bars, cum_a, cum_b)


def day_bars_to_affine_asof(
    bars: Sequence[DayBar],
    events: Sequence,
    dates: Sequence[int],
    asof_date: int,
) -> List[DayBar]:
    """Asof forward qfq via affine model: only events <= asof_date."""
    from .data.affine_adjust import compute_affine_params_asof

    cum_a, cum_b = compute_affine_params_asof(events, dates, asof_date)
    return _affine_day_bars(bars, cum_a, cum_b)


def day_bars_for_signals(
    bars: Sequence[DayBar],
    factor: np.ndarray,
    *,
    research_unadjusted: bool = False,
    snapshot_end_factor=None,
    signal_adjust: str = "asof_forward_qfq",
    asof_date: Optional[int] = None,
    dates: Optional[Sequence[int]] = None,
) -> List[DayBar]:
    """Bars used for technical signal generation (L1).

    research_unadjusted=True → raw OHLC.
    Formal default signal_adjust=asof_forward_qfq: scale = factor_t / factor_asof.
    Batch backtest should pass asof_date=run_end (anchor run_end; equals
    standard_qfq on a snapshot that ends at run_end). Bagua query may pass
    the query date for true asof.
    signal_adjust=standard_qfq keeps ordinary snapshot-end qfq.
    """
    if research_unadjusted:
        return list(bars)
    mode = (signal_adjust or "asof_forward_qfq").strip().lower()
    if mode in ("standard_qfq", "ordinary_qfq", "qfq"):
        return day_bars_to_standard_qfq(
            bars, factor, snapshot_end_factor=snapshot_end_factor
        )
    # asof_forward_qfq (default)
    asof_factor = snapshot_end_factor
    if asof_factor is None and asof_date is not None:
        from .data.adjustments import factor_value_on_or_before

        if dates is not None:
            ds = [int(x) for x in dates]
        elif bars:
            ds = [int(b.date) for b in bars]
        else:
            ds = []
        fac_list = list(np.asarray(factor, dtype=float))
        if ds and len(ds) == len(fac_list):
            asof_factor = factor_value_on_or_before(ds, fac_list, int(asof_date))
        elif bars:
            ds = [int(b.date) for b in bars]
            fac_list = list(np.asarray(factor, dtype=float))
            asof_factor = factor_value_on_or_before(ds, fac_list, int(asof_date))
    return day_bars_to_asof_forward_adjusted(
        bars, factor, asof_factor=asof_factor, asof_index=None
    )


def day_bars_for_signals_affine(
    bars: Sequence[DayBar],
    affine_series,
    *,
    research_unadjusted: bool = False,
    signal_adjust: str = "asof_forward_qfq",
    asof_date: Optional[int] = None,
) -> List[DayBar]:
    """Bars for L1 signals using affine model (1:1 TDX match).

    affine_series: AffineSeries from build_affine_series().
    """
    if research_unadjusted:
        return list(bars)
    if not bars:
        return []

    from .data.affine_adjust import DividendEvent, compute_affine_params, compute_affine_params_asof

    dates = [int(b.date) for b in bars]
    events = [DividendEvent(**ev) for ev in (affine_series.events or [])]

    mode = (signal_adjust or "asof_forward_qfq").strip().lower()

    if mode in ("standard_qfq", "ordinary_qfq", "qfq"):
        cum_a, cum_b = compute_affine_params(events, dates)
    else:
        if asof_date is None:
            asof_date = dates[-1] if dates else 0
        cum_a, cum_b = compute_affine_params_asof(events, dates, int(asof_date))

    return _affine_day_bars(bars, cum_a, cum_b)



def compute_indicator_signal(
    spec: IndicatorSpec,
    bars: Dict[str, np.ndarray],
    *,
    cross_period_data: Optional[Dict[str, np.ndarray]] = None,
    minute_mode: bool = False,
) -> Tuple[Optional[np.ndarray], Optional[str]]:
    if spec.compile_status != "ready":
        return None, f"indicator not ready: {spec.compile_status} ({spec.failure_reason})"
    if not spec.formula_text:
        return None, "missing formula_text"
    cross = dict(cross_period_data or {})
    # Research path: fill #MIN60 MACD refs with day-line DIF/DEA proxy.
    if "MIN60" in (spec.dependencies or []):
        if minute_mode or (spec.parameters or {}).get("min60_native"):
            # True 60-minute data: #MIN60 refs resolve from the minute series
            # itself (MACD over the intraday close), not the day-line proxy.
            from .indicators.min60_proxy import day_macd_dif_dea
            closes = bars.get("close")
            if closes is None:
                return None, "minute_mode MIN60 requires close series"
            dif, dea = day_macd_dif_dea(np.asarray(closes, dtype=np.float64))
            cross["MACD.DIF#MIN60"] = dif
            cross["MACD.DEA#MIN60"] = dea
            cross["DIF#MIN60"] = dif
            cross["DEA#MIN60"] = dea
        elif not getattr(spec, "uses_min60_day_proxy", False) and not (
            (spec.parameters or {}).get("min60_day_proxy")
        ):
            return None, "MIN60 dependency blocks signal generation without day proxy"
        else:
            from .indicators.min60_proxy import build_min60_day_proxy_cross, merge_cross_period
            try:
                proxy = build_min60_day_proxy_cross(bars)
            except Exception as e:  # noqa: BLE001
                return None, f"MIN60 day proxy failed: {e}"
            cross = merge_cross_period(cross, proxy)
    result = run_formula(
        spec.formula_text,
        bars,
        indicator_id=spec.id,
        cross_period_data=cross if cross else None,
        allow_missing_cross=False,
    )
    if result.error:
        return None, result.error
    if result.signal is None:
        return None, "no XG output in formula"
    return result.signal.astype(np.int8), None


def signal_dates(dates: np.ndarray, signal: np.ndarray) -> List[int]:
    out = []
    for d, s in zip(dates, signal):
        if s and not (isinstance(s, float) and np.isnan(s)):
            if int(s) != 0:
                out.append(int(d))
    return out


def combine_signals(signals: List[np.ndarray], mode: str = "all") -> np.ndarray:
    if not signals:
        raise ValueError("no signals")
    arrs = [np.asarray(s).astype(bool) for s in signals]
    out = arrs[0].copy()
    for a in arrs[1:]:
        if mode == "all":
            out &= a
        elif mode == "any":
            out |= a
        else:
            raise ValueError(f"unknown combine mode: {mode}")
    return out.astype(np.int8)


def build_period_bars(
    day_bars: Sequence[DayBar],
    period: str,
    *,
    asof: Optional[int] = None,
    include_open: bool = False,
    weekly_bar_mode: str = "local_aggregate",
    vendor_weekly_bars: Optional[Sequence] = None,
) -> List:
    period = period.upper()
    if period in ("DAY", "D", "1D"):
        return list(day_bars)
    if period in ("WEEK", "W", "1W"):
        if weekly_bar_mode == "vendor_native":
            if vendor_weekly_bars is not None:
                return list(vendor_weekly_bars)
            raise ValueError(
                "weekly_bar_mode=vendor_native but no vendor_weekly_bars provided. "
                "The selected source/dataset does not support native weekly bars. "
                "Use weekly_bar_mode=local_aggregate instead."
            )
        return aggregate_week(day_bars, asof=asof, include_open=include_open)
    if period in ("MONTH", "M", "1M"):
        return aggregate_month(day_bars, asof=asof, include_open=include_open)
    if period in ("MIN60", "M60", "60", "60M", "H1"):
        raise ValueError(
            "MIN60 bars are loaded from TDX minute files; "
            "do not aggregate day bars. Use minline_reader.load_min60_daybars."
        )
    raise ValueError(f"unsupported period {period}")


def compute_v5_dwm_resonance(
    day_bars: Sequence[DayBar],
    day_signal: np.ndarray,
    week_bars: Sequence[PeriodBar],
    week_signal: np.ndarray,
    month_bars: Sequence[PeriodBar],
    month_signal: np.ndarray,
) -> np.ndarray:
    day_dates = [b.date for b in day_bars]
    w_idx = align_closed_state(day_dates, week_bars)
    m_idx = align_closed_state(day_dates, month_bars)
    out = np.zeros(len(day_bars), dtype=np.int8)
    for i, d in enumerate(day_dates):
        if not day_signal[i]:
            continue
        wi = w_idx[i]
        mi = m_idx[i]
        if wi is None or mi is None:
            continue
        if week_signal[wi] and month_signal[mi]:
            if week_bars[wi].end_date <= d and month_bars[mi].end_date <= d:
                out[i] = 1
    return out


def period_bar_map(bars: Sequence) -> Dict[int, object]:
    """Map period end date -> bar (DayBar or PeriodBar)."""
    return {int(b.date): b for b in bars}


def _looks_like_week_or_month_bars(bars: Sequence) -> bool:
    if not bars:
        return False
    b0 = bars[0]
    return hasattr(b0, "start_date") and hasattr(b0, "end_date")


def find_bar_covering_date(bars: Sequence, asof: int):
    """Pick period/day bar for asof: covering [start,end], else exact date, else last end<=asof."""
    if not bars:
        return None
    asof = int(asof)
    covering = []
    exact = None
    before = None
    for b in bars:
        d = int(getattr(b, "date", 0) or 0)
        start_d = int(getattr(b, "start_date", d) or d)
        end_d = int(getattr(b, "end_date", d) or d)
        if start_d <= asof <= end_d:
            covering.append(b)
        if d == asof or end_d == asof:
            exact = b
        if end_d <= asof or d <= asof:
            if before is None:
                before = b
            else:
                prev_end = int(getattr(before, "end_date", before.date) or before.date)
                if end_d >= prev_end:
                    before = b
    if covering:
        return covering[-1]
    if exact is not None:
        return exact
    return before


def prepare_bars_for_bagua(
    bars_by_code: Dict[str, Sequence],
    *,
    bagua_period: str = "WEEK",
) -> Dict[str, list]:
    """Build per-code bars used for bagua OHLC (default: weekly L1 bars).

    If input bars are daily and bagua_period=WEEK, aggregate with include_open=True
    so a Friday (or mid-week) signal maps to that stock's week bar.
    """
    bp = (bagua_period or "WEEK").strip().upper()
    if bp in ("D", "1D"):
        bp = "DAY"
    if bp in ("W", "1W"):
        bp = "WEEK"
    if bp in ("M", "1M"):
        bp = "MONTH"
    out: Dict[str, list] = {}
    for code, bars in (bars_by_code or {}).items():
        seq = list(bars or [])
        if not seq:
            continue
        if bp == "DAY":
            out[code] = seq
            continue
        # Already aggregated week/month bars: keep when requesting WEEK/MONTH
        if bp in ("WEEK", "MONTH") and _looks_like_week_or_month_bars(seq):
            out[code] = seq
            continue
        if bp in ("WEEK", "MONTH"):
            try:
                out[code] = build_period_bars(seq, bp, asof=None, include_open=True)
            except Exception:
                out[code] = seq
        else:
            out[code] = seq
    return out


def attach_bagua(
    events: List[SignalEvent],
    bars_by_code_period: Dict[str, Sequence],
    calculator: BaguaCalculator,
    *,
    bagua_period: str = "WEEK",
    price_plane: str = "raw",
) -> List[SignalEvent]:
    """Attach bagua from OHLC digit-sum (default: weekly unadjusted bars).

    Product default:
    - bagua_period=WEEK: signal day (e.g. Friday) maps to that stock's week bar
      covering the signal date (week open/high/low/close = full week picture).
    - price_plane=raw (L2): unadjusted market OHLC for digit-sum / 卦 / 变卦.
      Formal bagua must NOT use L1 asof_forward_qfq or standard_qfq prices —
      adjusted OHLC changes digit sums and hexagrams.

    Pass raw day bars (or pre-aggregated raw week bars). WEEK aggregation uses
    include_open=True.
    """
    bp = (bagua_period or "WEEK").strip().upper()
    if bp in ("W", "1W"):
        bp = "WEEK"
    if bp in ("D", "1D"):
        bp = "DAY"
    prepared = prepare_bars_for_bagua(bars_by_code_period, bagua_period=bp)
    for ev in events:
        bars = prepared.get(ev.std_code) or []
        bar = find_bar_covering_date(bars, int(ev.date))
        if not bar:
            continue
        res = calculator.calculate(
            open_price=bar.open,
            high_price=bar.high,
            low_price=bar.low,
            close_price=bar.close,
        )
        d = res.to_dict()
        d["bagua_period"] = bp
        d["bagua_price_plane"] = (price_plane or "raw").strip().lower()
        d["bagua_bar_date"] = int(getattr(bar, "date", ev.date) or ev.date)
        if hasattr(bar, "start_date"):
            d["bagua_bar_start"] = int(bar.start_date)
        if hasattr(bar, "end_date"):
            d["bagua_bar_end"] = int(bar.end_date)
        if not d.get("biangua") and d.get("changed_hexagram_name"):
            d["biangua"] = d.get("changed_hexagram_name")
        ev.bagua = d
    return events


@dataclass
class ForwardStats:
    key: str
    n: int
    win_rate: float
    mean_return: float
    median_return: float
    mfe: float
    mae: float
    period: str = ""
    indicator_id: str = ""
    gua: str = ""
    yao: str = ""
    is_dwm: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _next_open_index(dates: Sequence[int], i: int) -> Optional[int]:
    if i + 1 < len(dates):
        return i + 1
    return None


def event_path_stats(
    bars: Sequence[DayBar],
    signal_indices: Sequence[int],
    *,
    hold_periods: int,
    period: str = "DAY",
    period_bars: Optional[Sequence[PeriodBar]] = None,
) -> List[dict]:
    """Per-event forward path using next open entry and period-based exit.

    Returns list of {entry_date, exit_date, ret, mfe, mae}.
    """
    if not bars:
        return []
    dates = [b.date for b in bars]
    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    out = []
    period = period.upper()

    # map for week/month: signal on period bar index
    for si in signal_indices:
        if period in ("DAY", "DWM"):
            entry_i = _next_open_index(dates, si)
            if entry_i is None:
                continue
            # exit after hold_periods trading days from entry: sell open at entry_i+hold
            exit_i = entry_i + int(hold_periods)
            if exit_i >= len(dates):
                continue
            entry_px = opens[entry_i]
            exit_px = opens[exit_i]
            if entry_px <= 0:
                continue
            path_h = highs[entry_i:exit_i]  # until day before exit open
            path_l = lows[entry_i:exit_i]
            if not path_h:
                path_h = [highs[entry_i]]
                path_l = [lows[entry_i]]
            mfe = max(path_h) / entry_px - 1.0
            mae = min(path_l) / entry_px - 1.0
            ret = exit_px / entry_px - 1.0
            out.append(
                {
                    "signal_date": dates[si],
                    "entry_date": dates[entry_i],
                    "exit_date": dates[exit_i],
                    "ret": ret,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
        else:
            # period_bars aligned; si indexes period bar; entry next day open after period end
            if period_bars is None:
                continue
            pb = period_bars[si]
            # find first day bar after pb.end_date
            entry_i = None
            for j, d in enumerate(dates):
                if d > pb.end_date:
                    entry_i = j
                    break
            if entry_i is None:
                continue
            # exit after hold_periods completed periods: use period bar si+hold end, next open
            exit_pb_i = si + int(hold_periods)
            if exit_pb_i >= len(period_bars):
                continue
            exit_end = period_bars[exit_pb_i].end_date
            exit_i = None
            for j, d in enumerate(dates):
                if d > exit_end:
                    exit_i = j
                    break
            if exit_i is None or exit_i <= entry_i:
                continue
            entry_px = opens[entry_i]
            exit_px = opens[exit_i]
            if entry_px <= 0:
                continue
            path_h = highs[entry_i:exit_i]
            path_l = lows[entry_i:exit_i]
            mfe = max(path_h) / entry_px - 1.0
            mae = min(path_l) / entry_px - 1.0
            ret = exit_px / entry_px - 1.0
            out.append(
                {
                    "signal_date": pb.end_date,
                    "entry_date": dates[entry_i],
                    "exit_date": dates[exit_i],
                    "ret": ret,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
    return out


def summarize_event_stats(
    key: str,
    events: List[dict],
    **meta,
) -> ForwardStats:
    if not events:
        return ForwardStats(key=key, n=0, win_rate=0.0, mean_return=0.0, median_return=0.0, mfe=0.0, mae=0.0, **meta)
    rets = np.array([e["ret"] for e in events], dtype=np.float64)
    mfes = np.array([e["mfe"] for e in events], dtype=np.float64)
    maes = np.array([e["mae"] for e in events], dtype=np.float64)
    return ForwardStats(
        key=key,
        n=int(len(events)),
        win_rate=float(np.mean(rets > 0)),
        mean_return=float(np.mean(rets)),
        median_return=float(np.median(rets)),
        mfe=float(np.mean(mfes)),
        mae=float(np.mean(maes)),
        **meta,
    )


def bagua_condition_study(
    day_bars: Sequence[DayBar],
    calculator: BaguaCalculator,
    *,
    period: str = "DAY",
    horizons: Sequence[int] = (1, 3, 5),
    asof: Optional[int] = None,
) -> List[ForwardStats]:
    """Classify each closed period bar; group by gua/yao; path stats.

    WEEK/MONTH use aggregated OHLC for classification (not last day only).
    """
    period = period.upper()
    if period == "DAY":
        p_bars = list(day_bars)
        labels = []
        for b in p_bars:
            r = calculator.calculate(
                open_price=b.open, high_price=b.high, low_price=b.low, close_price=b.close
            )
            labels.append((f"{r.full_name}", r.yao_name, r.gua_order, r.yao_order))
        stats: List[ForwardStats] = []
        groups: Dict[str, List[int]] = defaultdict(list)
        for i, lab in enumerate(labels):
            groups[f"{lab[0]}|{lab[1]}"].append(i)
        for lab, idxs in groups.items():
            for h in horizons:
                evs = event_path_stats(day_bars, idxs, hold_periods=h, period="DAY")
                gua, yao = lab.split("|", 1)
                stats.append(
                    summarize_event_stats(
                        f"{lab}|h{h}",
                        evs,
                        period=period,
                        gua=gua,
                        yao=yao,
                    )
                )
        return stats

    # week/month aggregated
    p_bars = build_period_bars(day_bars, period, asof=asof, include_open=False)
    labels = []
    for b in p_bars:
        r = calculator.calculate(
            open_price=b.open, high_price=b.high, low_price=b.low, close_price=b.close
        )
        labels.append((f"{r.full_name}", r.yao_name))
    stats = []
    groups = defaultdict(list)
    for i, lab in enumerate(labels):
        groups[f"{lab[0]}|{lab[1]}"].append(i)
    for lab, idxs in groups.items():
        for h in horizons:
            evs = event_path_stats(
                day_bars, idxs, hold_periods=h, period=period, period_bars=p_bars
            )
            gua, yao = lab.split("|", 1)
            stats.append(
                summarize_event_stats(
                    f"{lab}|h{h}",
                    evs,
                    period=period,
                    gua=gua,
                    yao=yao,
                )
            )
    return stats


def study_indicator_events(
    day_bars: Sequence[DayBar],
    signal: np.ndarray,
    *,
    period: str,
    indicator_id: str,
    horizons: Sequence[int] = (1, 3, 5),
    period_bars: Optional[Sequence] = None,
    bagua_labels: Optional[List[Optional[dict]]] = None,
    is_dwm: bool = False,
) -> List[ForwardStats]:
    period = period.upper()
    idxs = [i for i, s in enumerate(signal) if s]
    stats = []
    for h in horizons:
        if period in ("DAY", "DWM"):
            evs = event_path_stats(day_bars, idxs, hold_periods=h, period="DAY")
        else:
            evs = event_path_stats(
                day_bars, idxs, hold_periods=h, period=period, period_bars=period_bars
            )
        stats.append(
            summarize_event_stats(
                f"{indicator_id}|{period}|h{h}|dwm={is_dwm}",
                evs,
                period=period,
                indicator_id=indicator_id,
                is_dwm=is_dwm,
            )
        )
        # bagua breakdown
        if bagua_labels:
            by_g: Dict[str, List[int]] = defaultdict(list)
            for i in idxs:
                bg = bagua_labels[i] if i < len(bagua_labels) else None
                if not bg:
                    continue
                by_g[f"{bg.get('full_name')}|{bg.get('yao_name')}"].append(i)
            for gkey, gidx in by_g.items():
                if period in ("DAY", "DWM"):
                    gevs = event_path_stats(day_bars, gidx, hold_periods=h, period="DAY")
                else:
                    gevs = event_path_stats(
                        day_bars, gidx, hold_periods=h, period=period, period_bars=period_bars
                    )
                gua, yao = gkey.split("|", 1)
                stats.append(
                    summarize_event_stats(
                        f"{indicator_id}|{period}|h{h}|{gkey}|dwm={is_dwm}",
                        gevs,
                        period=period,
                        indicator_id=indicator_id,
                        gua=gua,
                        yao=yao,
                        is_dwm=is_dwm,
                    )
                )
    return stats
