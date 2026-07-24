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


def compute_indicator_signal(
    spec: IndicatorSpec,
    bars: Dict[str, np.ndarray],
    *,
    cross_period_data: Optional[Dict[str, np.ndarray]] = None,
) -> Tuple[Optional[np.ndarray], Optional[str]]:
    if spec.compile_status != "ready":
        return None, f"indicator not ready: {spec.compile_status} ({spec.failure_reason})"
    if not spec.formula_text:
        return None, "missing formula_text"
    cross = dict(cross_period_data or {})
    # Research path: fill #MIN60 MACD refs with day-line DIF/DEA proxy.
    if "MIN60" in (spec.dependencies or []):
        if not getattr(spec, "uses_min60_day_proxy", False) and not (
            (spec.parameters or {}).get("min60_day_proxy")
        ):
            return None, "MIN60 dependency blocks signal generation without day proxy"
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
) -> List:
    period = period.upper()
    if period in ("DAY", "D", "1D"):
        return list(day_bars)
    if period in ("WEEK", "W", "1W"):
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


def attach_bagua(
    events: List[SignalEvent],
    bars_by_code_period: Dict[str, Sequence],
    calculator: BaguaCalculator,
) -> List[SignalEvent]:
    """Attach bagua using the period K-line OHLC for the event date.

    bars_by_code_period must contain the same period bars used for the signal
    (day bars for DAY/DWM, aggregated week/month for WEEK/MONTH).
    """
    by_code_date: Dict[str, Dict[int, object]] = {}
    for code, bars in bars_by_code_period.items():
        by_code_date[code] = {int(b.date): b for b in bars}
    for ev in events:
        bar = by_code_date.get(ev.std_code, {}).get(ev.date)
        if not bar:
            continue
        res = calculator.calculate(
            open_price=bar.open,
            high_price=bar.high,
            low_price=bar.low,
            close_price=bar.close,
        )
        ev.bagua = res.to_dict()
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
