# -*- coding: utf-8 -*-
"""Period bagua: default WEEK gua on day signals; week OHLC not last-day OHLC."""

from __future__ import annotations

from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.periods import aggregate_week
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.study import SignalEvent, attach_bagua, find_bar_covering_date


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def test_week_bagua_uses_aggregated_ohlc_not_last_day():
    calc = BaguaCalculator.from_json(JSON_PATH)
    days = [
        DayBar(20240102, 6.27, 7.00, 6.00, 6.50, 1, 1),
        DayBar(20240103, 6.50, 7.33, 5.90, 6.00, 1, 1),
        DayBar(20240104, 6.00, 6.20, 5.95, 6.10, 1, 1),
        DayBar(20240105, 8.88, 8.90, 8.80, 8.85, 1, 1),  # Friday
    ]
    weeks = aggregate_week(days, include_open=True)
    assert len(weeks) == 1
    w = weeks[0]
    assert w.open == 6.27
    assert w.close == 8.85
    assert w.high == 8.90
    assert w.low == 5.90
    r_week = calc.calculate(
        open_price=w.open, high_price=w.high, low_price=w.low, close_price=w.close
    )
    r_last = calc.calculate(
        open_price=8.88, high_price=8.90, low_price=8.80, close_price=8.85
    )
    assert r_week.open_price == "6.27"
    assert r_last.open_price == "8.88"
    assert (r_week.upper_id, r_week.lower_id, r_week.yao_order) != (
        r_last.upper_id,
        r_last.lower_id,
        r_last.yao_order,
    )

    # Already-week bars still attach
    ev = [SignalEvent("SSE.STK.1", w.date, "WEEK", "t")]
    attach_bagua(ev, {"SSE.STK.1": weeks}, calc, bagua_period="WEEK")
    assert ev[0].bagua is not None
    assert ev[0].bagua["open_price"] == "6.27"
    assert ev[0].bagua["close_price"] == "8.85"
    assert ev[0].bagua.get("bagua_period") == "WEEK"


def test_friday_day_signal_binds_week_gua_not_day_gua():
    """Product path: day signal date (Friday) → stock week hexagram / 变卦."""
    calc = BaguaCalculator.from_json(JSON_PATH)
    days = [
        DayBar(20240102, 6.27, 7.00, 6.00, 6.50, 1, 1),
        DayBar(20240103, 6.50, 7.33, 5.90, 6.00, 1, 1),
        DayBar(20240104, 6.00, 6.20, 5.95, 6.10, 1, 1),
        DayBar(20240105, 8.88, 8.90, 8.80, 8.85, 1, 1),  # Friday signal
    ]
    # Day-gua from Friday alone
    day_g = calc.calculate(
        open_price=8.88, high_price=8.90, low_price=8.80, close_price=8.85
    )
    # Default attach: DAY bars in map, bagua_period=WEEK
    ev = [SignalEvent("SSE.STK.1", 20240105, "DAY", "macd")]
    attach_bagua(ev, {"SSE.STK.1": days}, calc)  # default WEEK
    bg = ev[0].bagua
    assert bg is not None
    assert bg.get("bagua_period") == "WEEK"
    assert bg["open_price"] == "6.27"
    assert bg["close_price"] == "8.85"
    # Must differ from pure Friday day gua
    assert (bg.get("upper_id"), bg.get("lower_id"), bg.get("yao_order")) != (
        day_g.upper_id,
        day_g.lower_id,
        day_g.yao_order,
    )
    # 变卦 present on knowledge-backed result when available
    assert "biangua" in bg or "changed_hexagram_name" in bg
    assert bg.get("bagua_bar_start") == 20240102
    assert bg.get("bagua_bar_end") == 20240105


def test_find_bar_covering_midweek():
    days = [
        DayBar(20240102, 1, 2, 0.5, 1, 1, 1),
        DayBar(20240103, 1, 2, 0.5, 1.1, 1, 1),
        DayBar(20240105, 1, 2, 0.5, 1.2, 1, 1),
    ]
    weeks = aggregate_week(days, include_open=True)
    bar = find_bar_covering_date(weeks, 20240103)
    assert bar is not None
    assert int(bar.start_date) <= 20240103 <= int(bar.end_date)
