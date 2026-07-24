# -*- coding: utf-8 -*-
"""PortfolioBacktester engine (day loop, fills, equity)."""

from __future__ import annotations

from datetime import date as _date
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

from dataclasses import asdict

from .config import AStockConfig, CostConfig
from .corporate_action import (
    check_open_hold_factor_change,
    factor_on_or_before,
    normalize_corporate_action_policy,
    risk_exit_session,
)
from .data.calendar import (
    DEFAULT_HOLIDAY_POLICY,
    TradeCalendar,
    normalize_holiday_policy,
)
from .data.limit_rules import (
    DefaultAShareLimitRule,
    LimitContext,
    LimitRuleProvider,
    infer_board,
)
from .data.tdx_reader import DayBar
from .study import SignalEvent
from .strategy_models import (
    ENTRY_REASON_SIGNAL,
    EXIT_REASON_FORCED_EXIT,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    BacktestResult,
    EquityPoint,
    Fill,
    Position,
    _commission,
    validate_risk_pct,
)
from .strategy_schedule import (
    _month_key,
    _week_key,
    bar_session_price,
    compose_sell_reason,
    filter_events_by_signal_weekdays,
    parse_price_session,
    parse_signal_weekdays,
    parse_single_weekday,
    format_single_weekday,
    format_signal_weekdays,
)
from .strategy_metrics import compute_metrics
from .strategy_engine_helpers import PortfolioBacktesterHelpers

class PortfolioBacktester(PortfolioBacktesterHelpers):
    def __init__(
        self,
        cfg: AStockConfig,
        calendar: TradeCalendar,
        bars_by_code: Dict[str, Sequence[DayBar]],
        *,
        limit_rules: Optional[LimitRuleProvider] = None,
        adj_bars_by_code: Optional[Dict[str, Sequence[DayBar]]] = None,
        factor_by_code: Optional[Dict[str, Dict[int, float]]] = None,
        corporate_action_policy: str = "fail_closed",
    ):
        self.cfg = cfg
        self.calendar = calendar
        # dual_price_v1: execution+valuation ALWAYS on bars_by_code (RAW formal);
        # adj_bars_by_code is fill audit only — never trading index.
        trade_bars = bars_by_code or {}
        self.bars_by_code = trade_bars
        self.raw_bars_by_code = trade_bars
        self.adj_bars_by_code = adj_bars_by_code
        self.factor_by_code: Dict[str, Dict[int, float]] = factor_by_code or {}
        self.corporate_action_policy = str(
            corporate_action_policy or "fail_closed"
        ).strip().lower()
        self.limit_rules = limit_rules or DefaultAShareLimitRule()
        self._index: Dict[str, Dict[int, DayBar]] = {}
        self._raw_index: Dict[str, Dict[int, DayBar]] = {}
        self._adj_index: Dict[str, Dict[int, DayBar]] = {}
        self._sorted_dates: Dict[str, List[int]] = {}
        self._last_close: Dict[str, Dict[int, float]] = {}
        for code, bars in trade_bars.items():
            idx = {b.date: b for b in bars}
            self._index[code] = idx
            self._raw_index[code] = idx
            dates = [b.date for b in bars]
            self._sorted_dates[code] = dates
            last = None
            lc: Dict[int, float] = {}
            for b in bars:
                last = b.close
                lc[b.date] = last
            self._last_close[code] = lc

        for code, bars in (adj_bars_by_code or {}).items():
            self._adj_index[code] = {b.date: b for b in bars}

        # prev close map for limit rules (RAW execution bars only)
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
        signal_weekdays: Optional[Union[Sequence[int], str, int]] = None,
        buy_on: str = "open",
        sell_on: str = "open",
        buy_weekday: Optional[Union[int, str]] = None,
        exit_weekday: Optional[Union[int, str]] = None,
        holiday_policy: Optional[str] = None,
    ) -> BacktestResult:
        stop_loss_pct = validate_risk_pct("stop_loss_pct", stop_loss_pct)
        take_profit_pct = validate_risk_pct("take_profit_pct", take_profit_pct)
        entry_lag = int(entry_lag)
        if entry_lag < 1:
            raise ValueError("entry_lag must be >= 1, got %s" % entry_lag)
        signal_weekdays = parse_signal_weekdays(signal_weekdays)
        if signal_weekdays:
            events = filter_events_by_signal_weekdays(events, signal_weekdays)
        buy_on = parse_price_session(buy_on, default="open")
        sell_on = parse_price_session(sell_on, default="open")
        buy_weekday = parse_single_weekday(buy_weekday)
        exit_weekday = parse_single_weekday(exit_weekday)
        holiday_policy = normalize_holiday_policy(
            holiday_policy, default=DEFAULT_HOLIDAY_POLICY
        )
        account_mode = (account_mode or "portfolio").strip().lower()
        if account_mode in ("tdx", "per_stock", "independent", "通达信", "单票"):
            account_mode = "per_symbol"
        if account_mode not in ("portfolio", "per_symbol"):
            raise ValueError(
                "account_mode must be portfolio or per_symbol, got %s" % account_mode
            )
        period = period.upper()
        schedule_mode = (
            "weekday" if (buy_weekday is not None or exit_weekday is not None) else "tn"
        )
        notes = [
            "Example costs only; not user real trading costs.",
            "Survivor bias possible: local TDX may lack delisted stocks.",
            DefaultAShareLimitRule.BOUNDARY_NOTE,
            "Price dual-mode: signal may use causal qfq outside; execution/valuation ALWAYS on "
            "bars_by_code (RAW in formal). adj_bars_by_code is fill audit only.",
            "Schedule engine: all entry/exit dates are solved on the A-share trading-day calendar "
            "(T+N family); weekday anchors use planned civil date + holiday_policy=%s."
            % holiday_policy,
            "Sell at %s on exit day for time-stop/deferred risk (sell_on=%s)." % (sell_on, sell_on),
        ]
        if buy_weekday is not None:
            notes.append(
                "Buy schedule (weekday anchor): planned %s after signal at %s "
                "(overrides entry_lag stepping; entry_lag=%d kept for repro only; "
                "holiday_policy=%s)."
                % (format_single_weekday(buy_weekday), buy_on, entry_lag, holiday_policy)
            )
        else:
            notes.append(
                "Buy schedule (T+N): buy at %s of the N-th trading day after signal close "
                "(entry_lag=%d)." % (buy_on, entry_lag)
            )
        if exit_weekday is not None:
            notes.append(
                "Exit schedule (weekday anchor): planned %s after entry at %s "
                "(overrides hold N stepping; hold=%d kept for repro only; "
                "holiday_policy=%s)."
                % (format_single_weekday(exit_weekday), sell_on, hold, holiday_policy)
            )
        else:
            notes.append(
                "Exit schedule (hold N): after hold=%d period(s)/session(s), force flat at %s."
                % (hold, sell_on)
            )
        if signal_weekdays:
            notes.append(
                "Signal weekday filter: only signals on %s are tradable."
                % format_signal_weekdays(signal_weekdays)
            )
        else:
            notes.append("Signal weekday filter: all weekdays (no UI restriction).")
        notes.extend(
            [
                (
                    "Account mode: per_symbol (通达信对照) — each stock has its own virtual capital; "
                    "no cross-stock cash competition; metrics include equal-weight mean stock return."
                    if account_mode == "per_symbol"
                    else "Account mode: portfolio — single shared cash account with max_weight cap."
                ),
                "Time-stop: reason time_exit; weekday anchor exit: weekday_exit (legacy hold_expired normalized).",
                "Risk: trigger_on_daily_high_low (incl. entry day); execute_next_trading_day_open (T+1).",
                "Risk conflict policy: stop_first when same bar hits both SL and TP.",
                "schedule_mode=%s (weekday | tn)." % schedule_mode,
            ]
        )
        status = "ok"
        if research_unadjusted:
            status = "research_unadjusted"
            notes.append(
                "RESEARCH_UNADJUSTED: signal path may use raw; engine still executes on "
                "bars_by_code (execution/valuation raw). Not formal signal study."
            )
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
        # pending_buys[actual_entry] -> list of dicts with code + schedule meta
        pending_buys: Dict[int, List[dict]] = {}
        for sig_date, codes in sig_map.items():
            planned_entry: Optional[int] = None
            entry_shift = 0
            if buy_weekday is not None:
                resolved = self.calendar.resolve_weekday_session(
                    sig_date,
                    buy_weekday,
                    strict=True,
                    holiday_policy=holiday_policy,
                )
                if resolved is None:
                    continue
                planned_entry, entry, entry_shift = resolved
            else:
                entry = self.calendar.nth_trading_day_after(sig_date, entry_lag)
                planned_entry = entry
            if entry is None:
                continue
            if start and entry < start:
                # still allow if signal before start but entry in range? skip entries before start
                continue
            if end and entry > end:
                continue
            pending_buys.setdefault(entry, [])
            have = {x["code"] for x in pending_buys[entry]}
            for c in codes:
                if c in have:
                    continue
                pending_buys[entry].append(
                    {
                        "code": c,
                        "signal_date": sig_date,
                        "planned_entry_date": planned_entry,
                        "entry_shift_days": int(entry_shift or 0),
                        "holiday_policy": holiday_policy,
                    }
                )
                have.add(c)

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
        ca_unsupported_notes: List[str] = []
        ca_fail = False
        ca_policy, _ca_init_notes, _ca_force = normalize_corporate_action_policy(
            getattr(self, "corporate_action_policy", None)
        )
        if _ca_init_notes:
            ca_unsupported_notes.extend(_ca_init_notes)
        if _ca_force:
            ca_fail = True

        # precompute period end sets for week/month completion tracking
        week_ends = self._period_end_dates("WEEK")
        month_ends = self._period_end_dates("MONTH")

        for d in sim_dates:
            # Factor change while open → unsupported (no share restatement)
            if self.factor_by_code and positions:
                for code, pos in list(positions.items()):
                    if pos.entry_factor is None:
                        continue
                    fac_now = factor_on_or_before(self.factor_by_code, code, d)
                    msg = check_open_hold_factor_change(
                        code=code,
                        entry_date=int(pos.entry_date),
                        entry_factor=pos.entry_factor,
                        day=d,
                        fac_now=fac_now,
                        policy=ca_policy,
                    )
                    if msg:
                        ca_fail = True
                        if msg not in ca_unsupported_notes:
                            ca_unsupported_notes.append(msg)
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
                # Exit session: risk SL/TP always next-day OPEN; time/weekday honor sell_on.
                sell_session = risk_exit_session(
                    info.get("trigger") or pos.trigger_reason, sell_on
                )
                raw_px = bar_session_price(bar, sell_session)
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
                # Prefer risk trigger; else weekday vs hold time-exit.
                if info.get("trigger") or pos.trigger_reason:
                    fallback = EXIT_REASON_TIME_EXIT
                elif pos.time_exit_kind == EXIT_REASON_WEEKDAY_EXIT or (
                    pos.exit_date is not None
                ):
                    fallback = EXIT_REASON_WEEKDAY_EXIT
                else:
                    fallback = EXIT_REASON_TIME_EXIT
                reason = compose_sell_reason(
                    info.get("trigger") or pos.trigger_reason,
                    info.get("defer") or pos.defer_reason,
                    fallback=fallback,
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
                        planned_date=pos.planned_exit_date or pos.exit_date,
                        actual_date=d,
                        shift_days=int(pos.exit_shift_days or 0),
                        holiday_policy=pos.holiday_policy,
                        **self._fill_price_audit(
                            code, d, sell_session, session_raw=raw_px
                        ),
                    )
                )

            # 2) entries at open/close (buy_on)
            for order in pending_buys.get(d, []):
                code = order["code"] if isinstance(order, dict) else order
                if code in positions:
                    # do not reset hold
                    continue
                bar = self._index.get(code, {}).get(d)
                if not bar:
                    continue
                raw_buy = bar_session_price(bar, buy_on)
                if raw_buy <= 0:
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
                px = raw_buy * (1.0 + self.cfg.costs.slippage)
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
                planned_entry = (
                    order.get("planned_entry_date") if isinstance(order, dict) else d
                ) or d
                entry_shift = (
                    int(order.get("entry_shift_days") or 0)
                    if isinstance(order, dict)
                    else 0
                )
                order_hp = (
                    (order.get("holiday_policy") if isinstance(order, dict) else None)
                    or holiday_policy
                )
                exit_date = None
                planned_exit = None
                exit_shift = 0
                time_exit_kind = EXIT_REASON_TIME_EXIT
                if exit_weekday is not None:
                    resolved_x = self.calendar.resolve_weekday_session(
                        d,
                        exit_weekday,
                        strict=True,
                        holiday_policy=order_hp,
                    )
                    if resolved_x is None:
                        # cannot schedule weekday exit — skip this entry
                        if account_mode == "per_symbol":
                            cash_by_code[code] = float(cash_by_code.get(code, 0.0)) + amount + comm
                        else:
                            cash += amount + comm
                        continue
                    planned_exit, exit_date, exit_shift = resolved_x
                    # A-share T+1: never exit same day as entry
                    if exit_date is not None and exit_date <= d:
                        nxt = self.calendar.next_trading_day(d)
                        if nxt is None:
                            if account_mode == "per_symbol":
                                cash_by_code[code] = float(cash_by_code.get(code, 0.0)) + amount + comm
                            else:
                                cash += amount + comm
                            continue
                        exit_date = nxt
                        exit_shift = (
                            (_date(exit_date // 10000, (exit_date // 100) % 100, exit_date % 100)
                             - _date(planned_exit // 10000, (planned_exit // 100) % 100, planned_exit % 100)).days
                            if planned_exit
                            else 0
                        )
                    # Disable session/period countdown; exit_date drives time-stop.
                    h_sess = 10**9
                    h_per = 10**9
                    time_exit_kind = EXIT_REASON_WEEKDAY_EXIT
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
                    exit_date=exit_date,
                    defer_reason=None,
                    planned_entry_date=int(planned_entry) if planned_entry else d,
                    entry_shift_days=entry_shift,
                    planned_exit_date=int(planned_exit) if planned_exit else None,
                    exit_shift_days=int(exit_shift or 0),
                    holiday_policy=order_hp,
                    time_exit_kind=time_exit_kind,
                    entry_adjusted_reference_price=self._adj_session_price(code, d, buy_on),
                    entry_factor=self._factor_on(code, d),
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
                        reason=ENTRY_REASON_SIGNAL,
                        planned_date=int(planned_entry) if planned_entry else d,
                        actual_date=d,
                        shift_days=entry_shift,
                        holiday_policy=order_hp,
                        **self._fill_price_audit(code, d, buy_on, session_raw=raw_buy),
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

        # ------------------------------------------------------------------
        # EOD forced exit (extracted helper)
        # ------------------------------------------------------------------
        cash = self._force_eod_exits(
            positions=positions,
            deferred_sells=deferred_sells,
            fills=fills,
            equity_curve=equity_curve,
            notes=notes,
            sim_dates=sim_dates,
            account_mode=account_mode,
            cash=cash,
            cash_by_code=cash_by_code,
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
        # open positions at end (after EOD forced exit)
        open_n = len(positions)
        open_mv = self._mkt_value(positions, sim_dates[-1]) if sim_dates and positions else 0.0
        metrics["n_open_positions"] = open_n
        metrics["open_market_value"] = float(open_mv)
        metrics["eod_forced_exit"] = True
        metrics["n_forced_exits"] = int(
            sum(1 for f in fills if f.side == "SELL" and f.reason == EXIT_REASON_FORCED_EXIT)
        )
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
        metrics = self._maybe_zero_cost_replay(
            metrics=metrics,
            notes=notes,
            events=events,
            hold=hold,
            period=period,
            run_id=run_id,
            start=start,
            end=end,
            research_unadjusted=research_unadjusted,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            entry_lag=entry_lag,
            account_mode=account_mode,
            signal_weekdays=signal_weekdays,
            buy_on=buy_on,
            sell_on=sell_on,
            buy_weekday=buy_weekday,
            exit_weekday=exit_weekday,
            holiday_policy=holiday_policy,
            _skip_zero_replay=_skip_zero_replay,
        )

        if ca_fail:
            status = "unsupported_corporate_action"
            notes.extend(ca_unsupported_notes)
            notes.append(
                "Formal metrics not claimed: corporate action during open hold "
                "(policy=%s). No share restatement from cumulative factors."
                % ca_policy
            )
        metrics["n_corporate_actions"] = 0
        metrics["corporate_action_policy"] = ca_policy

        return BacktestResult(
            run_id=run_id,
            config={
                "initial_capital": self.cfg.initial_capital,
                "max_weight": self.cfg.max_weight,
                "hold": hold,
                "period": period,
                "entry_lag": entry_lag,
                "signal_weekdays": signal_weekdays,
                "buy_on": buy_on,
                "sell_on": sell_on,
                "buy_weekday": buy_weekday,
                "exit_weekday": exit_weekday,
                "schedule_mode": schedule_mode,
                "holiday_policy": holiday_policy,
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


