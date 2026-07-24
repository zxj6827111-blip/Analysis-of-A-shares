# -*- coding: utf-8 -*-
"""Corporate-action policy helpers shared by full and fast engines.

Formal path: fail_closed only. Cumulative adjustment factors alone never invent
share/cash ledgers. Real cash-dividend / split event ledgers are not implemented.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Policies
POLICY_FAIL_CLOSED = "fail_closed"
POLICY_NOT_CHECKED = "not_checked"
# Rejected aliases (never formal-ok)
POLICY_LEDGER_ALIASES = frozenset({"ledger_factor_ratio", "ledger"})


def normalize_corporate_action_policy(policy: Optional[str]) -> Tuple[str, List[str], bool]:
    """Return (policy, notes, force_unsupported).

    Any ledger_* request is rewritten to fail_closed and marked unsupported.
    """
    p = str(policy or POLICY_FAIL_CLOSED).strip().lower()
    notes: List[str] = []
    force = False
    if p in POLICY_LEDGER_ALIASES:
        notes.append(
            "unsupported_corporate_action: ledger_factor_ratio rejected without "
            "real corporate-action cash/share events; forced fail_closed."
        )
        return POLICY_FAIL_CLOSED, notes, True
    if p in ("fail", "unsupported"):
        p = POLICY_FAIL_CLOSED
    if p not in (POLICY_FAIL_CLOSED, POLICY_NOT_CHECKED):
        p = POLICY_FAIL_CLOSED
    return p, notes, force


def build_factor_by_code(
    factor_series: Optional[Sequence[Any]],
) -> Tuple[Dict[str, Dict[int, float]], List[str]]:
    """Build {std_code: {date: factor}} from FactorSeries-like objects.

    Returns (maps, errors). Shared by full and fast service paths.
    """
    out: Dict[str, Dict[int, float]] = {}
    errors: List[str] = []
    if not factor_series:
        return out, errors
    try:
        for fs in factor_series:
            code_k = getattr(fs, "std_code", None) or getattr(fs, "code", None)
            if not code_k:
                continue
            dates_f = list(getattr(fs, "dates", None) or [])
            facs_f = list(getattr(fs, "factors", None) or [])
            if dates_f and facs_f and len(dates_f) == len(facs_f):
                out[str(code_k)] = {int(d): float(f) for d, f in zip(dates_f, facs_f)}
            else:
                errors.append("factor series length mismatch for %s" % code_k)
    except Exception as e:  # noqa: BLE001
        return {}, ["factor_by_code build failed: %s" % e]
    return out, errors


def factor_on_or_before(
    factor_by_code: Dict[str, Dict[int, float]],
    code: str,
    date: int,
) -> Optional[float]:
    """Last factor with date <= trade date (forward-filled from known points)."""
    fmap = factor_by_code.get(code) or {}
    if not fmap:
        return None
    if date in fmap:
        try:
            return float(fmap[date])
        except (TypeError, ValueError):
            return None
    best = None
    for k in fmap:
        if int(k) <= int(date) and (best is None or int(k) > int(best)):
            best = k
    if best is None:
        return None
    try:
        return float(fmap[best])
    except (TypeError, ValueError):
        return None


def code_has_factor_map(factor_by_code: Dict[str, Dict[int, float]], code: str) -> bool:
    return bool(factor_by_code.get(code))


def check_open_hold_factor_change(
    *,
    code: str,
    entry_date: int,
    entry_factor: Optional[float],
    day: int,
    fac_now: Optional[float],
    policy: str = POLICY_FAIL_CLOSED,
) -> Optional[str]:
    """If factor jumped while open, return unsupported message; else None."""
    if entry_factor is None or fac_now is None:
        return None
    try:
        f0 = float(entry_factor)
        f1 = float(fac_now)
    except (TypeError, ValueError):
        return (
            "unsupported_corporate_action: factor check error %s day=%s"
            % (code, day)
        )
    if abs(f1 - f0) <= 1e-9 or f0 == 0.0:
        return None
    return (
        "unsupported_corporate_action: factor changed while open "
        "%s entry_date=%s entry_factor=%s day=%s factor=%s policy=%s"
        % (code, entry_date, entry_factor, day, fac_now, policy)
    )


def check_trade_factor_coverage(
    factor_by_code: Dict[str, Dict[int, float]],
    *,
    code: str,
    entry_date: int,
    exit_date: int,
    enforce: bool,
) -> Optional[str]:
    """Per-trade CA gate for fast engine.

    When enforce is True:
    - missing code map → unsupported
    - missing entry or exit factor → unsupported
    - factor change entry→exit → unsupported
    Returns message to block trade, or None to allow.
    """
    if not enforce:
        return None
    if not code_has_factor_map(factor_by_code, code):
        return (
            "unsupported_corporate_action: missing factor map for trade code "
            "%s entry=%s exit=%s" % (code, entry_date, exit_date)
        )
    f_ent = factor_on_or_before(factor_by_code, code, entry_date)
    f_ex = factor_on_or_before(factor_by_code, code, exit_date)
    if f_ent is None or f_ex is None:
        return (
            "unsupported_corporate_action: incomplete factor coverage "
            "%s entry=%s factor=%s exit=%s factor=%s"
            % (code, entry_date, f_ent, exit_date, f_ex)
        )
    if abs(float(f_ex) - float(f_ent)) > 1e-9:
        return (
            "unsupported_corporate_action: fast hold spans factor change "
            "%s entry=%s factor=%s exit=%s factor=%s"
            % (code, entry_date, f_ent, exit_date, f_ex)
        )
    return None


def risk_exit_session(trigger: Optional[str], sell_on: str) -> str:
    """Risk stops always next session open; time/weekday exits honor sell_on."""
    t = str(trigger or "")
    if t in ("stop_loss", "take_profit") or t.endswith("stop_loss") or t.endswith(
        "take_profit"
    ):
        return "open"
    return sell_on
