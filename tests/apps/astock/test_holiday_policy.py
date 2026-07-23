# -*- coding: utf-8 -*-
"""Phase-1: holiday_policy for weekday schedule anchors."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import (
    HOLIDAY_POLICY_EXACT,
    HOLIDAY_POLICY_NEXT,
    HOLIDAY_POLICY_PREV,
    HOLIDAY_POLICY_SKIP,
    TradeCalendar,
    first_calendar_date_on_weekday,
    normalize_holiday_policy,
)
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import (
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    PortfolioBacktester,
)
from wtpy.apps.astock.study import SignalEvent


def test_normalize_holiday_policy():
    assert normalize_holiday_policy(None) == HOLIDAY_POLICY_NEXT
    assert normalize_holiday_policy("next") == HOLIDAY_POLICY_NEXT
    assert normalize_holiday_policy("previous_trading_day") == HOLIDAY_POLICY_PREV


def test_first_calendar_date_on_weekday():
    # 20240103 = Wednesday
    assert first_calendar_date_on_weekday(20240103, 5, strict=True) == 20240105  # Fri
    assert first_calendar_date_on_weekday(20240103, 1, strict=True) == 20240108  # Mon


def test_resolve_weekday_rolls_when_target_is_holiday():
    """Fri 20240105 is a holiday: only Mon 1/8 and Tue 1/9 trade."""
    # Civil Fri 20240105 missing from calendar → holiday
    cal = TradeCalendar([20240103, 20240104, 20240108, 20240109])  # Wed Thu Mon Tue
    planned = first_calendar_date_on_weekday(20240103, 5, strict=True)
    assert planned == 20240105
    # Legacy next_weekday jumps to next Fri (none in list) or would skip weeks
    assert cal.next_weekday_trading_day(20240103, 5, strict=True) is None
    # next_trading_day policy: roll Fri holiday → Mon 1/8
    got = cal.resolve_weekday_session(
        20240103, 5, strict=True, holiday_policy=HOLIDAY_POLICY_NEXT
    )
    assert got is not None
    planned_d, actual, shift = got
    assert planned_d == 20240105
    assert actual == 20240108
    assert shift == 3

    # previous: roll back to Thu 1/4
    got_p = cal.resolve_weekday_session(
        20240103, 5, strict=True, holiday_policy=HOLIDAY_POLICY_PREV
    )
    assert got_p is not None
    assert got_p[1] == 20240104

    # skip / exact: cancel
    assert (
        cal.resolve_weekday_session(
            20240103, 5, strict=True, holiday_policy=HOLIDAY_POLICY_SKIP
        )
        is None
    )
    assert (
        cal.resolve_weekday_session(
            20240103, 5, strict=True, holiday_policy=HOLIDAY_POLICY_EXACT
        )
        is None
    )


def test_resolve_when_weekday_is_trading_day_shift_zero():
    cal = TradeCalendar([20240103, 20240104, 20240105, 20240108])
    got = cal.resolve_weekday_session(
        20240103, 5, strict=True, holiday_policy=HOLIDAY_POLICY_NEXT
    )
    assert got == (20240105, 20240105, 0)


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_backtest_holiday_roll_records_planned_actual_on_fill():
    """Signal Wed; buy planned Fri holiday → actual Mon; exit Tue weekday."""
    code = "SSE.STK.600000"
    # Wed Thu (no Fri) Mon Tue Wed
    dates = [20240103, 20240104, 20240108, 20240109, 20240110]
    bars = {
        code: [DayBar(d, 10 + i, 12, 9, 10.5, 1, 1000) for i, d in enumerate(dates)]
    }
    bars[code][2] = DayBar(20240108, 12.0, 13, 11, 12.5, 1, 1000)
    bars[code][3] = DayBar(20240109, 14.0, 15, 13, 14.5, 1, 1000)

    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        entry_lag=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        buy_weekday=5,  # Friday (holiday)
        exit_weekday=2,  # Tuesday
        buy_on="open",
        sell_on="open",
        holiday_policy=HOLIDAY_POLICY_NEXT,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1
    assert buys[0].date == 20240108
    assert buys[0].planned_date == 20240105
    assert buys[0].actual_date == 20240108
    assert buys[0].shift_days == 3
    assert buys[0].holiday_policy == HOLIDAY_POLICY_NEXT
    assert len(sells) == 1
    assert sells[0].date == 20240109
    assert sells[0].reason == EXIT_REASON_WEEKDAY_EXIT
    assert res.config.get("holiday_policy") == HOLIDAY_POLICY_NEXT


def test_time_exit_reason_for_hold_path():
    code = "SSE.STK.600000"
    dates = [20240102, 20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240102, 10, 11, 9, 10, 1, 1),
            DayBar(20240103, 10, 11, 9, 10.5, 1, 1),
            DayBar(20240104, 11, 12, 10, 11, 1, 1),
            DayBar(20240105, 12, 13, 11, 12, 1, 1),
        ]
    }
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        entry_lag=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells and sells[0].reason == EXIT_REASON_TIME_EXIT
