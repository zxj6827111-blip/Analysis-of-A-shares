"""Backtest hold period, T+1, slippage, limit-down, suspension tests."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.limit_rules import DefaultAShareLimitRule, LimitContext
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 100_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0, slippage=0.01)
    return cfg


def _cal(dates):
    return TradeCalendar(list(dates))


def _bars(code_dates_ohlc):
    """code -> list of (date, o,h,l,c)"""
    out = {}
    for code, rows in code_dates_ohlc.items():
        out[code] = [
            DayBar(d, o, h, l, c, 1.0, 1000)
            for d, o, h, l, c in rows
        ]
    return out


def test_day_hold1_t1_next_open_sell():
    # signal on D1 close -> buy D2 open, hold=1 -> sell D3 open
    dates = [20240102, 20240103, 20240104, 20240105]
    code = "SSE.STK.600000"
    bars = _bars(
        {
            code: [
                (20240102, 10, 11, 9, 10),
                (20240103, 10, 11, 9, 10.5),  # signal day
                (20240104, 11, 12, 10, 11),  # buy open 11
                (20240105, 12, 13, 11, 12),  # sell open 12
            ]
        }
    )
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    events = [SignalEvent(code, 20240103, "DAY", "t")]
    res = bt.run(events, hold=1, period="DAY", start=20240102, end=20240105, formal_ok=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and buys[0].date == 20240104
    assert len(sells) == 1 and sells[0].date == 20240105
    assert buys[0].price == 11
    assert sells[0].price == 12


def test_day_hold3_exit_day():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108, 20240109]
    code = "SSE.STK.600000"
    rows = [(d, 10, 11, 9, 10) for d in dates]
    bars = _bars({code: rows})
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    events = [SignalEvent(code, 20240102, "DAY", "t")]
    res = bt.run(events, hold=3, period="DAY", formal_ok=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys[0].date == 20240103
    # hold 3 sessions: 1/3,1/4,1/5 end -> sell 1/8 open
    assert sells[0].date == 20240108


def test_repeat_signal_does_not_reset_hold():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108]
    code = "SSE.STK.600000"
    bars = _bars({code: [(d, 10, 11, 9, 10) for d in dates]})
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    events = [
        SignalEvent(code, 20240102, "DAY", "t"),
        SignalEvent(code, 20240103, "DAY", "t"),  # while in position
    ]
    res = bt.run(events, hold=2, period="DAY", formal_ok=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1


def test_buy_sell_slippage():
    dates = [20240102, 20240103, 20240104]
    code = "SSE.STK.600000"
    bars = _bars(
        {
            code: [
                (20240102, 10, 11, 9, 10),
                (20240103, 10, 11, 9, 10),  # buy open 10
                (20240104, 10, 11, 9, 10),  # sell open 10
            ]
        }
    )
    cfg = _cfg()
    cfg.costs.slippage = 0.01
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, period="DAY", formal_ok=True)
    buy = [f for f in res.fills if f.side == "BUY"][0]
    sell = [f for f in res.fills if f.side == "SELL"][0]
    assert abs(buy.price - 10 * 1.01) < 1e-9
    assert abs(sell.price - 10 * 0.99) < 1e-9


def test_limit_down_defers_sell():
    dates = [20240102, 20240103, 20240104, 20240105]
    code = "SSE.STK.600000"
    # day3 limit down board 9.00 from prev 10
    bars = _bars(
        {
            code: [
                (20240102, 10, 10.5, 9.5, 10),
                (20240103, 10, 10.5, 9.5, 10),  # buy
                (20240104, 9.0, 9.0, 9.0, 9.0),  # limit down untradeable
                (20240105, 9.5, 10, 9.2, 9.8),  # sell
            ]
        }
    )
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, period="DAY", formal_ok=True)
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240105


def test_suspension_valuation_uses_last_close():
    dates = [20240102, 20240103, 20240104, 20240105]
    code = "SSE.STK.600000"
    # missing bar on 20240104 = suspension
    bars = _bars(
        {
            code: [
                (20240102, 10, 11, 9, 10),
                (20240103, 10, 11, 9, 12),  # buy, close 12
                # 20240104 suspended
                (20240105, 11, 12, 10, 11),
            ]
        }
    )
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=2, period="DAY", formal_ok=True)
    # equity on suspended day should use last close 12
    eq_by = {e.date: e for e in res.equity_curve}
    assert 20240104 in eq_by
    # if still holding, market value uses 12
    if eq_by[20240104].market_value > 0:
        # shares * 12
        assert abs(eq_by[20240104].market_value / 12 - round(eq_by[20240104].market_value / 12)) < 1e-6 or eq_by[20240104].market_value % 12 == 0


def test_week_hold_uses_period_ends():
    # construct two weeks
    dates = [
        20240102, 20240103, 20240104, 20240105,  # week1 ends 5
        20240108, 20240109, 20240110, 20240111, 20240112,  # week2 ends 12
        20240115, 20240116,
    ]
    code = "SSE.STK.600000"
    bars = _bars({code: [(d, 10, 11, 9, 10) for d in dates]})
    cfg = _cfg()
    cfg.costs.slippage = 0.0
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    # signal Friday week1 close
    res = bt.run([SignalEvent(code, 20240105, "WEEK", "t")], hold=1, period="WEEK", formal_ok=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys and buys[0].date == 20240108
    # after completing 1 week (ending 20240112), sell next open 20240115
    assert sells and sells[0].date == 20240115


def test_formal_nogo_without_factors_flag():
    dates = [20240102, 20240103]
    code = "SSE.STK.600000"
    bars = _bars({code: [(d, 10, 11, 9, 10) for d in dates]})
    cfg = _cfg()
    bt = PortfolioBacktester(cfg, _cal(dates), bars)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, formal_ok=False, research_unadjusted=False)
    assert res.status == "no_go"
