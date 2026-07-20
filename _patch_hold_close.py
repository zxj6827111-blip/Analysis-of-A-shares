# -*- coding: utf-8 -*-
"""Make time-stop (hold) exits use same-day close; clarify UI labels."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def patch_strategy() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "strategy.py"
    t = p.read_text(encoding="utf-8")

    old_doc = """Business rules (locked):
- Signal confirmed on period close; buy at open of the N-th trading day after signal (entry_lag, default 1).
- DAY hold=N: hold N trading days; DAY hold=1 sells next session open (T+1).
- WEEK/MONTH hold=N: after N completed periods, sell next tradable day open.
- DWM hold=N: N trading days (same as day holds).
- Repeat signals do not reset remaining hold.
- Buy px = open*(1+slippage); sell px = open*(1-slippage).
- Suspended days: mark with last valid close.
- Limit-down untradeable sells are deferred.
"""
    new_doc = """Business rules (locked):
- Signal confirmed on period close; buy at open of the N-th trading day after signal (entry_lag, default 1).
- DAY hold=N: hold N trading days; DAY hold=1 exits on next session (T+1, cannot sell entry day).
- WEEK/MONTH hold=N: after N completed periods, exit on next tradable day.
- DWM hold=N: N trading days (same as day holds).
- Time-stop (hold expiry, no SL/TP trigger): sell at that day's **close** *(1-slippage), reason hold_expired.
- Risk exits (stop_loss / take_profit): still trigger on high/low, execute next tradable day **open** *(1-slippage).
- Repeat signals do not reset remaining hold.
- Buy px = open*(1+slippage).
- Suspended days: mark with last valid close.
- Limit-down untradeable sells are deferred.
"""
    if old_doc not in t:
        raise SystemExit("strategy docstring block not found")
    t = t.replace(old_doc, new_doc, 1)

    old_note = (
        '            "Buy at open of the N-th trading day after signal close (entry_lag=%d)." % entry_lag,\n'
        '            "Risk: trigger_on_daily_high_low (incl. entry day); execute_next_trading_day_open (T+1).",\n'
        '            "Risk conflict policy: stop_first when same bar hits both SL and TP.",\n'
    )
    new_note = (
        '            "Buy at open of the N-th trading day after signal close (entry_lag=%d)." % entry_lag,\n'
        '            "Time-stop: after hold periods, force flat at that day close (hold_expired), regardless of P&L.",\n'
        '            "Risk: trigger_on_daily_high_low (incl. entry day); execute_next_trading_day_open (T+1).",\n'
        '            "Risk conflict policy: stop_first when same bar hits both SL and TP.",\n'
    )
    if "Time-stop: after hold periods" not in t:
        if old_note not in t:
            raise SystemExit("notes block not found")
        t = t.replace(old_note, new_note, 1)

    old_px = """                if self.limit_rules.is_limit_down_untradeable(ctx):
                    pos.defer_reason = "limit_down"
                    deferred_sells[code] = {"trigger": trigger, "defer": "limit_down"}
                    continue
                px = bar.open * (1.0 - self.cfg.costs.slippage)
                if px <= 0:
                    pos.defer_reason = "bad_price"
                    deferred_sells[code] = {"trigger": trigger, "defer": "bad_price"}
                    continue
                pos = positions.pop(code)
                amount = pos.shares * px
                comm = _commission(amount, self.cfg.costs)
                tax = amount * self.cfg.costs.stamp_tax_rate
                cash += amount - comm - tax
                info = deferred_sells.pop(code, {}) or {}
                reason = compose_sell_reason(
                    info.get("trigger") or pos.trigger_reason,
                    info.get("defer") or pos.defer_reason,
                    fallback="hold_expired",
                )
"""
    new_px = """                if self.limit_rules.is_limit_down_untradeable(ctx):
                    pos.defer_reason = "limit_down"
                    deferred_sells[code] = {"trigger": trigger, "defer": "limit_down"}
                    continue
                # Time-stop (no risk trigger): force flat at **close**.
                # Stop-loss / take-profit: keep next-day **open** execution.
                use_close = not trigger
                raw_px = float(bar.close if use_close else bar.open)
                px = raw_px * (1.0 - self.cfg.costs.slippage)
                if px <= 0:
                    pos.defer_reason = "bad_price"
                    deferred_sells[code] = {"trigger": trigger, "defer": "bad_price"}
                    continue
                pos = positions.pop(code)
                amount = pos.shares * px
                comm = _commission(amount, self.cfg.costs)
                tax = amount * self.cfg.costs.stamp_tax_rate
                cash += amount - comm - tax
                info = deferred_sells.pop(code, {}) or {}
                reason = compose_sell_reason(
                    info.get("trigger") or pos.trigger_reason,
                    info.get("defer") or pos.defer_reason,
                    fallback="hold_expired",
                )
"""
    if "use_close = not trigger" not in t:
        if old_px not in t:
            raise SystemExit("sell price block not found")
        t = t.replace(old_px, new_px, 1)

    p.write_text(t, encoding="utf-8")
    print("OK strategy.py")


def patch_ui() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index.html"
    t = p.read_text(encoding="utf-8")

    old = """          <div>
            <label>持有期（买入后持有 N 期）</label>
            <input type="number" id="hold" min="1" value="1" />
          </div>
"""
    new = """          <div>
            <label>强制平仓周期 hold（开仓后经过 N 个周期，无论涨跌都平仓；平仓价=该日收盘价）</label>
            <input type="number" id="hold" min="1" value="1" title="日线=N个交易日；周/月线=N个完整周期。A股T+1：不能开仓当日卖出。到期强制平仓用收盘价；止损/止盈仍为次日开盘。" />
            <div class="hint" style="font-size:12px;opacity:.8;margin-top:4px">
              例：日线 hold=5 → 买入后第 5 个交易日计数满，在可卖日按<strong>收盘价</strong>强制平仓（未触发止损/止盈时）。
            </div>
          </div>
"""
    if "强制平仓周期 hold" not in t:
        if old not in t:
            # looser match
            if 'id="hold"' not in t:
                raise SystemExit("hold input not found")
            t = t.replace(
                '<label>持有期（买入后持有 N 期）</label>',
                '<label>强制平仓周期 hold（开仓后经过 N 个周期，无论涨跌都平仓；平仓价=该日收盘价）</label>',
                1,
            )
        else:
            t = t.replace(old, new, 1)
        p.write_text(t, encoding="utf-8")
        print("OK index.html")
    else:
        print("UI already labeled")


def patch_reports_notes() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "reports.py"
    t = p.read_text(encoding="utf-8")
    line = '        "收益率分母为买入金额。卖出原因含 hold_expired / stop_loss / take_profit 等。",\n'
    add = (
        '        "收益率分母为买入金额。卖出原因含 hold_expired / stop_loss / take_profit 等。",\n'
        '        "hold_expired：持有期满强制平仓，成交价为平仓日收盘价；止损/止盈仍为触发后下一可交易日开盘价。",\n'
    )
    if "持有期满强制平仓" not in t:
        if line not in t:
            print("reports note line skip")
        else:
            t = t.replace(line, add, 1)
            p.write_text(t, encoding="utf-8")
            print("OK reports.py notes")
    else:
        print("reports already")


def add_test() -> None:
    p = ROOT / "tests" / "apps" / "astock" / "test_hold_close_exit.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '''# -*- coding: utf-8 -*-
"""Time-stop (hold) exits at close; risk exits still at open."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, CostConfig
from wtpy.apps.astock.data.calendar import TradeCalendar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.strategy import PortfolioBacktester
from wtpy.apps.astock.study import SignalEvent


def _cfg():
    cfg = AStockConfig()
    cfg.initial_capital = 1_000_000
    cfg.max_weight = 1.0
    cfg.lot_size = 100
    cfg.costs = CostConfig(0, 0, 0, 0)
    return cfg


def test_hold1_sells_at_close_not_open():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10.0, 1, 1000),  # signal
            DayBar(20240104, 10.0, 11.0, 9.9, 10.5, 1, 1000),  # buy open 10
            DayBar(20240105, 10.8, 11.2, 10.5, 11.0, 1, 1000),  # hold exit: close 11 not open 10.8
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=1,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240105
    assert abs(sells[0].price - 11.0) < 1e-9
    assert sells[0].reason == "hold_expired"


def test_stop_loss_still_sells_at_open():
    code = "SSE.STK.600000"
    dates = [20240103, 20240104, 20240105]
    bars = {
        code: [
            DayBar(20240103, 10, 10.5, 9.8, 10, 1, 1000),
            DayBar(20240104, 10, 10.2, 9.0, 9.5, 1, 1000),  # buy 10; low hits 3%
            DayBar(20240105, 9.2, 9.5, 9.0, 9.3, 1, 1000),  # sell open 9.2
        ]
    }
    res = PortfolioBacktester(_cfg(), TradeCalendar(dates), bars).run(
        [SignalEvent(code, 20240103, "DAY", "t")],
        hold=10,
        period="DAY",
        formal_ok=True,
        _skip_zero_replay=True,
        stop_loss_pct=0.03,
    )
    sells = [f for f in res.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == 20240105
    assert abs(sells[0].price - 9.2) < 1e-9
    assert "stop" in (sells[0].reason or "")
''',
        encoding="utf-8",
    )
    print("OK test_hold_close_exit.py")


def main() -> None:
    patch_strategy()
    patch_ui()
    patch_reports_notes()
    add_test()
    print("done")


if __name__ == "__main__":
    main()
