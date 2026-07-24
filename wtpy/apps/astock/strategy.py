"""Portfolio backtest engine for A-share multi-period signals.

Business rules (locked):
- Dual schedule model (v2): UI configures weekdays; engine always solves on the trading-day calendar (T+N family).
- Signal confirmed on period close.
- Without buy_weekday: buy on the N-th trading day after signal (entry_lag, default 1).
- With buy_weekday: buy on the first trading day on/after that ISO weekday after signal (overrides entry_lag).
- Without exit_weekday: DAY hold=N holds N trading days (hold=1 exits next session; cannot sell entry day).
  WEEK/MONTH hold=N: after N completed periods, exit on next tradable day; DWM hold=N same as day holds.
- With exit_weekday: exit on first trading day on/after that weekday after entry (overrides hold stepping).
- Holidays: planned weekday is civil calendar; holiday_policy maps to actual session
  (default next_trading_day). Legacy next_weekday_trading_day may skip to next week.
- Optional signal_weekdays: only signals whose calendar weekday is in the allow-list (1=Mon … 7=Sun); empty = all.
- Time-stop (hold expiry): reason time_exit; weekday exit: weekday_exit (legacy alias hold_expired still mapped).
- Risk exits (stop_loss / take_profit): trigger on RAW high/low vs RAW entry_price;
  execute next tradable day RAW open *(1±slippage).
- Repeat signals do not reset remaining hold.
- Buy/sell execution px = RAW open|close *(1±slippage) per buy_on/sell_on.
- Valuation (equity MTM) uses RAW close only.
- Limit rules (prev_close/open/high/low) use RAW bars only.
- Optional adj_bars_by_code is audit/reference only (fill adjusted_reference_price); never the trading index.
- Signal bars may be causal-qfq outside this engine; research_unadjusted affects signals outside only.
- account_mode=portfolio: shared cash + max_weight.
- account_mode=per_symbol: each stock independent virtual capital (通达信对照).
- Suspended days: mark with last valid close.
- Limit-down untradeable sells are deferred.
- Fills record planned/actual dates and holiday_policy when schedule shifts apply.
- Optional factor_by_code: corporate-action fail-closed during open holds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

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
from .data.periods import PeriodBar, aggregate_month, aggregate_week
from .data.tdx_reader import DayBar
from .study import SignalEvent

# Canonical exit / entry reason codes (phase-1 research center).
EXIT_REASON_STOP_LOSS = "stop_loss"
EXIT_REASON_TAKE_PROFIT = "take_profit"
EXIT_REASON_TIME_EXIT = "time_exit"
EXIT_REASON_WEEKDAY_EXIT = "weekday_exit"
EXIT_REASON_REVERSE_SIGNAL = "reverse_signal"
EXIT_REASON_GUA_WEAKENING = "gua_weakening"
EXIT_REASON_FORCED_EXIT = "forced_exit"
EXIT_REASON_DELISTING_EXIT = "delisting_exit"
ENTRY_REASON_SIGNAL = "signal_entry"

# Legacy reason still accepted in compose fallbacks.
LEGACY_HOLD_EXPIRED = "hold_expired"


@dataclass
class Position:
    std_code: str
    shares: int
    entry_date: int
    entry_price: float  # RAW execution entry (with slippage)
    # For DAY/DWM: remaining trading sessions after entry before exit is allowed
    hold_left_sessions: int
    # For WEEK/MONTH: number of full periods still required after entry period
    hold_left_periods: int
    period_mode: str  # DAY | WEEK | MONTH | DWM
    cost: float  # true cash cost (amount + commission at RAW execution)
    entry_period_key: Optional[Tuple] = None
    # Optional risk exits (fraction, e.g. 0.03 = 3%).
    # trigger_on_daily_high_low including entry day; execute_next_trading_day_open (T+1).
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trigger_reason: Optional[str] = None  # stop_loss | take_profit (sticky once set)
    defer_reason: Optional[str] = None  # suspended | limit_down | bad_price while waiting to sell
    # When set: force time-stop on this calendar trading day (weekday-based exit).
    exit_date: Optional[int] = None
    # Schedule provenance (phase-1)
    planned_entry_date: Optional[int] = None
    entry_shift_days: int = 0
    planned_exit_date: Optional[int] = None
    exit_shift_days: int = 0
    holiday_policy: Optional[str] = None
    time_exit_kind: str = "time_exit"  # time_exit | weekday_exit
    # Dual-price audit: optional adjusted reference at entry (not used for PnL).
    entry_adjusted_reference_price: Optional[float] = None
    entry_factor: Optional[float] = None  # factor snapshot at entry for CA fail-closed


@dataclass
class Fill:
    date: int
    std_code: str
    side: str
    price: float  # execution price (RAW session * slippage)
    shares: int
    amount: float
    commission: float
    stamp_tax: float
    reason: str
    planned_date: Optional[int] = None
    actual_date: Optional[int] = None
    shift_days: int = 0
    holiday_policy: Optional[str] = None
    # RAW session open/close before slippage (market tape); equals execution basis.
    raw_price: Optional[float] = None
    # Optional causal-qfq (or other adj) session price same date for audit only.
    adjusted_reference_price: Optional[float] = None
    adjustment_factor: Optional[float] = None
    adjustment_base: Optional[float] = None
    adjustment_scale: Optional[float] = None
    price_session: Optional[str] = None  # open | close
    price_source: Optional[str] = None  # "raw" | "adjusted_reference"


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
    status: str = "ok"  # ok | research_unadjusted | no_go | unsupported_corporate_action

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



# Weekday allow-list: ISO 1=Monday … 7=Sunday (matches datetime.isoweekday()).
_WEEKDAY_ALIASES = {
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "mon": 1, "monday": 1, "一": 1, "周一": 1, "星期一": 1,
    "tue": 2, "tues": 2, "tuesday": 2, "二": 2, "周二": 2, "星期二": 2,
    "wed": 3, "wednesday": 3, "三": 3, "周三": 3, "星期三": 3,
    "thu": 4, "thur": 4, "thurs": 4, "thursday": 4, "四": 4, "周四": 4, "星期四": 4,
    "fri": 5, "friday": 5, "五": 5, "周五": 5, "星期五": 5,
    "sat": 6, "saturday": 6, "六": 6, "周六": 6, "星期六": 6,
    "sun": 7, "sunday": 7, "日": 7, "天": 7, "周日": 7, "周天": 7, "星期日": 7, "星期天": 7,
}
_WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}



def parse_price_session(value, *, default: str = "open") -> str:
    """Normalize buy/sell session: 'open' or 'close'."""
    if value is None or value == "":
        return default
    s = str(value).strip().lower()
    if s in ("open", "o", "开盘", "开", "op"):
        return "open"
    if s in ("close", "c", "收盘", "收", "cl"):
        return "close"
    raise ValueError("price session must be open or close, got %r" % (value,))


def session_label_cn(session: str) -> str:
    return "开盘" if parse_price_session(session) == "open" else "收盘"


def bar_session_price(bar, session: str) -> float:
    """Raw price from bar for open/close session (no slippage)."""
    session = parse_price_session(session)
    if session == "close":
        return float(bar.close)
    return float(bar.open)


def parse_signal_weekdays(value) -> Optional[List[int]]:
    """Parse weekday allow-list.

    Accepts None / empty (all days), list/tuple of ints or strings, or comma-separated
    string like "5" / "fri,五" / "1,3,5". Returns sorted unique ints in 1..7, or None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s in ("*", "all", "全部", "any"):
            return None
        parts = [p.strip() for p in s.replace("，", ",").replace("、", ",").split(",") if p.strip()]
        value = parts
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [int(value)]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("signal_weekdays must be list/str/int, got %r" % (type(value),))
    if len(value) == 0:
        return None
    out: Set[int] = set()
    for item in value:
        if item is None or item == "":
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            n = int(item)
            if n == 0:
                n = 7  # allow 0 as Sunday alias
            if n < 1 or n > 7:
                raise ValueError("signal_weekdays item out of range 1..7: %s" % item)
            out.add(n)
            continue
        key = str(item).strip().lower()
        if key in _WEEKDAY_ALIASES:
            out.add(_WEEKDAY_ALIASES[key])
            continue
        # bare digit string already handled via int path if pure digit
        if key.isdigit():
            n = int(key)
            if n == 0:
                n = 7
            if 1 <= n <= 7:
                out.add(n)
                continue
        raise ValueError("unknown weekday in signal_weekdays: %r" % (item,))
    if not out:
        return None
    return sorted(out)



def parse_single_weekday(value) -> Optional[int]:
    """Parse one weekday 1..7, or None if empty (use legacy lag/hold)."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    days = parse_signal_weekdays(value if not isinstance(value, (int, float)) else int(value))
    if not days:
        return None
    return int(days[0])


def format_single_weekday(day: Optional[int]) -> str:
    if day is None:
        return "—"
    return format_signal_weekdays([int(day)])


def format_signal_weekdays(days: Optional[Sequence[int]]) -> str:
    if not days:
        return "全部"
    return "、".join(_WEEKDAY_CN.get(int(d), str(d)) for d in days)


def yyyymmdd_isoweekday(d: int) -> int:
    """Return ISO weekday 1..7 for YYYYMMDD int."""
    y, m, day = int(d) // 10000, (int(d) // 100) % 100, int(d) % 100
    return _date(y, m, day).isoweekday()


def filter_events_by_signal_weekdays(
    events: Sequence[SignalEvent],
    weekdays: Optional[Sequence[int]],
) -> List[SignalEvent]:
    """Keep only events whose signal date weekday is allowed. None/empty = all."""
    allowed = parse_signal_weekdays(weekdays)
    if not allowed:
        return list(events)
    allow_set = set(int(x) for x in allowed)
    kept: List[SignalEvent] = []
    for ev in events:
        try:
            wd = yyyymmdd_isoweekday(int(ev.date))
        except Exception:
            continue
        if wd in allow_set:
            kept.append(ev)
    return kept


def normalize_exit_reason(reason: Optional[str]) -> str:
    """Map legacy codes to phase-1 canonical exit reasons."""
    if not reason:
        return EXIT_REASON_TIME_EXIT
    r = str(reason)
    if r == LEGACY_HOLD_EXPIRED:
        return EXIT_REASON_TIME_EXIT
    return r


def compose_sell_reason(
    trigger: Optional[str],
    defer: Optional[str],
    fallback: str = EXIT_REASON_TIME_EXIT,
) -> str:
    """Compose fill reason preserving original risk trigger across deferrals.

    Canonical fallbacks: time_exit (hold N), weekday_exit (weekday anchor).
    Legacy hold_expired is normalized to time_exit.
    """
    fb = normalize_exit_reason(fallback)
    if trigger and defer:
        return f"{trigger}_deferred_{defer}"
    if trigger:
        return trigger
    if defer in ("suspended", "limit_down", "bad_price"):
        return f"{fb}_deferred_{defer}"
    return fb


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
                    adj_bars_by_code=self.adj_bars_by_code,
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
        """Adjusted-reference session price (audit only; not used for PnL)."""
        bar = (self._adj_index.get(code) or {}).get(date)
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
        """Build Fill dual-price audit fields (execution remains RAW)."""
        raw_px = session_raw
        if raw_px is None:
            raw_px = self._unadj_session_price(code, date, session)
        adj_px = self._adj_session_price(code, date, session)
        fac = self._factor_on(code, date)
        scale = None
        base = None
        if adj_px is not None and raw_px is not None and float(raw_px) > 0:
            try:
                scale = float(adj_px) / float(raw_px)
                base = float(raw_px)
            except (TypeError, ValueError, ZeroDivisionError):
                scale = None
                base = None
        return {
            "raw_price": float(raw_px) if raw_px is not None else None,
            "adjusted_reference_price": float(adj_px) if adj_px is not None else None,
            "adjustment_factor": fac,
            "adjustment_base": base,
            "adjustment_scale": scale,
            "price_session": parse_price_session(session),
            "price_source": "raw",
        }

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
    win_pnls: List[float] = []
    loss_pnls: List[float] = []  # absolute magnitudes of losing trades
    gross_profit = 0.0
    gross_loss = 0.0  # positive number = sum of |loss|
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
                    win_pnls.append(float(pnl))
                    gross_profit += float(pnl)
                elif pnl < 0:
                    loss_pnls.append(float(-pnl))
                    gross_loss += float(-pnl)
                # pnl == 0: closed but neither win nor loss for avg
    win_rate = wins / closed if closed else 0.0
    avg_win = float(sum(win_pnls) / len(win_pnls)) if win_pnls else None
    avg_loss = float(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else None
    # 盈亏比 = 平均盈利 ÷ 平均亏损绝对值
    if avg_win is not None and avg_loss is not None and avg_loss > 1e-12:
        payoff_ratio = float(avg_win / avg_loss)
    elif avg_win is not None and (avg_loss is None or avg_loss <= 1e-12):
        payoff_ratio = None  # no losing trades — undefined / infinite
    else:
        payoff_ratio = None
    # 盈利因子 = 总盈利 ÷ 总亏损绝对值
    if gross_loss > 1e-12:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > 0 and gross_loss <= 1e-12:
        profit_factor = None
    else:
        profit_factor = 0.0 if closed else None

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
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "profit_loss_ratio": payoff_ratio,  # alias for UI
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "profit_factor": profit_factor,
        "turnover": float(turnover),
        "cost_total": float(cost_total),
    }
