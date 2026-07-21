# -*- coding: utf-8 -*-
"""Fast research engine: per-signal path stats without portfolio cash competition.

Used for large grid screening. Final candidates should re-run full PortfolioBacktester.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..data.calendar import DEFAULT_HOLIDAY_POLICY, TradeCalendar, normalize_holiday_policy
from ..data.tdx_reader import DayBar
from ..strategy import (
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    bar_session_price,
    parse_price_session,
    parse_single_weekday,
    yyyymmdd_isoweekday,
)
from ..study import SignalEvent


@dataclass
class FastTrade:
    std_code: str
    signal_date: int
    entry_date: int
    exit_date: int
    entry_price: float
    exit_price: float
    ret: float
    mfe: float
    mae: float
    exit_reason: str
    planned_entry_date: Optional[int] = None
    planned_exit_date: Optional[int] = None
    entry_shift_days: int = 0
    exit_shift_days: int = 0


@dataclass
class FastBacktestResult:
    engine: str = "fast"
    n_signals: int = 0
    n_trades: int = 0
    trades: List[FastTrade] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "n_signals": self.n_signals,
            "n_trades": self.n_trades,
            "trades": [asdict(t) for t in self.trades],
            "metrics": self.metrics,
            "notes": self.notes,
            "config": self.config,
        }


def _bar_index(bars: Sequence[DayBar]) -> Dict[int, DayBar]:
    return {int(b.date): b for b in bars}


def _summarize(trades: Sequence[FastTrade]) -> Dict[str, Any]:
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": None,
            "mean_return": None,
            "median_return": None,
            "total_return_sum": 0.0,
            "profit_factor": None,
            "mean_mfe": None,
            "mean_mae": None,
        }
    rets = np.array([t.ret for t in trades], dtype=float)
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 1e-12 else None
    return {
        "n_trades": int(len(trades)),
        "win_rate": float(np.mean(rets > 0)),
        "mean_return": float(np.mean(rets)),
        "median_return": float(np.median(rets)),
        "total_return_sum": float(np.sum(rets)),
        "profit_factor": pf,
        "mean_mfe": float(np.mean([t.mfe for t in trades])),
        "mean_mae": float(np.mean([t.mae for t in trades])),
        # Approximate portfolio-style fields for ranking tables
        "total_return": float(np.mean(rets)),  # equal-weight mean trade return
        "max_drawdown": None,  # not defined without equity curve
        "n_round_trips": int(len(trades)),
    }


def _resolve_entry(
    calendar: TradeCalendar,
    signal_date: int,
    *,
    entry_lag: int,
    buy_weekday: Optional[int],
    holiday_policy: str,
) -> Optional[Tuple[int, int, int]]:
    """Return (planned, actual, shift) or None."""
    if buy_weekday is not None:
        got = calendar.resolve_weekday_session(
            signal_date, buy_weekday, strict=True, holiday_policy=holiday_policy
        )
        return got
    entry = calendar.nth_trading_day_after(signal_date, entry_lag)
    if entry is None:
        return None
    return entry, entry, 0


def _resolve_exit(
    calendar: TradeCalendar,
    entry_date: int,
    *,
    hold: int,
    exit_weekday: Optional[int],
    holiday_policy: str,
) -> Optional[Tuple[int, int, int, str]]:
    """Return (planned, actual, shift, reason) or None."""
    if exit_weekday is not None:
        got = calendar.resolve_weekday_session(
            entry_date, exit_weekday, strict=True, holiday_policy=holiday_policy
        )
        if got is None:
            return None
        planned, actual, shift = got
        if actual <= entry_date:
            nxt = calendar.next_trading_day(entry_date)
            if nxt is None:
                return None
            actual = nxt
        return planned, actual, shift, EXIT_REASON_WEEKDAY_EXIT
    # hold N: exit on N-th trading day after entry (hold=1 → next session)
    exit_d = calendar.nth_trading_day_after(entry_date, max(1, int(hold)))
    if exit_d is None:
        return None
    return exit_d, exit_d, 0, EXIT_REASON_TIME_EXIT


def _path_mfe_mae(
    bars_by_date: Dict[int, DayBar],
    calendar: TradeCalendar,
    entry_date: int,
    exit_date: int,
    entry_px: float,
) -> Tuple[float, float]:
    if entry_px <= 0:
        return 0.0, 0.0
    highs: List[float] = []
    lows: List[float] = []
    for d in calendar.range(entry_date, exit_date):
        if d >= exit_date:
            break
        b = bars_by_date.get(d)
        if b:
            highs.append(float(b.high))
            lows.append(float(b.low))
    if not highs:
        b = bars_by_date.get(entry_date)
        if b:
            highs = [float(b.high)]
            lows = [float(b.low)]
        else:
            return 0.0, 0.0
    mfe = max(highs) / entry_px - 1.0
    mae = min(lows) / entry_px - 1.0
    return float(mfe), float(mae)


def run_fast_backtest(
    events: Sequence[SignalEvent],
    bars_by_code: Dict[str, Sequence[DayBar]],
    calendar: TradeCalendar,
    *,
    hold: int = 1,
    entry_lag: int = 1,
    buy_on: str = "open",
    sell_on: str = "open",
    buy_weekday: Optional[int] = None,
    exit_weekday: Optional[int] = None,
    holiday_policy: str = DEFAULT_HOLIDAY_POLICY,
    signal_weekdays: Optional[Sequence[int]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> FastBacktestResult:
    """Equal-weight per-signal trades; no cash / max_weight / concurrent position limits."""
    buy_on = parse_price_session(buy_on, default="open")
    sell_on = parse_price_session(sell_on, default="open")
    buy_weekday = parse_single_weekday(buy_weekday)
    exit_weekday = parse_single_weekday(exit_weekday)
    holiday_policy = normalize_holiday_policy(holiday_policy)
    hold = int(hold)
    entry_lag = int(entry_lag)
    if entry_lag < 1:
        raise ValueError("entry_lag must be >= 1")

    allow = None
    if signal_weekdays:
        allow = set(int(x) for x in signal_weekdays)

    index: Dict[str, Dict[int, DayBar]] = {
        code: _bar_index(bars) for code, bars in bars_by_code.items()
    }
    trades: List[FastTrade] = []
    n_sig = 0
    notes = [
        "engine=fast: per-signal path stats; no portfolio cash competition.",
        "holiday_policy=%s" % holiday_policy,
    ]

    for ev in events:
        d = int(ev.date)
        if start and d < start:
            continue
        if end and d > end:
            continue
        if allow is not None:
            try:
                if yyyymmdd_isoweekday(d) not in allow:
                    continue
            except Exception:
                continue
        n_sig += 1
        code = ev.std_code
        bars_idx = index.get(code)
        if not bars_idx:
            continue
        ent = _resolve_entry(
            calendar,
            d,
            entry_lag=entry_lag,
            buy_weekday=buy_weekday,
            holiday_policy=holiday_policy,
        )
        if ent is None:
            continue
        planned_e, entry_date, e_shift = ent
        if start and entry_date < start:
            continue
        if end and entry_date > end:
            continue
        entry_bar = bars_idx.get(entry_date)
        if not entry_bar:
            continue
        entry_px = float(bar_session_price(entry_bar, buy_on))
        if entry_px <= 0:
            continue
        ex = _resolve_exit(
            calendar,
            entry_date,
            hold=hold,
            exit_weekday=exit_weekday,
            holiday_policy=holiday_policy,
        )
        if ex is None:
            continue
        planned_x, exit_date, x_shift, reason = ex
        if exit_date <= entry_date:
            continue
        exit_bar = bars_idx.get(exit_date)
        if not exit_bar:
            continue
        exit_px = float(bar_session_price(exit_bar, sell_on))
        if exit_px <= 0:
            continue
        ret = exit_px / entry_px - 1.0
        mfe, mae = _path_mfe_mae(bars_idx, calendar, entry_date, exit_date, entry_px)
        trades.append(
            FastTrade(
                std_code=code,
                signal_date=d,
                entry_date=entry_date,
                exit_date=exit_date,
                entry_price=entry_px,
                exit_price=exit_px,
                ret=float(ret),
                mfe=mfe,
                mae=mae,
                exit_reason=reason,
                planned_entry_date=planned_e,
                planned_exit_date=planned_x,
                entry_shift_days=int(e_shift or 0),
                exit_shift_days=int(x_shift or 0),
            )
        )

    metrics = _summarize(trades)
    return FastBacktestResult(
        n_signals=n_sig,
        n_trades=len(trades),
        trades=trades,
        metrics=metrics,
        notes=notes,
        config={
            "hold": hold,
            "entry_lag": entry_lag,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "holiday_policy": holiday_policy,
            "signal_weekdays": list(signal_weekdays) if signal_weekdays else None,
            "engine": "fast",
        },
    )
