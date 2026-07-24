# -*- coding: utf-8 -*-
from pathlib import Path
import tempfile

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def test_fill_standard_qfq_and_pit_references_distinct():
    root = Path(tempfile.mkdtemp())
    cfg = AStockConfig(
        project_root=root,
        tdx_root=root,
        storage_root=root / 's',
        output_root=root / 'o',
        initial_capital=1_000_000,
        max_weight=1.0,
        lot_size=100,
    )
    cfg.costs = CostConfig(
        commission_rate=0, min_commission=0, stamp_tax_rate=0, slippage=0
    )
    dates = [20260529, 20260601, 20260602, 20260603, 20260604]
    cal = TradeCalendar(dates)
    code = 'SZSE.STK.300040'
    raw = [
        DayBar(20260529, 7.5, 7.6, 7.4, 7.55, 1, 1, 0),
        DayBar(20260601, 7.79, 7.9, 7.7, 7.8, 1, 1, 0),
        DayBar(20260602, 7.8, 7.9, 7.7, 7.85, 1, 1, 0),
        DayBar(20260603, 7.85, 7.95, 7.8, 7.9, 1, 1, 0),
        DayBar(20260604, 7.98, 8.0, 7.9, 7.95, 1, 1, 0),
    ]
    pit_s = 1.509274
    qfq_s = 1.1
    pit = [
        DayBar(
            b.date,
            round(b.open * pit_s, 4),
            round(b.high * pit_s, 4),
            round(b.low * pit_s, 4),
            round(b.close * pit_s, 4),
            b.amount,
            b.volume,
            0,
        )
        for b in raw
    ]
    qfq = [
        DayBar(
            b.date,
            round(b.open * qfq_s, 4),
            round(b.high * qfq_s, 4),
            round(b.low * qfq_s, 4),
            round(b.close * qfq_s, 4),
            b.amount,
            b.volume,
            0,
        )
        for b in raw
    ]
    fac = {d: 0.99688 for d in dates}
    bt = PortfolioBacktester(
        cfg,
        cal,
        {code: raw},
        adj_bars_by_code={code: pit},
        standard_qfq_bars_by_code={code: qfq},
        factor_by_code={code: fac},
    )
    res = bt.run(
        [SignalEvent(code, 20260529, 'DAY', 't')],
        hold=1,
        formal_ok=True,
        research_unadjusted=False,
        entry_lag=1,
        buy_on='open',
        sell_on='open',
    )
    buys = [f for f in res.fills if f.side == 'BUY']
    assert buys
    b = buys[0]
    assert abs(b.price - 7.79) < 1e-6
    assert abs(float(b.point_in_time_reference_price) - round(7.79 * pit_s, 4)) < 1e-3
    assert abs(float(b.standard_qfq_reference_price) - round(7.79 * qfq_s, 4)) < 1e-3
    assert float(b.standard_qfq_reference_price) != float(b.point_in_time_reference_price)
