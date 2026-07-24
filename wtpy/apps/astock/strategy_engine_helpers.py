# -*- coding: utf-8 -*-
"""Mixin helpers for PortfolioBacktester (EOD, zero-cost, pricing, periods)."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from dataclasses import asdict

from .config import AStockConfig, CostConfig
from .corporate_action import factor_on_or_before
from .data.tdx_reader import DayBar
from .study import SignalEvent
from .strategy_models import (
    EXIT_REASON_FORCED_EXIT,
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_WEEKDAY_EXIT,
    BacktestResult,
    EquityPoint,
    Fill,
    Position,
    _commission,
)
from .strategy_schedule import bar_session_price, parse_price_session, _week_key, _month_key


class PortfolioBacktesterHelpers:
    """Post-run and pricing helpers mixed into PortfolioBacktester."""

    def _maybe_zero_cost_replay(
        self,
        *,
        metrics: dict,
        notes: List[str],
        events: Sequence[SignalEvent],
        hold: int,
        period: str,
        run_id: str,
        start: Optional[int],
        end: Optional[int],
        research_unadjusted: bool,
        stop_loss_pct: Optional[float],
        take_profit_pct: Optional[float],
        entry_lag: int,
        account_mode: str,
        signal_weekdays: Optional[List[int]],
        buy_on: str,
        sell_on: str,
        buy_weekday: Optional[int],
        exit_weekday: Optional[int],
        holiday_policy: str,
        _skip_zero_replay: bool,
    ) -> dict:
        """Zero-cost control replay (same dual-price maps); mutates metrics/notes."""
        if not _skip_zero_replay:
            try:
                # type(self) constructs the concrete backtester without importing strategy_engine
                # (avoids circular import with the mixin host class).
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
                z_bt = type(self)(
                    zero_cfg,
                    self.calendar,
                    self.raw_bars_by_code,
                    adj_bars_by_code=self.adj_bars_by_code,
                    standard_qfq_bars_by_code=getattr(
                        self, "standard_qfq_bars_by_code", None
                    ),
                    factor_by_code=self.factor_by_code,
                    limit_rules=self.limit_rules,
                    corporate_action_policy=self.corporate_action_policy,
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
                    signal_weekdays=signal_weekdays,
                    buy_on=buy_on,
                    sell_on=sell_on,
                    buy_weekday=buy_weekday,
                    exit_weekday=exit_weekday,
                    account_mode=account_mode,
                    holiday_policy=holiday_policy,
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

        return metrics

    def _force_eod_exits(
        self,
        *,
        positions: Dict[str, Position],
        deferred_sells: Dict[str, dict],
        fills: List[Fill],
        equity_curve: List[EquityPoint],
        notes: List[str],
        sim_dates: Sequence[int],
        account_mode: str,
        cash: float,
        cash_by_code: Dict[str, float],
    ) -> float:
        """Liquidate residual positions on last sim day at raw close (T+1 aware).

        Mutates positions/fills/equity_curve/notes/cash_by_code. Returns updated cash.
        """
        if not positions or not sim_dates:
            return cash
        last_d = int(sim_dates[-1])
        forced_n = 0
        for code, pos in list(positions.items()):
            if int(pos.entry_date) >= last_d:
                continue
            bar = self._index.get(code, {}).get(last_d)
            if not bar:
                bar_px = self._last_px_on_or_before(code, last_d)
                if not bar_px or bar_px <= 0:
                    continue
                session_raw = float(bar_px)
            else:
                session_raw = float(bar.close)
            if session_raw <= 0:
                continue
            px = session_raw * (1.0 - self.cfg.costs.slippage)
            if px <= 0:
                continue
            pos = positions.pop(code)
            amount = pos.shares * px
            comm = _commission(amount, self.cfg.costs)
            tax = amount * self.cfg.costs.stamp_tax_rate
            if account_mode == "per_symbol":
                cash_by_code[code] = (
                    float(cash_by_code.get(code, 0.0)) + amount - comm - tax
                )
            else:
                cash += amount - comm - tax
            fills.append(
                Fill(
                    date=last_d,
                    std_code=code,
                    side="SELL",
                    price=px,
                    shares=pos.shares,
                    amount=amount,
                    commission=comm,
                    stamp_tax=tax,
                    reason=EXIT_REASON_FORCED_EXIT,
                    planned_date=last_d,
                    actual_date=last_d,
                    shift_days=0,
                    holiday_policy=pos.holiday_policy,
                    execution_price=px,
                        **self._fill_price_audit(
                        code, last_d, "close", session_raw=session_raw
                    ),
                )
            )
            forced_n += 1
            deferred_sells.pop(code, None)
        if forced_n:
            notes.append(
                "EOD forced_exit: liquidated %d open position(s) at last "
                "sim day close (raw); T+1 skips same-day entry leftovers."
                % forced_n
            )
            mv = self._mkt_value(positions, last_d)
            if account_mode == "per_symbol":
                cash_sum = float(sum(cash_by_code.values())) if cash_by_code else 0.0
                pt = EquityPoint(
                    date=last_d, cash=cash_sum, market_value=mv, equity=cash_sum + mv
                )
            else:
                pt = EquityPoint(
                    date=last_d, cash=cash, market_value=mv, equity=cash + mv
                )
            if equity_curve and equity_curve[-1].date == last_d:
                equity_curve[-1] = pt
            else:
                equity_curve.append(pt)
        return cash

    def _is_exit_due(
        self,
        pos: Position,
        date: int,
        week_ends: set,
        month_ends: set,
    ) -> bool:
        if date <= pos.entry_date:
            return False
        # Weekday-based force flat: exit on/after scheduled trading day
        if getattr(pos, "exit_date", None) is not None:
            return int(date) >= int(pos.exit_date)
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

    def _unadj_session_price(
        self, code: str, date: int, session: str
    ) -> Optional[float]:
        """RAW session open/close on date (no slippage). Same as execution basis."""
        bar = (self._raw_index.get(code) or {}).get(date)
        if bar is None:
            bar = (self._index.get(code) or {}).get(date)
        if not bar:
            return None
        try:
            px = bar_session_price(bar, session)
            return float(px) if px and float(px) > 0 else None
        except Exception:
            return None

    def _adj_session_price(
        self, code: str, date: int, session: str
    ) -> Optional[float]:
        """Point-in-time research reference session price (audit only)."""
        bar = (self._adj_index.get(code) or {}).get(date)
        if not bar:
            return None
        try:
            px = bar_session_price(bar, session)
            return float(px) if px and float(px) > 0 else None
        except Exception:
            return None

    def _qfq_session_price(
        self, code: str, date: int, session: str
    ) -> Optional[float]:
        """Standard ordinary qfq session price (signal-level audit only)."""
        qfq_index = getattr(self, "_qfq_index", None) or {}
        bar = (qfq_index.get(code) or {}).get(date)
        if not bar:
            return None
        try:
            px = bar_session_price(bar, session)
            return float(px) if px and float(px) > 0 else None
        except Exception:
            return None

    def _factor_on(self, code: str, date: int) -> Optional[float]:
        return factor_on_or_before(self.factor_by_code, code, date)

    def _fill_price_audit(
        self,
        code: str,
        date: int,
        session: str,
        *,
        session_raw: Optional[float] = None,
    ) -> dict:
        """Build Fill four-lane audit fields (execution remains RAW)."""
        raw_px = session_raw
        if raw_px is None:
            raw_px = self._unadj_session_price(code, date, session)
        pit_px = self._adj_session_price(code, date, session)
        qfq_px = self._qfq_session_price(code, date, session)
        fac = self._factor_on(code, date)
        point_scale = None
        point_anchor = None
        qfq_scale = None
        qfq_anchor = None
        if pit_px is not None and raw_px is not None and float(raw_px) > 0:
            try:
                point_scale = float(pit_px) / float(raw_px)
                if fac is not None and point_scale and float(point_scale) != 0.0:
                    point_anchor = float(fac) / float(point_scale)
            except (TypeError, ValueError, ZeroDivisionError):
                point_scale = None
                point_anchor = None
        if qfq_px is not None and raw_px is not None and float(raw_px) > 0:
            try:
                qfq_scale = float(qfq_px) / float(raw_px)
                if fac is not None and qfq_scale and float(qfq_scale) != 0.0:
                    qfq_anchor = float(fac) / float(qfq_scale)
            except (TypeError, ValueError, ZeroDivisionError):
                qfq_scale = None
                qfq_anchor = None
        pit = float(pit_px) if pit_px is not None else None
        qfq = float(qfq_px) if qfq_px is not None else None
        return {
            "raw_price": float(raw_px) if raw_px is not None else None,
            # execution_price set by caller (RAW * slippage)
            "adjusted_reference_price": pit,
            "point_in_time_reference_price": pit,
            "standard_qfq_reference_price": qfq,
            "adjustment_factor": fac,
            "adjustment_base": point_anchor,
            "adjustment_scale": point_scale,
            "point_scale": point_scale,
            "point_anchor_factor": point_anchor,
            "qfq_scale": qfq_scale,
            "qfq_anchor_factor": qfq_anchor,
            "price_session": parse_price_session(session),
            "price_source": "raw",
            "execution_price_mode": "raw",
            "valuation_price_mode": "raw",
            "signal_price_mode": "standard_qfq",
        }

    def _enrich_fill_price_fields(self, fill_kwargs: dict, *, session_raw: float, px: float) -> dict:
        """Ensure four-lane Fill fields; price remains raw execution."""
        fill_kwargs = dict(fill_kwargs or {})
        fill_kwargs.setdefault("raw_price", session_raw)
        fill_kwargs["execution_price"] = px
        fill_kwargs.setdefault("execution_price_mode", "raw")
        fill_kwargs.setdefault("valuation_price_mode", "raw")
        fill_kwargs.setdefault("signal_price_mode", "standard_qfq")
        pit = fill_kwargs.get("adjusted_reference_price")
        if pit is not None:
            fill_kwargs.setdefault("point_in_time_reference_price", pit)
        if fill_kwargs.get("adjustment_scale") is not None:
            fill_kwargs.setdefault("point_scale", fill_kwargs.get("adjustment_scale"))
        if fill_kwargs.get("adjustment_base") is not None:
            fill_kwargs.setdefault("point_anchor_factor", fill_kwargs.get("adjustment_base"))
        if fill_kwargs.get("adjustment_factor") is not None and session_raw:
            pass
        slip = abs(float(px) - float(session_raw)) if session_raw is not None else None
        if slip is not None:
            fill_kwargs.setdefault("slippage_amount", slip)
        return fill_kwargs


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


