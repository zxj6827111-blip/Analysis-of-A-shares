# -*- coding: utf-8 -*-
"""L3 corporate-action event ledger (research scaffolding).

Formal policy default is fail_closed (see normalize_corporate_action_policy).

Factor jumps on Baostock-style cumulative foreAdjustFactor mix cash dividends
and share events. Inferring share_multiplier = f1/f0 and applying it to open
positions is **not** formal. This module may still:

- normalize policies
- optionally *audit-list* inferred jumps (research)
- apply **only** when events are explicit (cash_per_share / typed share events
  from a future external source) — never auto-apply factor_jump share ratios
  in the portfolio engine.

Policies:
- fail_closed: factor jump while open → unsupported (no apply)
- event_ledger: reserved opt-in; engine still fail_closed on factor jumps until
  explicit events exist (factor_jump share apply disabled)
- not_checked: skip CA checks (research)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Event types
CA_SHARE_RATIO = "share_ratio"  # split / bonus / capitalization
CA_CASH_DIVIDEND = "cash_dividend"
CA_RIGHTS_ISSUE = "rights_issue"
CA_CODE_CHANGE = "code_change"
CA_DELIST = "delist"
CA_SUSPENSION = "suspension"
CA_UNKNOWN = "unknown"
CA_FACTOR_JUMP_AUDIT = "factor_jump_audit"  # inferred only; never auto-applied

POLICY_FAIL_CLOSED = "fail_closed"
POLICY_EVENT_LEDGER = "event_ledger"
POLICY_NOT_CHECKED = "not_checked"

SUPPORTED_POLICIES = frozenset(
    {POLICY_FAIL_CLOSED, POLICY_EVENT_LEDGER, POLICY_NOT_CHECKED}
)

# Hard gate: portfolio engine must not restatement shares from factor jumps.
ALLOW_FACTOR_JUMP_SHARE_APPLY = False


@dataclass
class CorporateActionEvent:
    std_code: str
    date: int  # ex-date / effective trading date
    event_type: str
    share_multiplier: float = 1.0  # new_shares = old * multiplier
    cash_per_share: float = 0.0  # cash credited per share held pre-event (raw CNY)
    note: str = ""
    source: str = "factor_jump"
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_applyable(self) -> bool:
        """True only for explicit non-audit events with real economic payload."""
        if self.event_type == CA_FACTOR_JUMP_AUDIT:
            return False
        if self.source == "factor_jump" and not ALLOW_FACTOR_JUMP_SHARE_APPLY:
            return False
        if float(self.cash_per_share or 0) != 0.0:
            return True
        if abs(float(self.share_multiplier or 1.0) - 1.0) > 1e-12 and self.source != "factor_jump":
            return True
        return False


@dataclass
class LedgerApplyResult:
    shares: int
    cash_delta: float
    cost_basis_scale: float  # multiply position cost basis / entry_price ref
    events_applied: List[CorporateActionEvent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def normalize_corporate_action_policy(
    policy: Optional[str],
) -> Tuple[str, List[str], bool]:
    """Return (policy, notes, force_unsupported).

    ledger / ledger_factor_ratio aliases map to event_ledger with a warning that
    factor-jump share apply is disabled; formal default remains fail_closed when
    policy is empty.
    """
    p = str(policy or POLICY_FAIL_CLOSED).strip().lower()
    notes: List[str] = []
    force = False
    if p in ("ledger_factor_ratio", "ledger"):
        notes.append(
            "policy alias ledger/ledger_factor_ratio mapped to event_ledger "
            "(research only; factor-jump share apply disabled; use fail_closed for formal)."
        )
        p = POLICY_EVENT_LEDGER
    if p in ("fail", "unsupported"):
        p = POLICY_FAIL_CLOSED
    if p not in SUPPORTED_POLICIES:
        notes.append("unknown corporate_action_policy %r → fail_closed" % policy)
        p = POLICY_FAIL_CLOSED
    if p == POLICY_EVENT_LEDGER and not ALLOW_FACTOR_JUMP_SHARE_APPLY:
        notes.append(
            "event_ledger: factor-jump share restatement disabled; "
            "open holdings still fail_closed on residual factor jumps until "
            "explicit CA events exist."
        )
    return p, notes, force


def events_from_factor_series(
    series: Any,
    *,
    lot_size: int = 100,
    for_apply: bool = False,
) -> List[CorporateActionEvent]:
    """List factor steps as audit events (not formal share restatements).

    When for_apply=True and ALLOW_FACTOR_JUMP_SHARE_APPLY is False, returns [].
    Default builds CA_FACTOR_JUMP_AUDIT rows for research manifests only.
    """
    if for_apply and not ALLOW_FACTOR_JUMP_SHARE_APPLY:
        return []
    code = str(getattr(series, "std_code", "") or "")
    ed = list(getattr(series, "event_dates", None) or [])
    ef = list(getattr(series, "event_factors", None) or [])
    if not ed or not ef or len(ed) != len(ef):
        return []
    out: List[CorporateActionEvent] = []
    prev = None
    for d, f in zip(ed, ef):
        try:
            di, fv = int(d), float(f)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(fv) or fv <= 0:
            continue
        if prev is None:
            prev = fv
            continue
        if abs(fv - prev) <= 1e-12:
            prev = fv
            continue
        mult = float(fv / prev) if prev != 0 else 1.0
        if not np.isfinite(mult) or mult <= 0:
            prev = fv
            continue
        if abs(mult - 1.0) > 1e-9:
            out.append(
                CorporateActionEvent(
                    std_code=code,
                    date=di,
                    event_type=CA_FACTOR_JUMP_AUDIT
                    if not ALLOW_FACTOR_JUMP_SHARE_APPLY
                    else CA_SHARE_RATIO,
                    share_multiplier=mult,
                    cash_per_share=0.0,
                    note="audit-only factor jump %.8g → %.8g (not applied as shares)"
                    % (prev, fv)
                    if not ALLOW_FACTOR_JUMP_SHARE_APPLY
                    else "inferred from cumulative factor %.8g → %.8g" % (prev, fv),
                    source="factor_jump",
                    meta={
                        "factor_before": prev,
                        "factor_after": fv,
                        "lot_size": lot_size,
                        "applyable": ALLOW_FACTOR_JUMP_SHARE_APPLY,
                    },
                )
            )
        prev = fv
    return out


def build_events_by_code(
    factor_series: Optional[Sequence[Any]],
    *,
    extra_events: Optional[Sequence[CorporateActionEvent]] = None,
    for_apply: bool = False,
) -> Dict[str, List[CorporateActionEvent]]:
    """{std_code: sorted events}. Factor-inferred rows are audit-only by default."""
    by: Dict[str, List[CorporateActionEvent]] = {}
    if factor_series:
        for fs in factor_series:
            code = str(getattr(fs, "std_code", "") or "")
            if not code:
                continue
            evs = events_from_factor_series(fs, for_apply=for_apply)
            if evs:
                by.setdefault(code, []).extend(evs)
    if extra_events:
        for e in extra_events:
            by.setdefault(e.std_code, []).append(e)
    for code in by:
        by[code] = sorted(by[code], key=lambda x: (int(x.date), x.event_type))
    return by


def events_between(
    events_by_code: Dict[str, List[CorporateActionEvent]],
    code: str,
    *,
    after_date: int,
    on_or_before: int,
) -> List[CorporateActionEvent]:
    """Events with after_date < event.date <= on_or_before."""
    out = []
    for e in events_by_code.get(code) or []:
        d = int(e.date)
        if d > int(after_date) and d <= int(on_or_before):
            out.append(e)
    return out


def apply_events_to_position(
    *,
    shares: int,
    cash: float,
    entry_price: float,
    events: Sequence[CorporateActionEvent],
    lot_size: int = 100,
) -> LedgerApplyResult:
    """Apply chronological CA events to open position (raw cash/shares).

    Skips non-applyable / factor_jump audit events.
    """
    sh = int(shares)
    c = float(cash)
    px = float(entry_price)
    applied: List[CorporateActionEvent] = []
    notes: List[str] = []
    cost_scale = 1.0
    for e in events:
        if not getattr(e, "is_applyable", True):
            notes.append(
                "skip non-applyable CA %s d=%s type=%s source=%s"
                % (e.std_code, e.date, e.event_type, e.source)
            )
            continue
        if sh <= 0:
            break
        if e.event_type == CA_CASH_DIVIDEND or float(e.cash_per_share or 0) != 0.0:
            delta = float(sh) * float(e.cash_per_share or 0.0)
            c += delta
            notes.append("cash_div %s d=%s +%.4f" % (e.std_code, e.date, delta))
            applied.append(e)
        mult = float(e.share_multiplier or 1.0)
        if e.event_type in (CA_SHARE_RATIO, CA_RIGHTS_ISSUE) or (
            abs(mult - 1.0) > 1e-12 and e.event_type != CA_CASH_DIVIDEND
        ):
            if e.source == "factor_jump" and not ALLOW_FACTOR_JUMP_SHARE_APPLY:
                notes.append(
                    "skip factor_jump share_ratio %s d=%s" % (e.std_code, e.date)
                )
                continue
            if abs(mult - 1.0) > 1e-12 and mult > 0:
                new_sh = int(sh * mult)
                if lot_size > 1:
                    new_sh = (new_sh // lot_size) * lot_size
                if new_sh < 0:
                    new_sh = 0
                notes.append(
                    "share_ratio %s d=%s %d→%d x%.8g"
                    % (e.std_code, e.date, sh, new_sh, mult)
                )
                sh = new_sh
                px = px / mult if mult != 0 else px
                cost_scale *= 1.0 / mult if mult != 0 else 1.0
                applied.append(e)
    return LedgerApplyResult(
        shares=sh,
        cash_delta=c - float(cash),
        cost_basis_scale=cost_scale,
        events_applied=applied,
        notes=notes,
    )


def apply_day_events_to_open_positions(
    *,
    positions: Dict[str, Any],
    cash: float,
    day: int,
    events_by_code: Dict[str, List[CorporateActionEvent]],
    last_applied: Dict[str, int],
    lot_size: int = 100,
) -> Tuple[float, List[str], int]:
    """Apply applyable events for ``day``; factor_jump audit events are skipped.

    Returns (cash, notes, n_applied). With ALLOW_FACTOR_JUMP_SHARE_APPLY=False
    and only factor-inferred events, n_applied stays 0.
    """
    notes: List[str] = []
    n = 0
    c = float(cash)
    for code, pos in list(positions.items()):
        prev = int(last_applied.get(code) or getattr(pos, "entry_date", 0) or 0)
        day_evs = [
            e
            for e in (events_by_code.get(code) or [])
            if int(e.date) == int(day) and int(e.date) > prev
        ]
        if not day_evs:
            continue
        applyable = [e for e in day_evs if e.is_applyable]
        if not applyable:
            for e in day_evs:
                notes.append(
                    "ca_audit_only %s d=%s type=%s (not applied)"
                    % (code, e.date, e.event_type)
                )
            continue
        sh0 = int(getattr(pos, "shares", 0) or 0)
        px0 = float(getattr(pos, "entry_price", 0) or 0)
        res = apply_events_to_position(
            shares=sh0,
            cash=0.0,
            entry_price=px0,
            events=applyable,
            lot_size=lot_size,
        )
        if res.shares != sh0:
            pos.shares = res.shares
        if abs(res.cost_basis_scale - 1.0) > 1e-15 and px0:
            pos.entry_price = px0 * res.cost_basis_scale
            if hasattr(pos, "cost") and pos.cost is not None:
                try:
                    pos.cost = float(pos.cost) * res.cost_basis_scale
                except (TypeError, ValueError):
                    pass
        if res.cash_delta:
            c += res.cash_delta
        if res.events_applied:
            last_applied[code] = int(day)
            n += len(res.events_applied)
        notes.extend(res.notes)
    return c, notes, n


def ledger_manifest_sha(events_by_code: Dict[str, List[CorporateActionEvent]]) -> str:
    import hashlib

    payload = {
        k: [e.to_dict() for e in v]
        for k, v in sorted(events_by_code.items(), key=lambda x: x[0])
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
