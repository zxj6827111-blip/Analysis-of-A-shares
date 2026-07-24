# -*- coding: utf-8 -*-
"""Portfolio backtest data models and small pure helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config import CostConfig

# Canonical exit / entry reason codes (phase-1 research center).
EXIT_REASON_STOP_LOSS = "stop_loss"
EXIT_REASON_TAKE_PROFIT = "take_profit"
EXIT_REASON_TIME_EXIT = "time_exit"
EXIT_REASON_WEEKDAY_EXIT = "weekday_exit"
EXIT_REASON_REVERSE_SIGNAL = "reverse_signal"
EXIT_REASON_GUA_WEAKENING = "gua_weakening"
EXIT_REASON_FORCED_EXIT = "forced_exit"
EXIT_REASON_DELISTING_EXIT = "delisting_exit"
ENTRY_REASON_SIGNAL = "signal_entry"

# Legacy reason still accepted in compose fallbacks.
LEGACY_HOLD_EXPIRED = "hold_expired"


@dataclass
class Position:
    std_code: str
    shares: int
    entry_date: int
    entry_price: float  # RAW execution entry (with slippage)
    # For DAY/DWM: remaining trading sessions after entry before exit is allowed
    hold_left_sessions: int
    # For WEEK/MONTH: number of full periods still required after entry period
    hold_left_periods: int
    period_mode: str  # DAY | WEEK | MONTH | DWM
    cost: float  # true cash cost (amount + commission at RAW execution)
    entry_period_key: Optional[Tuple] = None
    # Optional risk exits (fraction, e.g. 0.03 = 3%).
    # trigger_on_daily_high_low including entry day; execute_next_trading_day_open (T+1).
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trigger_reason: Optional[str] = None  # stop_loss | take_profit (sticky once set)
    defer_reason: Optional[str] = None  # suspended | limit_down | bad_price while waiting to sell
    # When set: force time-stop on this calendar trading day (weekday-based exit).
    exit_date: Optional[int] = None
    # Schedule provenance (phase-1)
    planned_entry_date: Optional[int] = None
    entry_shift_days: int = 0
    planned_exit_date: Optional[int] = None
    exit_shift_days: int = 0
    holiday_policy: Optional[str] = None
    time_exit_kind: str = "time_exit"  # time_exit | weekday_exit
    # Dual-price audit: optional adjusted reference at entry (not used for PnL).
    entry_adjusted_reference_price: Optional[float] = None
    entry_factor: Optional[float] = None  # factor snapshot at entry for CA fail-closed


@dataclass
class Fill:
    date: int
    std_code: str
    side: str
    price: float  # execution price (RAW session * slippage)
    shares: int
    amount: float
    commission: float
    stamp_tax: float
    reason: str
    planned_date: Optional[int] = None
    actual_date: Optional[int] = None
    shift_days: int = 0
    holiday_policy: Optional[str] = None
    # RAW session open/close before slippage (market tape); equals execution basis.
    raw_price: Optional[float] = None
    # Optional causal-qfq (or other adj) session price same date for audit only.
    adjusted_reference_price: Optional[float] = None
    adjustment_factor: Optional[float] = None
    adjustment_base: Optional[float] = None
    adjustment_scale: Optional[float] = None
    price_session: Optional[str] = None  # open | close
    price_source: Optional[str] = None  # "raw" | "adjusted_reference"


@dataclass
class EquityPoint:
    date: int
    cash: float
    market_value: float
    equity: float


@dataclass
class BacktestResult:
    run_id: str
    config: dict
    fills: List[Fill] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    status: str = "ok"  # ok | research_unadjusted | no_go | unsupported_corporate_action

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "config": self.config,
            "fills": [asdict(f) for f in self.fills],
            "equity_curve": [asdict(e) for e in self.equity_curve],
            "metrics": self.metrics,
            "notes": self.notes,
        }


def _commission(amount: float, costs: CostConfig) -> float:
    return max(amount * costs.commission_rate, costs.min_commission)


def validate_risk_pct(name: str, value: Optional[float]) -> Optional[float]:
    """Require None or strictly 0 < value < 1. Never clamp or abs()."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be a number, got {value!r}") from e
    if not (0.0 < v < 1.0):
        raise ValueError(f"{name} must satisfy 0 < value < 1, got {v}")
    return v



# Weekday allow-list: ISO 1=Monday … 7=Sunday (matches datetime.isoweekday()).
_WEEKDAY_ALIASES = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "mon": 1, "monday": 1, "一": 1, "周一": 1, "星期一": 1,
    "tue": 2, "tues": 2, "tuesday": 2, "二": 2, "周二": 2, "星期二": 2,
    "wed": 3, "wednesday": 3, "三": 3, "周三": 3, "星期三": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "四": 4, "周四": 4, "星期四": 4,
    "fri": 5, "friday": 5, "五": 5, "周五": 5, "星期五": 5,
    "sat": 6, "saturday": 6, "六": 6, "周六": 6, "星期六": 6,
    "sun": 7, "sunday": 7, "日": 7, "天": 7, "周日": 7, "周天": 7, "星期日": 7, "星期天": 7,
}
_WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}



