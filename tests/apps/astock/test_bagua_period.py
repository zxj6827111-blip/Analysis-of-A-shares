"""Period bagua uses aggregated OHLC for week/month."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.periods import aggregate_week
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.study import SignalEvent, attach_bagua


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
        DayBar(20240105, 8.88, 8.90, 8.80, 8.85, 1, 1),
    ]
    weeks = aggregate_week(days)
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

    ev = [SignalEvent("SSE.STK.1", w.date, "WEEK", "t")]
    attach_bagua(ev, {"SSE.STK.1": weeks}, calc)
    assert ev[0].bagua is not None
    assert ev[0].bagua["open_price"] == "6.27"
    assert ev[0].bagua["close_price"] == "8.85"
