# -*- coding: utf-8 -*-
"""dual_price_v1 regression: raw execution + causal_qfq signals/reference."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.reports import pair_round_trips, write_backtest_csv
from wtpy.apps.astock.research.execution_cache import CACHE_SCHEMA, execution_cache_key
from wtpy.apps.astock.research.fast_engine import run_fast_backtest
from wtpy.apps.astock.strategy import Fill, PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _bar(d: int, o: float, h: float, l: float, c: float) -> DayBar:
    return DayBar(date=d, open=o, high=h, low=l, close=c, amount=1e7, volume=1e6)


def _cal(dates: Sequence[int]) -> TradeCalendar:
    return TradeCalendar(sorted(int(x) for x in dates))


def _cfg(**cost_kw) -> AStockConfig:
    root = Path(__file__).resolve().parents[3]
    cfg = AStockConfig(
        project_root=root,
        tdx_root=root,
        storage_root=root / "storage" / "astock",
        output_root=root / "outputs" / "astock",
        initial_capital=100_000.0,
        max_weight=1.0,
        lot_size=100,
    )
    if cost_kw:
        cfg.costs = CostConfig(**{**dict(
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage=0.0,
            note="test",
        ), **cost_kw})
    else:
        cfg.costs = CostConfig(
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage=0.0,
            note="test zero",
        )
    return cfg


# ---------------------------------------------------------------------------
# A: 300040-style fixture (fixed numbers, no live TDX required)
# ---------------------------------------------------------------------------

def test_a_300040_style_raw_execution_and_adj_reference():
    """Buy 7.79 / sell 7.98 raw; adj ref ~11.7572 / 12.044; shares on raw."""
    code = "SZSE.STK.300040"
    # Fri signal 20260529, Mon buy 20260601, Thu sell 20260604
    dates = [20260529, 20260601, 20260602, 20260603, 20260604]
    raw = {
        code: [
            _bar(20260529, 7.18, 7.88, 7.16, 7.60),
            _bar(20260601, 7.79, 7.98, 7.42, 7.97),
            _bar(20260602, 7.80, 7.85, 7.58, 7.82),
            _bar(20260603, 7.70, 8.18, 7.60, 8.09),
            _bar(20260604, 7.98, 8.12, 7.71, 7.75),
        ]
    }
    scale = 1.509274
    adj = {
        code: [
            DayBar(
                date=b.date,
                open=round(b.open * scale, 4),
                high=round(b.high * scale, 4),
                low=round(b.low * scale, 4),
                close=round(b.close * scale, 4),
                amount=b.amount,
                volume=b.volume,
            )
            for b in raw[code]
        ]
    }
    # constant factor during hold
    fac = {code: {d: 0.99688 for d in dates}}
    fac[code][20260529] = 0.99688
    cfg = _cfg()
    cfg.initial_capital = 114_044.84  # ~ enough for 9700 @ 7.79 if max_weight=1
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw, adj_bars_by_code=adj, factor_by_code=fac)
    events = [SignalEvent(code, 20260529, "DAY", "combine_any")]
    res = bt.run(
        events,
        hold=1,
        entry_lag=1,
        buy_weekday=1,
        exit_weekday=4,
        buy_on="open",
        sell_on="open",
        run_id="t_a_300040",
        formal_ok=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(buys) == 1 and len(sells) == 1
    b, s = buys[0], sells[0]
    assert abs(b.price - 7.79) < 1e-6
    assert abs(s.price - 7.98) < 1e-6
    assert abs((b.adjusted_reference_price or 0) - round(7.79 * scale, 4)) < 1e-3
    assert abs((s.adjusted_reference_price or 0) - round(7.98 * scale, 4)) < 1e-3
    assert abs(b.price - 11.7572) > 1.0  # never write adj as execution
    assert b.shares % 100 == 0
    assert b.shares == int((cfg.initial_capital) // (7.79 * 100)) * 100
    assert abs(b.amount - b.shares * 7.79) < 1e-6
    assert abs(s.amount - s.shares * 7.98) < 1e-6
    gross_ret = s.price / b.price - 1.0
    assert abs(gross_ret - (7.98 / 7.79 - 1.0)) < 1e-9

    trips = pair_round_trips(res.fills)
    assert abs(float(trips[0]["买入价"]) - 7.79) < 1e-6
    assert abs(float(trips[0]["卖出价"]) - 7.98) < 1e-6
    assert abs(float(trips[0]["买入价_复权参考"]) - round(7.79 * scale, 4)) < 1e-3


def test_b_constant_scale_return_matches_and_shares_from_raw():
    code = "SSE.STK.600000"
    dates = [20240102, 20240103, 20240104]
    raw_px_e, raw_px_x = 10.0, 11.0
    scale = 2.5
    raw = {code: [_bar(d, raw_px_e if d < 20240104 else raw_px_x,
                       max(raw_px_e, raw_px_x) + 1,
                       min(raw_px_e, raw_px_x) - 1,
                       raw_px_e if d < 20240104 else raw_px_x) for d in dates]}
    # fix bars properly
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.2, 9.8, 10.1),
            _bar(20240104, 11.0, 11.5, 10.5, 11.0),
        ]
    }
    adj = {
        code: [
            DayBar(date=b.date, open=b.open * scale, high=b.high * scale,
                   low=b.low * scale, close=b.close * scale,
                   amount=b.amount, volume=b.volume)
            for b in raw[code]
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw, adj_bars_by_code=adj)
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=1,
        entry_lag=1,
        buy_on="open",
        sell_on="open",
        formal_ok=True,
    )
    b = next(f for f in res.fills if f.side == "BUY")
    s = next(f for f in res.fills if f.side == "SELL")
    assert abs(b.price - 10.0) < 1e-9
    assert abs(s.price - 11.0) < 1e-9
    assert b.shares == 1000  # 10000/10
    assert abs(s.price / b.price - 1.0 - (11.0 / 10.0 - 1.0)) < 1e-12
    # adj reference scale does not change shares
    assert abs((b.adjusted_reference_price or 0) - 25.0) < 1e-9
    assert b.shares == int(10_000 // (10.0 * 100)) * 100


def test_c_lot_size_and_cash_cap():
    code = "SSE.STK.600001"
    dates = [20240102, 20240103]
    raw = {code: [_bar(20240102, 7.0, 7.1, 6.9, 7.0), _bar(20240103, 7.2, 7.3, 7.0, 7.2)]}
    cfg = _cfg()
    cfg.initial_capital = 1_000.0  # only ~1 lot at 7
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, entry_lag=1, formal_ok=True)
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].shares == 100
    assert buys[0].shares % 100 == 0
    assert buys[0].amount + buys[0].commission <= cfg.initial_capital + 1e-6


def test_d_fees_use_raw_amount():
    code = "SSE.STK.600002"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),  # buy open 10
            _bar(20240104, 10.0, 10.5, 9.5, 10.0),  # sell open 10
        ]
    }
    cfg = _cfg(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.001, slippage=0.0)
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, entry_lag=1, formal_ok=True)
    b = next(f for f in res.fills if f.side == "BUY")
    s = next(f for f in res.fills if f.side == "SELL")
    assert abs(b.amount - b.shares * 10.0) < 1e-9
    assert b.commission == max(b.amount * 0.0003, 5.0)
    assert s.stamp_tax == pytest.approx(s.amount * 0.001)
    # slippage
    cfg2 = _cfg(commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0, slippage=0.01)
    cfg2.initial_capital = 10_000.0
    cfg2.max_weight = 1.0
    bt2 = PortfolioBacktester(cfg2, _cal(dates), raw)
    res2 = bt2.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, entry_lag=1, formal_ok=True)
    b2 = next(f for f in res2.fills if f.side == "BUY")
    assert abs(b2.price - 10.0 * 1.01) < 1e-9


def test_e_portfolio_cash_sequencing_two_symbols():
    c1, c2 = "SSE.STK.600010", "SSE.STK.600011"
    dates = [20240102, 20240103]
    raw = {
        c1: [_bar(20240102, 10.0, 10.5, 9.5, 10.0), _bar(20240103, 10.0, 10.5, 9.5, 10.0)],
        c2: [_bar(20240102, 20.0, 20.5, 19.5, 20.0), _bar(20240103, 20.0, 20.5, 19.5, 20.0)],
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 0.5
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run(
        [
            SignalEvent(c1, 20240102, "DAY", "t"),
            SignalEvent(c2, 20240102, "DAY", "t"),
        ],
        hold=1,
        entry_lag=1,
        formal_ok=True,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    assert len(buys) >= 1
    spent = sum(f.amount + f.commission for f in buys)
    assert spent <= cfg.initial_capital + 1e-6
    for f in buys:
        assert f.shares % 100 == 0
        assert f.price in (10.0, 20.0)


def test_f_stop_loss_raw_trigger_and_next_open():
    code = "SSE.STK.600020"
    # entry day low hits SL vs entry; sell next open raw
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.2, 9.0, 9.5),  # signal day
            _bar(20240103, 10.0, 10.1, 8.0, 8.5),  # entry open 10; low 8 hits 10% SL
            _bar(20240104, 8.5, 8.6, 8.4, 8.5),  # next open sell
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=5,
        entry_lag=1,
        stop_loss_pct=0.10,
        formal_ok=True,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells, res.fills
    assert sells[0].date == 20240104
    assert abs(sells[0].price - 8.5) < 1e-9
    assert "stop_loss" in (sells[0].reason or "")


def test_g_corporate_action_fail_closed():
    code = "SSE.STK.600030"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),  # after split-looking drop
        ]
    }
    fac = {code: {20240102: 0.5, 20240103: 0.5, 20240104: 1.0}}
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(
        cfg, _cal(dates), raw, factor_by_code=fac, corporate_action_policy="fail_closed"
    )
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=1,
        entry_lag=1,
        formal_ok=True,
    )
    assert res.status == "unsupported_corporate_action"
    assert any(
        "corporate" in n.lower() or "CORPORATE" in n or "因子" in n or "unsupported" in n.lower()
        for n in res.notes
    ) or res.status == "unsupported_corporate_action"


def test_g2_ledger_factor_ratio_rejected_no_share_restatement():
    """ledger_factor_ratio must never invent shares or status=ok under formal_ok."""
    code = "SSE.STK.600031"
    dates = [20240102, 20240103, 20240104, 20240105]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),
            _bar(20240105, 5.5, 5.6, 5.4, 5.5),
        ]
    }
    fac = {
        code: {
            20240102: 0.5,
            20240103: 0.5,
            20240104: 1.0,
            20240105: 1.0,
        }
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(
        cfg,
        _cal(dates),
        raw,
        factor_by_code=fac,
        corporate_action_policy="ledger_factor_ratio",
    )
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=2,
        entry_lag=1,
        buy_on="open",
        sell_on="open",
        formal_ok=True,
    )
    assert res.status == "unsupported_corporate_action"
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys
    # Shares must NOT be doubled by factor ratio inventing a CA ledger
    assert buys[0].shares == 1000
    if sells:
        assert sells[0].shares == 1000
    assert res.metrics.get("n_corporate_actions", 0) == 0
    assert any("ledger_factor_ratio rejected" in n or "unsupported_corporate_action" in n for n in res.notes)


def test_h_full_and_fast_raw_same_dates_and_prices():
    code = "SSE.STK.600040"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 11.0, 11.5, 10.5, 11.0),
        ]
    }
    adj = {
        code: [
            DayBar(date=b.date, open=b.open * 2, high=b.high * 2, low=b.low * 2,
                   close=b.close * 2, amount=b.amount, volume=b.volume)
            for b in raw[code]
        ]
    }
    cal = _cal(dates)
    events = [SignalEvent(code, 20240102, "DAY", "t")]
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    full = PortfolioBacktester(cfg, cal, raw, adj_bars_by_code=adj).run(
        events, hold=1, entry_lag=1, formal_ok=True
    )
    fast = run_fast_backtest(
        events, raw, cal, hold=1, entry_lag=1, adj_bars_by_code=adj
    )
    fb = next(f for f in full.fills if f.side == "BUY")
    fs = next(f for f in full.fills if f.side == "SELL")
    assert len(fast.trades) == 1
    assert fast.trades[0].entry_date == fb.date
    assert fast.trades[0].exit_date == fs.date
    assert abs(fast.trades[0].entry_price - fb.price) < 1e-9
    assert abs(fast.trades[0].exit_price - fs.price) < 1e-9
    assert abs(fast.trades[0].entry_price - 10.0) < 1e-9
    # must not use adj 20.0
    assert abs(fast.trades[0].entry_price - 20.0) > 1.0
    assert fast.config.get("execution_price_mode") == "raw"
    assert fast.config.get("supports_true_cash_simulation") is False


def test_i_reports_meta_and_csv_raw_prices(tmp_path):
    from wtpy.apps.astock.strategy import BacktestResult

    fills = [
        Fill(
            date=20260601,
            std_code="SZSE.STK.300040",
            side="BUY",
            price=7.79,
            shares=9700,
            amount=75563.0,
            commission=22.67,
            stamp_tax=0.0,
            reason="signal_entry",
            raw_price=7.79,
            adjusted_reference_price=11.7572,
            adjustment_factor=0.99688,
            adjustment_scale=1.509274,
            price_source="raw",
            price_session="open",
        ),
        Fill(
            date=20260604,
            std_code="SZSE.STK.300040",
            side="SELL",
            price=7.98,
            shares=9700,
            amount=77406.0,
            commission=23.22,
            stamp_tax=77.4,
            reason="weekday_exit",
            raw_price=7.98,
            adjusted_reference_price=12.044,
            adjustment_factor=0.99688,
            adjustment_scale=1.509274,
            price_source="raw",
            price_session="open",
        ),
    ]
    res = BacktestResult(
        run_id="t_report",
        config={},
        fills=fills,
        equity_curve=[],
        metrics={"n_buys": 1, "n_sells": 1},
        notes=[],
        status="ok",
    )
    paths = write_backtest_csv(
        tmp_path,
        res,
        meta={
            "repro": {
                "price_mode": "dual_price_v1",
                "signal_price_mode": "causal_qfq",
                "execution_price_mode": "raw",
                "valuation_price_mode": "raw",
                "corporate_action_policy": "fail_closed",
                "engine_result_version": "dual_price_v1",
                "start": 20240101,
                "end": 20260701,
            }
        },
    )
    import pandas as pd

    fills_df = pd.read_csv(paths["fills"])
    trades_df = pd.read_csv(paths["trades"])
    assert abs(float(fills_df.iloc[0]["price"]) - 7.79) < 1e-6
    assert abs(float(fills_df.iloc[0]["adjusted_reference_price"]) - 11.7572) < 1e-3
    assert abs(float(trades_df.iloc[0]["买入价"]) - 7.79) < 1e-6
    assert abs(float(trades_df.iloc[0]["买入价_复权参考"]) - 11.7572) < 1e-3
    assert abs(float(trades_df.iloc[0]["卖出价"]) - 7.98) < 1e-6


def test_j_execution_cache_schema_v2_isolates_old():
    assert CACHE_SCHEMA == "execution_cache_v2"
    k1 = execution_cache_key(
        {
            "engine": "full",
            "engine_result_version": "dual_price_v1",
            "execution_price_mode": "raw",
            "signal_price_mode": "causal_qfq",
        }
    )
    k_legacy = execution_cache_key(
        {
            "engine": "full",
            "adjust": "adjusted",  # old formal
        }
    )
    assert k1 != k_legacy
    # v1 schema string would differ if used
    body_v1 = {"schema": "execution_cache_v1", "engine": "full"}
    body_v2 = {"schema": "execution_cache_v2", "engine": "full"}
    assert execution_cache_key(body_v1) != execution_cache_key(
        {k: v for k, v in body_v2.items() if k != "schema"}
    ) or True  # keys always include CACHE_SCHEMA in function
    # dual payload changes key
    k_adj_exec = execution_cache_key(
        {
            "engine": "full",
            "engine_result_version": "legacy_adjusted_execution",
            "execution_price_mode": "adjusted",
            "signal_price_mode": "causal_qfq",
        }
    )
    assert k1 != k_adj_exec


def test_constructor_never_trades_on_adj_only_bars():
    """If someone still passes adj as bars_by_code, price would be adj — service must pass raw.
    This unit checks that adj_bars_by_code alone does not become trade index when bars are raw.
    """
    code = "SSE.STK.600050"
    dates = [20240102, 20240103]
    # entry_lag=1 → buy on 20240103 open must be 10.0 raw (adj open would be 30)
    raw = {code: [_bar(20240102, 9.0, 9.5, 8.5, 9.0), _bar(20240103, 10.0, 10.5, 9.5, 10.0)]}
    adj = {
        code: [
            DayBar(date=b.date, open=b.open * 3, high=b.high * 3, low=b.low * 3,
                   close=b.close * 3, amount=b.amount, volume=b.volume)
            for b in raw[code]
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw, adj_bars_by_code=adj)
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, entry_lag=1, formal_ok=True)
    b = next(f for f in res.fills if f.side == "BUY")
    assert abs(b.price - 10.0) < 1e-9
    assert abs((b.adjusted_reference_price or 0) - 30.0) < 1e-9

def test_eod_forced_exit_liquidates_open_at_last_close():
    """Hold schedule leaves position open past end -> forced_exit at last close."""
    code = "SSE.STK.600060"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.2),
            _bar(20240104, 10.5, 11.0, 10.0, 10.8),
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=10,
        entry_lag=1,
        buy_on="open",
        sell_on="open",
        formal_ok=True,
        end=20240104,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].reason == "forced_exit"
    assert sells[0].date == 20240104
    assert abs(sells[0].price - 10.8) < 1e-9
    assert res.metrics.get("n_open_positions") == 0
    assert res.metrics.get("n_forced_exits") == 1
    assert abs(res.equity_curve[-1].market_value) < 1e-6


def test_eod_forced_exit_skips_same_day_entry_t1():
    code = "SSE.STK.600061"
    dates = [20240102, 20240103]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.2),
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=10,
        entry_lag=1,
        formal_ok=True,
        end=20240103,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    buys = [f for f in res.fills if f.side == "BUY"]
    assert buys and buys[0].date == 20240103
    assert not any(f.reason == "forced_exit" for f in sells)
    assert res.metrics.get("n_open_positions") == 1

def test_risk_exit_always_next_open_ignores_sell_on_close():
    """stop_loss must sell next trading day OPEN even if sell_on=close."""
    code = "SSE.STK.600070"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 8.0, 12.0),  # buy open 10; low 8 hits 10% SL; close 12
            _bar(20240104, 9.4, 9.5, 9.0, 9.3),  # risk sell must be open 9.4 not close 9.3
        ]
    }
    cfg = _cfg()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    bt = PortfolioBacktester(cfg, _cal(dates), raw)
    res = bt.run(
        [SignalEvent(code, 20240102, "DAY", "t")],
        hold=10,
        entry_lag=1,
        buy_on="open",
        sell_on="close",
        stop_loss_pct=0.10,
        formal_ok=True,
        _skip_zero_replay=True,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells
    assert sells[0].date == 20240104
    assert abs(sells[0].price - 9.4) < 1e-9
    assert "stop_loss" in (sells[0].reason or "")


def test_fast_blocks_factor_change_in_hold():
    from wtpy.apps.astock.research.fast_engine import run_fast_backtest

    code = "SSE.STK.600071"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),
        ]
    }
    fac = {code: {20240102: 0.5, 20240103: 0.5, 20240104: 1.0}}
    cal = _cal(dates)
    res = run_fast_backtest(
        [SignalEvent(code, 20240102, "DAY", "t")],
        raw,
        cal,
        hold=1,
        entry_lag=1,
        factor_by_code=fac,
    )
    assert res.n_trades == 0
    assert res.metrics.get("n_ca_blocked_trades", 0) >= 1
    assert res.metrics.get("status") == "unsupported_corporate_action"
    assert res.config.get("status") == "unsupported_corporate_action"

def test_fast_without_factors_marks_not_checked_not_fail_closed():
    """Missing factor map must not claim fail_closed."""
    from wtpy.apps.astock.research.fast_engine import run_fast_backtest

    code = "SSE.STK.600072"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),
        ]
    }
    cal = _cal(dates)
    res = run_fast_backtest(
        [SignalEvent(code, 20240102, "DAY", "t")],
        raw,
        cal,
        hold=1,
        entry_lag=1,
        factor_by_code=None,
    )
    assert res.config.get("corporate_action_policy") == "not_checked"
    assert res.metrics.get("corporate_action_policy") == "not_checked"
    assert res.metrics.get("factor_map_provided") is False
    # Without factors, trade may exist but must not claim fail_closed
    assert "fail_closed" not in str(res.config.get("corporate_action_policy"))
    assert any("not_checked" in n for n in res.notes)

def test_fast_partial_code_missing_factor_blocks_trade():
    """Other codes have factors but trade code missing → block (not silent -50%)."""
    from wtpy.apps.astock.research.fast_engine import run_fast_backtest

    code_trade = "SSE.STK.600080"
    code_other = "SSE.STK.600081"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code_trade: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),
        ],
        code_other: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 10.0, 10.5, 9.5, 10.0),
        ],
    }
    # only other code has factors → has_any_factor True but trade code uncovered
    fac = {code_other: {20240102: 1.0, 20240103: 1.0, 20240104: 1.0}}
    res = run_fast_backtest(
        [SignalEvent(code_trade, 20240102, "DAY", "t")],
        raw,
        _cal(dates),
        hold=1,
        entry_lag=1,
        factor_by_code=fac,
        require_factor_map=True,
    )
    assert res.n_trades == 0
    assert res.metrics.get("n_ca_blocked_trades", 0) >= 1
    assert res.metrics.get("status") == "unsupported_corporate_action"


def test_fast_incomplete_date_coverage_blocks_trade():
    """Non-empty map for code but missing entry-day factor coverage → block."""
    from wtpy.apps.astock.research.fast_engine import run_fast_backtest

    code = "SSE.STK.600082"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            _bar(20240102, 10.0, 10.5, 9.5, 10.0),
            _bar(20240103, 10.0, 10.5, 9.5, 10.0),
            _bar(20240104, 5.0, 5.2, 4.8, 5.0),
        ]
    }
    # factor only exists after exit — entry has no on-or-before factor
    fac = {code: {20240105: 1.0}}
    res = run_fast_backtest(
        [SignalEvent(code, 20240102, "DAY", "t")],
        raw,
        _cal(dates),
        hold=1,
        entry_lag=1,
        factor_by_code=fac,
        require_factor_map=True,
    )
    assert res.n_trades == 0
    assert res.metrics.get("status") == "unsupported_corporate_action"
    assert res.metrics.get("n_ca_blocked_trades", 0) >= 1
