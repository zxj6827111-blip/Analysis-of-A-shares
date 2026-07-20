"""Multi-period aggregation: day / week / month with closed-bar rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .tdx_reader import DayBar


@dataclass
class PeriodBar:
    date: int  # period end trading date YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    amount: float
    volume: float
    start_date: int
    end_date: int
    n_days: int
    closed: bool = True

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "amount": self.amount,
            "volume": self.volume,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "n_days": self.n_days,
            "closed": self.closed,
        }


def _ymd(d: int) -> date:
    y = d // 10000
    m = (d // 100) % 100
    day = d % 100
    return date(y, m, day)


def _week_key(d: int) -> Tuple[int, int]:
    """ISO year-week for grouping."""
    dt = _ymd(d)
    iso = dt.isocalendar()
    return int(iso[0]), int(iso[1])


def _month_key(d: int) -> Tuple[int, int]:
    return d // 10000, (d // 100) % 100


def _aggregate_group(bars: Sequence[DayBar], *, closed: bool) -> PeriodBar:
    return PeriodBar(
        date=bars[-1].date,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        amount=sum(b.amount for b in bars),
        volume=sum(b.volume for b in bars),
        start_date=bars[0].date,
        end_date=bars[-1].date,
        n_days=len(bars),
        closed=closed,
    )


def aggregate_week(
    bars: Sequence[DayBar],
    *,
    asof: Optional[int] = None,
    include_open: bool = False,
) -> List[PeriodBar]:
    """Aggregate daily bars into weekly bars.

    A week is closed after its last trading day is observed.
    If asof is provided, weeks whose end_date > asof are open.
    By default open (unclosed) weeks are excluded.
    """
    if not bars:
        return []
    groups: Dict[Tuple[int, int], List[DayBar]] = {}
    order: List[Tuple[int, int]] = []
    for b in bars:
        if asof is not None and b.date > asof:
            break
        key = _week_key(b.date)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)

    result: List[PeriodBar] = []
    for i, key in enumerate(order):
        g = groups[key]
        # closed if this is not the last group OR last bar date is end of week
        # relative to available data: last group may still be open if asof is mid-week
        is_last = i == len(order) - 1
        closed = True
        if is_last and asof is not None:
            # open if asof is not past Friday of that week... use: if more trading
            # days could still arrive in same week key from calendar perspective.
            # Conservative: last group is closed only if asof is the group's last date
            # and we're not including partial — actually for historical full data,
            # treat last group closed only if its last day week's Friday has passed.
            last_dt = _ymd(g[-1].date)
            # week ends on Sunday in ISO; market week ends Friday.
            # If asof == last bar date and there is no future bar in input, still
            # mark last week open unless include_open False means drop it when
            # asof is within the week and not at known week end.
            # Rule per plan: exclude unclosed current week by default.
            # If the dataset's last date equals asof and asof is last available,
            # we treat the final week as open only when caller sets asof to "today"
            # and week is not finished. For historical import without asof, all closed.
            closed = g[-1].date < asof or _is_week_closed(g[-1].date, asof)
        elif is_last and asof is None:
            closed = True  # full history import
        pb = _aggregate_group(g, closed=closed)
        if closed or include_open:
            result.append(pb)
    return result


def _is_week_closed(last_bar_date: int, asof: int) -> bool:
    """Week considered closed if asof is on/after the week's last trading day
    and the ISO week of asof differs from last_bar or asof is Friday+.
    """
    if asof > last_bar_date and _week_key(asof) != _week_key(last_bar_date):
        return True
    if asof == last_bar_date:
        # Friday=4
        return _ymd(last_bar_date).weekday() >= 4
    return asof > last_bar_date


def aggregate_month(
    bars: Sequence[DayBar],
    *,
    asof: Optional[int] = None,
    include_open: bool = False,
) -> List[PeriodBar]:
    if not bars:
        return []
    groups: Dict[Tuple[int, int], List[DayBar]] = {}
    order: List[Tuple[int, int]] = []
    for b in bars:
        if asof is not None and b.date > asof:
            break
        key = _month_key(b.date)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)

    result: List[PeriodBar] = []
    for i, key in enumerate(order):
        g = groups[key]
        is_last = i == len(order) - 1
        closed = True
        if is_last and asof is not None:
            y, m = key
            # closed if asof is in a later month
            asof_key = _month_key(asof)
            closed = asof_key > key or (
                asof_key == key and asof == g[-1].date and _is_month_end(g[-1].date)
            )
            if asof_key > key:
                closed = True
            elif asof_key == key:
                # still in month -> open unless we know it's last trading day of month
                closed = False
        pb = _aggregate_group(g, closed=closed)
        if closed or include_open:
            result.append(pb)
    return result


def _is_month_end(d: int) -> bool:
    dt = _ymd(d)
    nxt = dt + timedelta(days=1)
    return nxt.month != dt.month


def period_bars_to_arrays(bars: Sequence[PeriodBar]) -> Dict[str, np.ndarray]:
    if not bars:
        return {
            "date": np.array([], dtype=np.int64),
            "open": np.array([], dtype=np.float64),
            "high": np.array([], dtype=np.float64),
            "low": np.array([], dtype=np.float64),
            "close": np.array([], dtype=np.float64),
            "amount": np.array([], dtype=np.float64),
            "volume": np.array([], dtype=np.float64),
        }
    return {
        "date": np.array([b.date for b in bars], dtype=np.int64),
        "open": np.array([b.open for b in bars], dtype=np.float64),
        "high": np.array([b.high for b in bars], dtype=np.float64),
        "low": np.array([b.low for b in bars], dtype=np.float64),
        "close": np.array([b.close for b in bars], dtype=np.float64),
        "amount": np.array([b.amount for b in bars], dtype=np.float64),
        "volume": np.array([b.volume for b in bars], dtype=np.float64),
    }


def align_closed_state(
    day_dates: Sequence[int],
    higher_bars: Sequence[PeriodBar],
) -> List[Optional[int]]:
    """For each day date, return index of latest closed higher-period bar with end_date <= day.

    Guarantees no future leakage: only bars fully closed on or before the day.
    """
    out: List[Optional[int]] = []
    j = -1
    n = len(higher_bars)
    for d in day_dates:
        while j + 1 < n and higher_bars[j + 1].closed and higher_bars[j + 1].end_date <= d:
            j += 1
        out.append(j if j >= 0 else None)
    return out
