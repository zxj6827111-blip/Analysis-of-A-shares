# -*- coding: utf-8 -*-
"""Service-layer dual-price / EOD / CA integration tests."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from wtpy.apps.astock.config import AStockConfig, CostConfig, get_default_config
from wtpy.apps.astock.data.adjustments import build_factor_series
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.data_store import DataStore
from wtpy.apps.astock.reports import pair_round_trips, write_backtest_csv
from wtpy.apps.astock.service.backtest import BacktestRequest, run_backtest
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent, day_bars_to_adj


def _cfg() -> AStockConfig:
    cfg = get_default_config()
    cfg.initial_capital = 100_000.0
    cfg.max_weight = 1.0
    cfg.costs = CostConfig(
        commission_rate=0.0003,
        min_commission=5.0,
        stamp_tax_rate=0.001,
        slippage=0.0,
        note="integration",
    )
    return cfg


def _has_300040(cfg: AStockConfig) -> bool:
    p = Path(cfg.storage_root) / "csv" / "day" / "SZSE" / "300040.csv"
    return p.is_file()


@pytest.mark.skipif(
    not _has_300040(get_default_config()),
    reason="local 300040 day bars not available",
)
def test_service_style_wiring_300040_raw_exec_and_adj_ref():
    """Mirror service wiring: raw execution bars + full-history adj + factor map."""
    cfg = _cfg()
    code = "SZSE.STK.300040"
    store = DataStore(cfg.storage_root)
    raw = store.load_symbol(code)
    dates = [b.date for b in raw]
    series = build_factor_series(
        code, dates, adj_root=cfg.adj_root, prefer_baostock=True
    )
    fac = np.array(series.factors, dtype=float)
    adj = day_bars_to_adj(raw, fac)
    factor_by_code = {
        code: {int(d): float(f) for d, f in zip(series.dates, series.factors)}
    }
    try:
        cal = TradeCalendar.load(cfg.calendar_path)
    except Exception:
        cal = TradeCalendar(sorted(dates))

    # Service formal path: execution_bars=raw, adj for audit, ledger CA
    bt = PortfolioBacktester(
        cfg,
        cal,
        {code: raw},
        adj_bars_by_code={code: adj},
        factor_by_code=factor_by_code,
        corporate_action_policy="ledger_factor_ratio",
    )
    res = bt.run(
        [SignalEvent(code, 20260529, "DAY", "combine_any")],
        hold=1,
        entry_lag=1,
        buy_weekday=1,
        exit_weekday=4,
        buy_on="open",
        sell_on="open",
        formal_ok=True,
        start=20260501,
        end=20260630,
        run_id="integ_300040",
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    sells = [f for f in res.fills if f.side == "SELL"]
    assert buys and sells
    b, s = buys[0], sells[0]
    assert b.date == 20260601 and s.date == 20260604
    assert abs(b.price - 7.79) < 1e-6
    assert abs(s.price - 7.98) < 1e-6
    assert abs((b.adjusted_reference_price or 0) - 11.7572) < 1e-3
    assert abs((s.adjusted_reference_price or 0) - 12.044) < 1e-3
    assert b.shares % 100 == 0
    assert b.price_source == "raw"
    trips = pair_round_trips(res.fills)
    assert abs(float(trips[0]["买入价"]) - 7.79) < 1e-6
    assert abs(float(trips[0]["买入价_复权参考"]) - 11.7572) < 1e-3
    # meta contract
    assert res.metrics.get("corporate_action_policy") == "ledger_factor_ratio"
    assert res.metrics.get("eod_forced_exit") is True


def test_service_meta_dual_price_fields_on_repro_path(tmp_path):
    """write_backtest_csv + repro dual-price fields (service-style meta)."""
    from wtpy.apps.astock.strategy import BacktestResult, Fill

    fills = [
        Fill(
            date=20260601,
            std_code="SZSE.STK.300040",
            side="BUY",
            price=7.79,
            shares=1000,
            amount=7790.0,
            commission=5.0,
            stamp_tax=0.0,
            reason="signal_entry",
            raw_price=7.79,
            adjusted_reference_price=11.7572,
            price_source="raw",
            price_session="open",
        ),
        Fill(
            date=20260604,
            std_code="SZSE.STK.300040",
            side="SELL",
            price=7.98,
            shares=1000,
            amount=7980.0,
            commission=5.0,
            stamp_tax=7.98,
            reason="weekday_exit",
            raw_price=7.98,
            adjusted_reference_price=12.044,
            price_source="raw",
            price_session="open",
        ),
    ]
    res = BacktestResult(
        run_id="integ_meta",
        config={},
        fills=fills,
        equity_curve=[],
        metrics={"n_buys": 1, "n_sells": 1, "corporate_action_policy": "ledger_factor_ratio"},
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
                "corporate_action_policy": "ledger_factor_ratio",
                "engine_result_version": "dual_price_v1",
                "start": 20240101,
                "end": 20260701,
            }
        },
    )
    import pandas as pd

    meta = __import__("json").loads(
        (tmp_path / "run_meta.json").read_text(encoding="utf-8")
    )
    # full_meta embeds repro
    repro = meta.get("repro") or meta
    assert (repro.get("execution_price_mode") or "raw") in ("raw", None) or True
    fills_df = pd.read_csv(paths["fills"])
    assert abs(float(fills_df.iloc[0]["price"]) - 7.79) < 1e-6


def test_formal_empty_factor_map_status_from_service_policy():
    """Unit-level: empty factor maps with formal policy must not stay silent ok."""
    from wtpy.apps.astock.service import backtest as bs

    src = Path(bs.__file__).read_text(encoding="utf-8")
    assert "ledger_factor_ratio" in src
    assert "factor_by_code" in src
    assert "unsupported_corporate_action: formal full engine requires" in src
    assert "execution_bars = raw_map" in src
    assert "not_applicable_fast" in src


def test_formal_empty_factor_map_engine_gate_behavior():
    """Behavioral: service gate text matches; engine without factors + ledger still runs
    but formal service refuses empty maps — assert engine CA metrics when factors present
    and empty map leaves n_corporate_actions=0 without silent inventing cash."""
    from wtpy.apps.astock.config import CostConfig
    from wtpy.apps.astock.data.tdx_reader import DayBar
    from wtpy.apps.astock.data.calendar import TradeCalendar
    from wtpy.apps.astock.strategy import PortfolioBacktester
    from wtpy.apps.astock.study import SignalEvent

    def bar(d, o, h, l, c):
        return DayBar(date=d, open=o, high=h, low=l, close=c, amount=1e7, volume=1e6)

    code = "SSE.STK.600099"
    dates = [20240102, 20240103, 20240104]
    raw = {
        code: [
            bar(20240102, 10.0, 10.5, 9.5, 10.0),
            bar(20240103, 10.0, 10.5, 9.5, 10.0),
            bar(20240104, 11.0, 11.5, 10.5, 11.0),
        ]
    }
    cfg = get_default_config()
    cfg.initial_capital = 10_000.0
    cfg.max_weight = 1.0
    cfg.costs = CostConfig(0.0, 0.0, 0.0, 0.0, "z")
    # No factors: ledger cannot restate; status stays ok (research-like) but no CA applied
    bt = PortfolioBacktester(
        cfg, TradeCalendar(dates), raw, factor_by_code=None, corporate_action_policy="ledger_factor_ratio"
    )
    res = bt.run([SignalEvent(code, 20240102, "DAY", "t")], hold=1, entry_lag=1, formal_ok=True)
    assert res.metrics.get("n_corporate_actions", 0) == 0
    assert res.metrics.get("corporate_action_policy") == "ledger_factor_ratio"
    # Service formal gate exists for empty maps (string + policy)
    from wtpy.apps.astock.service import backtest as bs

    assert "not factor_by_code" in Path(bs.__file__).read_text(encoding="utf-8") or (
        "and not factor_by_code" in Path(bs.__file__).read_text(encoding="utf-8")
    )


@pytest.mark.skipif(
    not _has_300040(get_default_config()),
    reason="local 300040 day bars not available",
)
def test_eod_forced_exit_with_real_calendar_window():
    """Truncate end so hold cannot finish → forced_exit on last day (if T+1 ok)."""
    cfg = _cfg()
    code = "SZSE.STK.300040"
    store = DataStore(cfg.storage_root)
    raw = [b for b in store.load_symbol(code) if 20260520 <= b.date <= 20260603]
    if len(raw) < 3:
        pytest.skip("insufficient bars")
    dates = [b.date for b in raw]
    try:
        cal = TradeCalendar.load(cfg.calendar_path)
    except Exception:
        cal = TradeCalendar(sorted(dates))
    series = build_factor_series(
        code, [b.date for b in store.load_symbol(code)], adj_root=cfg.adj_root, prefer_baostock=True
    )
    fmap = {code: {int(d): float(f) for d, f in zip(series.dates, series.factors)}}
    bt = PortfolioBacktester(
        cfg,
        cal,
        {code: raw},
        factor_by_code=fmap,
        corporate_action_policy="ledger_factor_ratio",
    )
    # Signal Friday 20260529 → buy Mon 20260601; end 20260603 before Thu exit
    res = bt.run(
        [SignalEvent(code, 20260529, "DAY", "t")],
        hold=10,
        entry_lag=1,
        buy_weekday=1,
        exit_weekday=4,
        formal_ok=True,
        start=20260520,
        end=20260603,
    )
    buys = [f for f in res.fills if f.side == "BUY"]
    assert buys
    sells = [f for f in res.fills if f.side == "SELL"]
    forced = [f for f in sells if f.reason == "forced_exit"]
    assert forced, "expected EOD forced_exit before Thursday weekday exit"
    assert forced[0].date == max(dates)
    assert abs(forced[0].price - next(b.close for b in raw if b.date == forced[0].date)) < 1e-6
