"""entry_lag: buy on N-th trading day after signal."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 100_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(
        commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0, slippage=0.0
    )
    return cfg


def _bars(code, dates, px=10.0):
    return {
        code: [DayBar(d, px, px + 1, px - 1, px, 1.0, 1000) for d in dates]
    }


def test_nth_trading_day_after():
    cal = TradeCalendar([20240102, 20240103, 20240104, 20240105])
    assert cal.nth_trading_day_after(20240102, 1) == 20240103
    assert cal.nth_trading_day_after(20240102, 2) == 20240104
    assert cal.next_trading_day(20240102) == cal.nth_trading_day_after(20240102, 1)


def test_entry_lag_1_matches_legacy_t1():
    dates = [20240102, 20240103, 20240104, 20240105]
    code = "SSE.STK.600000"
    bars = {
        code: [
            DayBar(20240102, 10, 11, 9, 10, 1, 1),
            DayBar(20240103, 10, 11, 9, 10.5, 1, 1),  # signal
            DayBar(20240104, 11, 12, 10, 11, 1, 1),  # buy lag=1
            DayBar(20240105, 12, 13, 11, 12, 1, 1),  # sell hold=1
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
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys[0].date == 20240104
    assert sells[0].date == 20240105
    assert res.config.get("entry_lag") == 1


def test_entry_lag_2_buys_second_session():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    code = "SSE.STK.600000"
    bars = _bars(code, dates, 10.0)
    # set distinct opens
    bars[code][2] = DayBar(20240104, 11, 12, 10, 11, 1, 1)
    bars[code][3] = DayBar(20240105, 13, 14, 12, 13, 1, 1)
    bars[code][4] = DayBar(20240108, 14, 15, 13, 14, 1, 1)
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    # signal 1/2 -> lag2 buy 1/4 open 11; hold=1 sell 1/5 open 13
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=1,
        entry_lag=2,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and buys[0].date == 20240104
    assert buys[0].price == 11
    assert len(sells) == 1 and sells[0].date == 20240105
    assert res.config.get("entry_lag") == 2


def test_entry_lag_rejects_zero():
    dates = [20240102, 20240103]
    code = "SSE.STK.600000"
    bars = _bars(code, dates)
    bt = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars)
    try:
        bt.run(
            [SignalEvent(code, 20240102, "DAY", "t")],
            hold=1,
            entry_lag=0,
            formal_ok=True,
            _skip_zero_replay=True,
        )
        assert False, "expected ValueError"
    except ValueError as e:
        assert "entry_lag" in str(e)
