"""Tests for 735 explicit formula pairing and balanced risk exits."""

from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

import numpy as np
import pytest

from wtpy.apps.astock.config import AStockConfig, CostConfig, get_default_config
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.indicators.compiler import compile_formula
from wtpy.apps.astock.indicators.registry import IndicatorRegistry
from wtpy.apps.astock.indicators.runtime import run_formula
from wtpy.apps.astock.indicators.tn6_importer import import_tn6_with_source
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


FORMULA = """
MA7:=MA(C,7);
MA35:=MA(C,35);
DEV:=(MA7-MA35)/MA35*100;
UP7:=MA7>REF(MA7,1);
UP35:=MA35>REF(MA35,1);
TJ1:=CROSS(MA7,MA35) AND UP7 AND UP35;
TJ2:=MA7>MA35 AND UP7 AND UP35 AND DEV<=2;
XG:TJ1 OR TJ2;
"""


def test_735_formula_compiles_and_runs():
    cr = compile_formula(FORMULA, indicator_id="735")
    assert cr.ok, cr.error
    n = 80
    close = np.linspace(10, 20, n) + np.sin(np.linspace(0, 6, n))
    bars = {
        "close": close,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "volume": np.ones(n) * 1000,
    }
    res = run_formula(FORMULA, bars, indicator_id="735")
    assert res.error is None
    assert res.signal is not None
    assert len(res.signal) == n


def test_pair_735_source_makes_ready(tmp_path):
    cfg = get_default_config()
    ind = Path(cfg.indicator_dir)
    tn6 = list(ind.glob("*735*.tn6"))
    src = ind / "735金叉及趋势.txt"
    if not tn6 or not src.exists():
        pytest.skip("735 files missing")
    map_path = tmp_path / "map.json"
    mapping, spec = import_tn6_with_source(
        tn6[0],
        src,
        map_path,
        note="explicit human formula",
    )
    assert mapping["source_sha256"]
    assert spec.compile_status == "ready"
    assert spec.backtestable
    assert "MIN60" not in (spec.dependencies or [])


def test_stop_loss_triggers_early_exit():
    dates = [20240102, 20240103, 20240104, 20240105, 20240108, 20240109]
    code = "SSE.STK.600000"
    # buy day open 10; next day low 9.5 -> -5% vs entry if entry~10
    bars = {
        code: [
            DayBar(20240102, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240103, 10, 10.2, 9.9, 10, 1, 1000),  # signal
            DayBar(20240104, 10, 10.1, 9.9, 10, 1, 1000),  # buy
            DayBar(20240105, 9.8, 9.9, 9.5, 9.6, 1, 1000),  # stop hit low
            DayBar(20240108, 9.6, 9.7, 9.5, 9.6, 1, 1000),  # sell open
            DayBar(20240109, 9.6, 9.8, 9.5, 9.7, 1, 1000),
        ]
    }
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    bt = PortfolioBacktester(cfg, TradeCalendar(dates), bars)
    res = bt.run(
        [SignalEvent(code, 20240103, "DAY", "735")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
        take_profit_pct=0.08,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert sells
    # should sell before hold=10 exhausted — on or before 20240109
    assert sells[0].date <= 20240109
    assert sells[0].reason == "stop_loss", sells[0].reason
