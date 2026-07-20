"""Portfolio backtest engine for A-share multi-period signals.

Business rules (locked):
- Signal confirmed on period close; buy at open of the N-th trading day after signal (entry_lag, default 1).
- DAY hold=N: hold N trading days; DAY hold=1 exits on next session (T+1, cannot sell entry day).
- WEEK/MONTH hold=N: after N completed periods, exit on next tradable day.
- DWM hold=N: N trading days (same as day holds).
- Time-stop (hold expiry, no SL/TP trigger): sell at that day's **close** *(1-slippage), reason hold_expired.
- Risk exits (stop_loss / take_profit): still trigger on high/low, execute next tradable day **open** *(1-slippage).
- Repeat signals do not reset remaining hold.
- Buy px = open*(1+slippage).
- account_mode=portfolio: shared cash + max_weight.
- account_mode=per_symbol: each stock independent virtual capital (通达信对照).
- Suspended days: mark with last valid close.
- Limit-down untradeable sells are deferred.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import AStockConfig, CostConfig
from .data.calendar import TradeCalendar
from .data.limit_rules import (
    DefaultAShareLimitRule,
    LimitContext,
    LimitRuleProvider,
    infer_board,
)
from .data.periods import PeriodBar, aggregate_month, aggregate_week
from .data.tdx_reader import DayBar
from .study import SignalEvent


@dataclass
class Position:
    std_code: str
    shares: int
    entry_date: int
    entry_price: float
    # For DAY/DWM: remaining trading sessions after entry before exit is allowed
    hold_left_sessions: int
    # For WEEK/MONTH: number of full periods still required after entry period
    hold_left_periods: int
    period_mode: str  # DAY | WEEK | MONTH | DWM
    cost: float
    entry_period_key: Optional[Tuple] = None
    # Optional risk exits (fraction, e.g. 0.03 = 3%).
    # trigger_on_daily_high_low including entry day; execute_next_trading_day_open (T+1).
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trigger_reason: Optional[str] = None  # stop_loss | take_profit (sticky once set)
    defer_reason: Optional[str] = None  # suspended | limit_down | bad_price while waiting to sell


@dataclass
class Fill:
    date: int
    std_code: str
    side: str
    price: float
    shares: int
    amount: float
    commission: float
    stamp_tax: float
    reason: str


@dataclass
class EquityPoint:
    date: int
    cash: float
    market_value: float
    equity: float


@dataclass
class BacktestResult:
    run_id: str
    config: dict
    fills: List[Fill] = field(default_factory=list)
    equity_curve: List[EquityPoint] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    status: str = "ok"  # ok | research_unadjusted | no_go

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "config": self.config,
            "fills": [asdict(f) for f in self.fills],
            "equity_curve": [asdict(e) for e in self.equity_curve],
            "metrics": self.metrics,
            "notes": self.notes,
        }


def _commission(amount: float, costs: CostConfig) -> float:
    return max(amount * costs.commission_rate, costs.min_commission)


def validate_risk_pct(name: str, value: Optional[float]) -> Optional[float]:
    """Require None or strictly 0 < value < 1. Never clamp or abs()."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be a number, got {value!r}") from e
    if not (0.0 < v < 1.0):
        raise ValueError(f"{name} must satisfy 0 < value < 1, got {v}")
    return v


def compose_sell_reason(trigger: Optional[str], defer: Optional[str], fallback: str = "hold_expired") -> str:
    """Compose fill reason preserving original risk trigger across deferrals."""
    if trigger and defer:
        return f"{trigger}_deferred_{defer}"
    if trigger:
        return trigger
    if defer in ("suspended", "limit_down", "bad_price"):
        return f"{fallback}_deferred_{defer}"
    return fallback


def _week_key(d: int) -> Tuple[int, int]:
    from datetime import date

    y, m, day = d // 10000, (d // 100) % 100, d % 100
    iso = date(y, m, day).isocalendar()
    return int(iso[0]), int(iso[1])


def _month_key(d: int) -> Tuple[int, int]:
    return d // 10000, (d // 100) % 100


class PortfolioBacktester:
    def __init__(
        self,
        cfg: AStockConfig,
        calendar: TradeCalendar,
        bars_by_code: Dict[str, Sequence[DayBar]],
        *,
        limit_rules: Optional[LimitRuleProvider] = None,
        adj_bars_by_code: Optional[Dict[str, Sequence[DayBar]]] = None,
    ):
        self.cfg = cfg
        self.calendar = calendar
        # valuation / trading uses adj bars when provided else raw
        trade_bars = adj_bars_by_code if adj_bars_by_code is not None else bars_by_code
        self.bars_by_code = trade_bars
        self.raw_bars_by_code = bars_by_code
        self.limit_rules = limit_rules or DefaultAShareLimitRule()
        self._index: Dict[str, Dict[int, DayBar]] = {}
        self._sorted_dates: Dict[str, List[int]] = {}
        self._last_close: Dict[str, Dict[int, float]] = {}
        for code, bars in trade_bars.items():
            idx = {b.date: b for b in bars}
            self._index[code] = idx
            dates = [b.date for b in bars]
            self._sorted_dates[code] = dates
            last = None
            lc: Dict[int, float] = {}
            # map every calendar trading day? only bar dates; valuation uses walk
            for b in bars:
                last = b.close
                lc[b.date] = last
            self._last_close[code] = lc

        # prev close map for limit rules
        self._prev_close: Dict[str, Dict[int, float]] = {}
        for code, bars in trade_bars.items():
            pc: Dict[int, float] = {}
            prev = None
            for b in bars:
                if prev is not None:
                    pc[b.date] = prev
                prev = b.close
            self._prev_close[code] = pc

    def run(
        self,
        events: Sequence[SignalEvent],
        *,
        hold: int = 1,
        period: str = "DAY",
        run_id: str = "bt",
        start: Optional[int] = None,
        end: Optional[int] = None,
        research_unadjusted: bool = False,
        formal_ok: bool = True,
        _skip_zero_replay: bool = False,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        entry_lag: int = 1,
        account_mode: str = "portfolio",
    ) -> BacktestResult:
        stop_loss_pct = validate_risk_pct("stop_loss_pct", stop_loss_pct)
        take_profit_pct = validate_risk_pct("take_profit_pct", take_profit_pct)
        entry_lag = int(entry_lag)
        if entry_lag < 1:
            raise ValueError("entry_lag must be >= 1, got %s" % entry_lag)
        account_mode = (account_mode or "portfolio").strip().lower()
        if account_mode in ("tdx", "per_stock", "independent", "通达信", "单票"):
            account_mode = "per_symbol"
        if account_mode not in ("portfolio", "per_symbol"):
            raise ValueError(
                "account_mode must be portfolio or per_symbol, got %s" % account_mode
            )
        period = period.upper()
        notes = [
            "Example costs only; not user real trading costs.",
            "Survivor bias possible: local TDX may lack delisted stocks.",
            DefaultAShareLimitRule.BOUNDARY_NOTE,
            "Buy at open of the N-th trading day after signal close (entry_lag=%d)." % entry_lag,
            (
                "Account mode: per_symbol (通达信对照) — each stock has its own virtual capital; "
                "no cross-stock cash competition; metrics include equal-weight mean stock return."
                if account_mode == "per_symbol"
                else "Account mode: portfolio — single shared cash account with max_weight cap."
            ),
            "Time-stop: after hold periods, force flat at that day close (hold_expired), regardless of P&L.",
            "Risk: trigger_on_daily_high_low (incl. entry day); execute_next_trading_day_open (T+1).",
            "Risk conflict policy: stop_first when same bar hits both SL and TP.",
        ]
        status = "ok"
        if research_unadjusted:
            status = "research_unadjusted"
            notes.append("RESEARCH_UNADJUSTED: results use raw prices; not formal.")
        if not formal_ok and not research_unadjusted:
            return BacktestResult(
                run_id=run_id,
                config={"hold": hold, "period": period, "entry_lag": entry_lag},
                status="no_go",
                notes=notes
                + ["NO-GO: adjustment factors unavailable; refuse formal backtest."],
                metrics={},
            )

        # filter events to range (signal date)
        sig_map: Dict[int, List[str]] = {}
        for ev in events:
            if start and ev.date < start:
                continue
            if end and ev.date > end:
                continue
            sig_map.setdefault(ev.date, []).append(ev.std_code)

        dates = list(self.calendar.dates)
        if start:
            # include from day before start for context? keep full for holds, filter equity later
            pass
        # trading dates window: from min signal next day to end
        pending_buys: Dict[int, List[str]] = {}
        for sig_date, codes in sig_map.items():
            entry = self.calendar.nth_trading_day_after(sig_date, entry_lag)
            if entry is None:
                continue
            if start and entry < start:
                # still allow if signal before start but entry in range? skip entries before start
                continue
            if end and entry > end:
                continue
            pending_buys.setdefault(entry, [])
            for c in codes:
                if c not in pending_buys[entry]:
                    pending_buys[entry].append(c)

        # determine simulation date range
        sim_dates = dates
        if start:
            sim_dates = [d for d in sim_dates if d >= start]
        if end:
            sim_dates = [d for d in sim_dates if d <= end]

        # portfolio: one shared cash; per_symbol: each code has its own book (TDX-style).
        cash = float(self.cfg.initial_capital)
        cash_by_code: Dict[str, float] = {}
        positions: Dict[str, Position] = {}
        fills: List[Fill] = []
        equity_curve: List[EquityPoint] = []
        # deferred_sells[code] = {"trigger": optional risk reason, "defer": last defer reason}
        deferred_sells: Dict[str, dict] = {}

        # precompute period end sets for week/month completion tracking
        week_ends = self._period_end_dates("WEEK")
        month_ends = self._period_end_dates("MONTH")

        for d in sim_dates:
            # 1) process deferred + matured sells at open (never same-day as BUY)
            sell_codes = set(deferred_sells.keys())
            for code, pos in list(positions.items()):
                if pos.trigger_reason:
                    sell_codes.add(code)
                    deferred_sells.setdefault(
                        code, {"trigger": pos.trigger_reason, "defer": pos.defer_reason}
                    )
                    deferred_sells[code]["trigger"] = (
                        deferred_sells[code].get("trigger") or pos.trigger_reason
                    )
                if self._is_exit_due(pos, d, week_ends, month_ends):
                    sell_codes.add(code)
                    deferred_sells.setdefault(
                        code, {"trigger": pos.trigger_reason, "defer": None}
                    )
            for code in list(sell_codes):
                if code not in positions:
                    deferred_sells.pop(code, None)
                    continue
                pos = positions[code]
                # T+1: never sell on entry date
                if d <= pos.entry_date:
                    continue
                bar = self._index.get(code, {}).get(d)
                info = deferred_sells.get(code) or {
                    "trigger": pos.trigger_reason,
                    "defer": None,
                }
                trigger = info.get("trigger") or pos.trigger_reason
                if not bar:
                    pos.defer_reason = "suspended"
                    deferred_sells[code] = {"trigger": trigger, "defer": "suspended"}
                    continue
                prev_c = self._prev_close.get(code, {}).get(d, bar.open)
                ctx = LimitContext(
                    std_code=code,
                    date=d,
                    prev_close=prev_c,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    board=infer_board(code),
                )
                if self.limit_rules.is_limit_down_untradeable(ctx):
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
                if account_mode == "per_symbol":
                    cash_by_code[code] = float(cash_by_code.get(code, 0.0)) + amount - comm - tax
                else:
                    cash += amount - comm - tax
                info = deferred_sells.pop(code, {}) or {}
                reason = compose_sell_reason(
                    info.get("trigger") or pos.trigger_reason,
                    info.get("defer") or pos.defer_reason,
                    fallback="hold_expired",
                )
                fills.append(
                    Fill(
                        date=d,
                        std_code=code,
                        side="SELL",
                        price=px,
                        shares=pos.shares,
                        amount=amount,
                        commission=comm,
                        stamp_tax=tax,
                        reason=reason,
                    )
                )

            # 2) entries at open
            for code in pending_buys.get(d, []):
                if code in positions:
                    # do not reset hold
                    continue
                bar = self._index.get(code, {}).get(d)
                if not bar or bar.open <= 0:
                    continue
                prev_c = self._prev_close.get(code, {}).get(d, bar.open)
                ctx = LimitContext(
                    std_code=code,
                    date=d,
                    prev_close=prev_c,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    board=infer_board(code),
                )
                if self.limit_rules.is_limit_up_unbuyable(ctx):
                    continue
                if account_mode == "per_symbol":
                    # Lazy open a virtual account for this symbol (TDX-style independent capital).
                    if code not in cash_by_code:
                        cash_by_code[code] = float(self.cfg.initial_capital)
                    book = float(cash_by_code[code])
                    if book <= 0:
                        continue
                    equity = book
                    # Full virtual capital (no cross-stock max_weight competition).
                    target_value = book
                    alloc = target_value
                    avail_cash = book
                else:
                    equity = cash + self._mkt_value(positions, d)
                    if equity <= 0:
                        continue
                    target_value = equity * self.cfg.max_weight
                    n_new = max(1, len(pending_buys.get(d, [])))
                    alloc = min(target_value, equity / n_new)
                    avail_cash = cash
                px = bar.open * (1.0 + self.cfg.costs.slippage)
                shares = int(alloc // (px * self.cfg.lot_size)) * self.cfg.lot_size
                if shares < self.cfg.lot_size:
                    continue
                amount = shares * px
                comm = _commission(amount, self.cfg.costs)
                if amount + comm > avail_cash:
                    shares = (
                        int((avail_cash - self.cfg.costs.min_commission) // (px * self.cfg.lot_size))
                        * self.cfg.lot_size
                    )
                    if shares < self.cfg.lot_size:
                        continue
                    amount = shares * px
                    comm = _commission(amount, self.cfg.costs)
                if account_mode == "per_symbol":
                    cash_by_code[code] = float(cash_by_code.get(code, 0.0)) - amount - comm
                else:
                    cash -= amount + comm
                if period in ("DAY", "DWM"):
                    # hold=1 means sell next trading day open => hold_left_sessions=1
                    # After entry day ends we will decrement; exit when hold_left<=0 at next open
                    h_sess = int(hold)
                    h_per = 0
                    pkey = None
                elif period == "WEEK":
                    h_sess = 0
                    h_per = int(hold)
                    pkey = _week_key(d)
                elif period == "MONTH":
                    h_sess = 0
                    h_per = int(hold)
                    pkey = _month_key(d)
                else:
                    h_sess = int(hold)
                    h_per = 0
                    pkey = None
                positions[code] = Position(
                    std_code=code,
                    shares=shares,
                    entry_date=d,
                    entry_price=px,
                    hold_left_sessions=h_sess,
                    hold_left_periods=h_per,
                    period_mode=period,
                    cost=amount + comm,
                    entry_period_key=pkey,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    trigger_reason=None,
                    defer_reason=None,
                )
                fills.append(
                    Fill(
                        date=d,
                        std_code=code,
                        side="BUY",
                        price=px,
                        shares=shares,
                        amount=amount,
                        commission=comm,
                        stamp_tax=0.0,
                        reason="signal_entry",
                    )
                )

            # 3) end of day: risk marks (incl. entry day high/low) then decrement holds
            # Policy: risk_conflict_policy=stop_first; trigger sticky; sell next open (T+1).
            for pos in positions.values():
                if d >= pos.entry_date and not pos.trigger_reason:
                    bar = self._index.get(pos.std_code, {}).get(d)
                    if bar and pos.entry_price > 0:
                        hit_sl = False
                        hit_tp = False
                        if pos.stop_loss_pct is not None:
                            if bar.low <= pos.entry_price * (1.0 - float(pos.stop_loss_pct)):
                                hit_sl = True
                        if pos.take_profit_pct is not None:
                            if bar.high >= pos.entry_price * (1.0 + float(pos.take_profit_pct)):
                                hit_tp = True
                        if hit_sl and hit_tp:
                            pos.trigger_reason = "stop_loss"  # stop_first
                        elif hit_sl:
                            pos.trigger_reason = "stop_loss"
                        elif hit_tp:
                            pos.trigger_reason = "take_profit"
                if pos.period_mode in ("DAY", "DWM"):
                    # Count trading days held including entry day so hold=1 exits next open (T+1).
                    pos.hold_left_sessions -= 1
                elif pos.period_mode == "WEEK":
                    # Complete periods: count a week-end on/after entry day
                    if d in week_ends and d >= pos.entry_date:
                        pos.hold_left_periods -= 1
                elif pos.period_mode == "MONTH":
                    if d in month_ends and d >= pos.entry_date:
                        pos.hold_left_periods -= 1

            mv = self._mkt_value(positions, d)
            if account_mode == "per_symbol":
                # Sum only opened virtual books (+ idle cash of codes that traded / were funded).
                cash_sum = float(sum(cash_by_code.values())) if cash_by_code else 0.0
                # Positions already reflected: cash reduced at buy; MV from holdings.
                equity_curve.append(
                    EquityPoint(date=d, cash=cash_sum, market_value=mv, equity=cash_sum + mv)
                )
            else:
                equity_curve.append(
                    EquityPoint(date=d, cash=cash, market_value=mv, equity=cash + mv)
                )

        if account_mode == "per_symbol":
            n_books = max(1, len(cash_by_code))
            capital_base = float(self.cfg.initial_capital) * float(n_books)
        else:
            capital_base = float(self.cfg.initial_capital)
        metrics = compute_metrics(
            equity_curve, capital_base, fills, self.cfg.costs
        )
        metrics["account_mode"] = account_mode
        metrics["capital_base"] = capital_base
        # open positions at end
        open_n = len(positions)
        open_mv = self._mkt_value(positions, sim_dates[-1]) if sim_dates and positions else 0.0
        metrics["n_open_positions"] = open_n
        metrics["open_market_value"] = float(open_mv)
        if account_mode == "per_symbol":
            # Equal-weight mean of per-stock total returns (TDX-style summary orientation).
            last_d = sim_dates[-1] if sim_dates else None
            stock_rets = []
            for code, book0 in list(cash_by_code.items()):
                end_cash = float(cash_by_code.get(code, 0.0))
                pos = positions.get(code)
                end_mv = 0.0
                if pos and last_d is not None:
                    bar = self._index.get(code, {}).get(last_d)
                    px = bar.close if bar else (self._last_px_on_or_before(code, last_d) or pos.entry_price)
                    end_mv = float(pos.shares) * float(px)
                end_eq = end_cash + end_mv
                stock_rets.append((end_eq / float(self.cfg.initial_capital)) - 1.0)
            if stock_rets:
                metrics["n_symbol_accounts"] = len(stock_rets)
                metrics["mean_symbol_return"] = float(sum(stock_rets) / len(stock_rets))
                srt = sorted(stock_rets)
                mid = len(srt) // 2
                metrics["median_symbol_return"] = float(
                    srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2.0
                )
                metrics["pct_symbols_profitable"] = float(
                    sum(1 for r in stock_rets if r > 0) / len(stock_rets)
                )
            else:
                metrics["n_symbol_accounts"] = 0
                metrics["mean_symbol_return"] = None
                metrics["median_symbol_return"] = None
                metrics["pct_symbols_profitable"] = None
        from collections import Counter
        sell_reasons = Counter(f.reason for f in fills if f.side == "SELL")
        metrics["sell_reason_counts"] = dict(sell_reasons)
        if not _skip_zero_replay:
            try:
                zero_cfg = AStockConfig(
                    project_root=self.cfg.project_root,
                    tdx_root=self.cfg.tdx_root,
                    storage_root=self.cfg.storage_root,
                    output_root=self.cfg.output_root,
                    initial_capital=self.cfg.initial_capital,
                    max_weight=self.cfg.max_weight,
                    lot_size=self.cfg.lot_size,
                )
                zero_cfg.costs = CostConfig(
                    commission_rate=0.0,
                    min_commission=0.0,
                    stamp_tax_rate=0.0,
                    slippage=0.0,
                    note="zero-cost control replay",
                )
                z_bt = PortfolioBacktester(
                    zero_cfg,
                    self.calendar,
                    self.raw_bars_by_code,
                    adj_bars_by_code=self.bars_by_code,
                    limit_rules=self.limit_rules,
                )
                z_res = z_bt.run(
                    events,
                    hold=hold,
                    period=period,
                    run_id=run_id + "_zerocost",
                    start=start,
                    end=end,
                    research_unadjusted=research_unadjusted,
                    formal_ok=True,
                    _skip_zero_replay=True,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    entry_lag=entry_lag,
                    account_mode=account_mode,
                )
                z_ret = z_res.metrics.get("total_return")
                metrics["zero_cost_return"] = z_ret
                metrics["zero_cost_final_equity"] = z_res.metrics.get("final_equity")
                metrics["zero_cost_n_buys"] = z_res.metrics.get("n_buys")
                metrics["zero_cost_n_sells"] = z_res.metrics.get("n_sells")
                metrics["cost_impact"] = (
                    None if z_ret is None else float(z_ret) - float(metrics.get("total_return") or 0.0)
                )
                metrics["control_method"] = "full_replay"
            except Exception as e:  # noqa: BLE001
                metrics["control_method"] = "full_replay_failed"
                metrics["zero_cost_error"] = str(e)
                notes.append(f"zero-cost full replay failed: {e}")

        return BacktestResult(
            run_id=run_id,
            config={
                "initial_capital": self.cfg.initial_capital,
                "max_weight": self.cfg.max_weight,
                "hold": hold,
                "period": period,
                "entry_lag": entry_lag,
                "account_mode": account_mode,
                "costs": asdict(self.cfg.costs),
                "start": start,
                "end": end,
                "n_events": len([e for e in events if (not start or e.date >= start) and (not end or e.date <= end)]),
                "research_unadjusted": research_unadjusted,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "risk_conflict_policy": "stop_first",
                "risk_trigger_policy": "daily_high_low",
                "risk_execution_policy": "next_trading_day_open",
            },
            fills=fills,
            equity_curve=equity_curve,
            metrics=metrics,
            notes=notes,
            status=status,
        )


    def _is_exit_due(
        self,
        pos: Position,
        date: int,
        week_ends: set,
        month_ends: set,
    ) -> bool:
        if date <= pos.entry_date:
            return False
        if pos.period_mode in ("DAY", "DWM"):
            return pos.hold_left_sessions <= 0
        if pos.period_mode in ("WEEK", "MONTH"):
            return pos.hold_left_periods <= 0
        return pos.hold_left_sessions <= 0

    def _period_end_dates(self, period: str) -> set:
        """Union of closed period end dates across all symbols (approx via calendar weeks/months)."""
        ends = set()
        # use calendar dates to build synthetic bars then aggregate? simpler: last trading day of each iso week/month in calendar
        dates = self.calendar.dates
        if not dates:
            return ends
        if period == "WEEK":
            last_by = {}
            for d in dates:
                last_by[_week_key(d)] = d
            ends = set(last_by.values())
        else:
            last_by = {}
            for d in dates:
                last_by[_month_key(d)] = d
            ends = set(last_by.values())
        return ends

    def _mkt_value(self, positions: Dict[str, Position], date: int) -> float:
        total = 0.0
        for code, p in positions.items():
            bar = self._index.get(code, {}).get(date)
            if bar:
                px = bar.close
            else:
                # last valid close <= date
                px = self._last_px_on_or_before(code, date) or p.entry_price
            total += p.shares * px
        return total

    def _last_px_on_or_before(self, code: str, date: int) -> Optional[float]:
        dates = self._sorted_dates.get(code) or []
        # binary search
        lo, hi = 0, len(dates) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if dates[mid] <= date:
                best = dates[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            return None
        return self._index[code][best].close


def compute_metrics(
    curve: Sequence[EquityPoint],
    init: float,
    fills: Sequence[Fill],
    costs: CostConfig,
) -> dict:
    if not curve:
        return {}
    eq = np.array([e.equity for e in curve], dtype=np.float64)
    if len(eq) > 1:
        denom = np.where(eq[:-1] == 0, np.nan, eq[:-1])
        rets = np.diff(eq) / denom
        rets = rets[np.isfinite(rets)]
    else:
        rets = np.array([])
    total_return = eq[-1] / init - 1.0 if init else 0.0
    n_days = len(eq)
    ann_factor = 242.0
    ann_return = (1 + total_return) ** (ann_factor / max(n_days, 1)) - 1 if n_days else 0.0
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.where(peak == 0, 1, peak)
    max_dd = float(dd.min()) if len(dd) else 0.0
    vol = float(np.std(rets) * np.sqrt(ann_factor)) if len(rets) else 0.0
    sharpe = float((np.mean(rets) * ann_factor) / vol) if vol > 1e-12 else 0.0

    buys = [f for f in fills if f.side == "BUY"]
    sells = [f for f in fills if f.side == "SELL"]
    n_buys = len(buys)
    n_sells = len(sells)
    n_round = min(n_buys, n_sells)
    # pair FIFO for win rate
    from collections import defaultdict, deque

    lots: Dict[str, deque] = defaultdict(deque)
    wins = 0
    closed = 0
    for f in fills:
        if f.side == "BUY":
            lots[f.std_code].append(f)
        else:
            if lots[f.std_code]:
                b = lots[f.std_code].popleft()
                pnl = (f.price - b.price) * f.shares - f.commission - f.stamp_tax - b.commission
                closed += 1
                if pnl > 0:
                    wins += 1
    win_rate = wins / closed if closed else 0.0
    cost_total = sum(f.commission + f.stamp_tax for f in fills)
    # slippage cost is embedded in fill prices; track explicit fees only here
    turnover = sum(f.amount for f in fills) / init if init else 0.0

    return {
        "total_return": float(total_return),
        "annual_return": float(ann_return),
        "max_drawdown": max_dd,
        "volatility": vol,
        "sharpe": sharpe,
        "final_equity": float(eq[-1]),
        "n_days": n_days,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "n_round_trips": closed,
        "win_rate": float(win_rate),
        "turnover": float(turnover),
        "cost_total": float(cost_total),
    }
