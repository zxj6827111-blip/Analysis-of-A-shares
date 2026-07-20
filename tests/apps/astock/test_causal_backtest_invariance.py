"""End-to-end causal adjustment: future data must not rewrite history through T."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

import copy
from dataclasses import asdict

import numpy as np

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent, day_bars_to_adj
from wtpy.apps.astock.data.adjustments import align_factors_to_dates, causal_qfq_scale


def _cfg(capital=100_000.0, slip=0.0, lot=100):
    cfg = AStockConfig()
    cfg.initial_capital = capital
    cfg.max_weight = 1.0
    cfg.lot_size = lot
    # non-zero costs so share rounding is fee-sensitive
    cfg.costs = CostConfig(
        commission_rate=0.0003,
        min_commission=5.0,
        stamp_tax_rate=0.001,
        slippage=slip,
    )
    return cfg


def _run(raw_bars, factors, events, end, *, capital=100_000.0):
    code = "SSE.STK.600000"
    dates = [b.date for b in raw_bars]
    cal = TradeCalendar(dates)
    fac = np.asarray(factors, dtype=float)
    adj = day_bars_to_adj(raw_bars, fac)
    cfg = _cfg(capital=capital)
    bt = PortfolioBacktester(
        cfg,
        cal,
        {code: raw_bars},
        adj_bars_by_code={code: adj},
    )
    res = bt.run(
        events,
        hold=1,
        period="DAY",
        start=dates[0],
        end=end,
        formal_ok=True,
        research_unadjusted=False,
        _skip_zero_replay=True,
    )
    fills = [asdict(f) for f in res.fills if f.date <= end]
    equity = [asdict(e) for e in res.equity_curve if e.date <= end]
    # adjusted OHLC through end
    adj_thru = [
        {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        for b in adj
        if b.date <= end
    ]
    return {
        "adj": adj_thru,
        "fills": fills,
        "equity": equity,
        "metrics_prefix_equity": equity[-1]["equity"] if equity else None,
        "n_buys": sum(1 for f in fills if f["side"] == "BUY"),
        "buy_shares": [f["shares"] for f in fills if f["side"] == "BUY"],
        "buy_prices": [f["price"] for f in fills if f["side"] == "BUY"],
    }


def test_A_local_future_tail_invariance_full_portfolio():
    """Append bars+factors after T; re-run end=T must match exactly (incl. shares)."""
    code = "SSE.STK.600000"
    # History through T=20240105; prices chosen so lot rounding is sensitive
    hist_dates = [20240102, 20240103, 20240104, 20240105]
    hist_raw = [
        DayBar(20240102, 9.5, 10.0, 9.0, 9.8, 1e6, 1e5),
        DayBar(20240103, 9.8, 10.2, 9.5, 10.0, 1e6, 1e5),  # signal
        DayBar(20240104, 10.05, 10.5, 9.9, 10.2, 1e6, 1e5),  # buy open ~10.05
        DayBar(20240105, 10.1, 10.4, 9.8, 10.0, 1e6, 1e5),  # sell
    ]
    # cumulative factors: base 1.0 then corporate action mid-history
    hist_events = {20240102: 1.0, 20240104: 1.1}
    hist_fac = align_factors_to_dates(hist_events, hist_dates, seed_factor=1.0)
    events = [SignalEvent(code, 20240103, "DAY", "t")]
    T = 20240105
    r1 = _run(hist_raw, hist_fac, events, T, capital=50_000.0)

    # Append future tail with new corporate action that would change factor[-1]
    full_dates = hist_dates + [20240108, 20240109]
    full_raw = hist_raw + [
        DayBar(20240108, 20.0, 21.0, 19.0, 20.5, 1e6, 1e5),
        DayBar(20240109, 20.5, 21.0, 20.0, 20.8, 1e6, 1e5),
    ]
    full_events = dict(hist_events)
    full_events[20240108] = 1.25  # future CA
    full_fac = align_factors_to_dates(full_events, full_dates, seed_factor=1.0)
    # prove classic leak would change hist scale: factor[-1] path
    classic_hist = hist_fac / hist_fac[-1]
    classic_full_prefix = full_fac[:4] / full_fac[-1]
    assert not np.allclose(classic_hist, classic_full_prefix)

    r2 = _run(full_raw, full_fac, events, T, capital=50_000.0)

    assert r1["adj"] == r2["adj"]
    assert r1["fills"] == r2["fills"]
    assert r1["equity"] == r2["equity"]
    assert r1["buy_shares"] == r2["buy_shares"]
    assert r1["buy_prices"] == r2["buy_prices"]
    assert r1["n_buys"] >= 1


def test_B_end_prefix_invariance_with_lot_rounding():
    """end=T vs end=T2>T with new CA: prefix through T of fills/equity/adj identical."""
    code = "SSE.STK.600000"
    dates = [20240102, 20240103, 20240104, 20240105, 20240108, 20240109]
    raw = [
        DayBar(20240102, 9.5, 10.0, 9.0, 9.8, 1e6, 1e5),
        DayBar(20240103, 9.8, 10.2, 9.5, 10.0, 1e6, 1e5),
        DayBar(20240104, 10.05, 10.5, 9.9, 10.2, 1e6, 1e5),
        DayBar(20240105, 10.1, 10.4, 9.8, 10.0, 1e6, 1e5),
        DayBar(20240108, 11.0, 11.5, 10.5, 11.2, 1e6, 1e5),
        DayBar(20240109, 11.2, 11.6, 11.0, 11.3, 1e6, 1e5),
    ]
    events_T = {20240102: 1.0, 20240104: 1.1}
    events_T2 = dict(events_T)
    events_T2[20240108] = 1.25
    fac_T = align_factors_to_dates(events_T, dates[:4], seed_factor=1.0)
    fac_T2 = align_factors_to_dates(events_T2, dates, seed_factor=1.0)
    sig = [SignalEvent(code, 20240103, "DAY", "t")]
    T, T2 = 20240105, 20240109
    r_short = _run(raw[:4], fac_T, sig, T, capital=50_000.0)
    r_long = _run(raw, fac_T2, sig, T2, capital=50_000.0)

    # truncate long run to T
    long_adj = [x for x in r_long["adj"] if x["date"] <= T]
    long_fills = [x for x in r_long["fills"] if x["date"] <= T]
    long_eq = [x for x in r_long["equity"] if x["date"] <= T]
    assert r_short["adj"] == long_adj
    assert r_short["fills"] == long_fills
    assert r_short["equity"] == long_eq
    assert r_short["buy_shares"] == [
        f["shares"] for f in long_fills if f["side"] == "BUY"
    ]
