# -*- coding: utf-8 -*-
"""Portfolio backtest engine for A-share multi-period signals.

Public facade: re-exports models, schedule helpers, PortfolioBacktester, metrics.
Implementation is split across strategy_models / schedule / engine / metrics.
"""

from __future__ import annotations

from .strategy_models import (  # noqa: F401
    ENTRY_REASON_SIGNAL,
    EXIT_REASON_DELISTING_EXIT,
    EXIT_REASON_FORCED_EXIT,
    EXIT_REASON_GUA_WEAKENING,
    EXIT_REASON_REVERSE_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    LEGACY_HOLD_EXPIRED,
    BacktestResult,
    EquityPoint,
    Fill,
    Position,
    _commission,
    validate_risk_pct,
)
from .strategy_schedule import (  # noqa: F401
    _month_key,
    _week_key,
    bar_session_price,
    compose_sell_reason,
    filter_events_by_signal_weekdays,
    format_signal_weekdays,
    format_single_weekday,
    normalize_exit_reason,
    parse_price_session,
    parse_signal_weekdays,
    parse_single_weekday,
    session_label_cn,
    yyyymmdd_isoweekday,
)
from .strategy_engine import PortfolioBacktester  # noqa: F401
from .strategy_metrics import compute_metrics  # noqa: F401

__all__ = [
    "PortfolioBacktester",
    "BacktestResult",
    "Fill",
    "Position",
    "EquityPoint",
    "compute_metrics",
    "validate_risk_pct",
    "parse_price_session",
    "parse_signal_weekdays",
    "parse_single_weekday",
    "filter_events_by_signal_weekdays",
    "bar_session_price",
    "compose_sell_reason",
    "normalize_exit_reason",
]
