# -*- coding: utf-8 -*-
"""Delisted-position exit policy — Gate B5.

Solves the infinite-holding problem: without this policy a stock whose bars
end (delisting) stays in the book forever, valued at its last close
("suspended" defer loop). With a policy active, the first simulated trading
day AFTER the stock's last tradable day forces a terminal exit.

Rule (DELIST_EXIT_RULE_VERSION = delist_exit_v1):
  - 退市整理期participates normally: while bars exist, ordinary buy/sell/limit
    rules apply, including the last tradable day itself (sell allowed).
  - last tradable day = bar-derived last_trade_date from the point-in-time
    universe (covers 停牌至退市: the last REAL bar is the reference).
  - terminal exit fires on the first sim date > last_trade_date, at a
    scenario price derived from the last tradable close:
      last_tradable_price   -> last_close * 1.0
      discounted_recovery   -> last_close * recovery_discount (configurable)
      zero_recovery         -> 0.0
  - terminal exit is a book-out, not a market trade: by default no
    commission / stamp tax / slippage is applied (apply_costs=False).
  - proceeds are credited to cash; the position leaves the book, so account
    market value never retains a dead stock.
  - the fill is marked reason=delist_terminal_exit with scenario fields;
    realized loss versus position cost is aggregated separately in metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

DELIST_EXIT_RULE_VERSION = "delist_exit_v1"

SCENARIO_LAST_TRADABLE_PRICE = "last_tradable_price"
SCENARIO_DISCOUNTED_RECOVERY = "discounted_recovery"
SCENARIO_ZERO_RECOVERY = "zero_recovery"
ALL_SCENARIOS = (
    SCENARIO_LAST_TRADABLE_PRICE,
    SCENARIO_DISCOUNTED_RECOVERY,
    SCENARIO_ZERO_RECOVERY,
)

EXIT_REASON_DELIST_TERMINAL = "delist_terminal_exit"

DEFAULT_RECOVERY_DISCOUNT = 0.5


@dataclass(frozen=True)
class DelistExitPolicy:
    scenario: str = SCENARIO_LAST_TRADABLE_PRICE
    recovery_discount: float = DEFAULT_RECOVERY_DISCOUNT
    apply_costs: bool = False
    rule_version: str = DELIST_EXIT_RULE_VERSION

    def recovery_rate(self) -> float:
        if self.scenario == SCENARIO_LAST_TRADABLE_PRICE:
            return 1.0
        if self.scenario == SCENARIO_DISCOUNTED_RECOVERY:
            return float(self.recovery_discount)
        return 0.0

    def terminal_price(self, last_tradable_close: float) -> float:
        return float(last_tradable_close) * self.recovery_rate()

    def to_meta(self) -> dict:
        return {
            "delist_exit_rule_version": self.rule_version,
            "delist_exit_scenario": self.scenario,
            "delist_recovery_discount": float(self.recovery_discount),
            "delist_exit_apply_costs": bool(self.apply_costs),
        }


def normalize_delist_policy(
    scenario: Optional[str],
    recovery_discount: Optional[float] = None,
) -> Tuple[DelistExitPolicy, List[str]]:
    """Validate + normalize request inputs into a policy.

    Raises ValueError on unknown scenario or out-of-range discount — the
    request layer maps this to 4xx; never a silent default for bad input.
    An empty/None scenario means the standard scenario (last_tradable_price).
    """
    notes: List[str] = []
    s = (scenario or SCENARIO_LAST_TRADABLE_PRICE).strip().lower()
    if s not in ALL_SCENARIOS:
        raise ValueError(
            f"unknown delist_exit_scenario: {scenario!r}; "
            f"expected one of {ALL_SCENARIOS}"
        )
    d = DEFAULT_RECOVERY_DISCOUNT if recovery_discount is None else float(recovery_discount)
    if not (0.0 <= d <= 1.0):
        raise ValueError(
            f"delist_recovery_discount must be within [0, 1], got {recovery_discount}"
        )
    if s != SCENARIO_DISCOUNTED_RECOVERY and recovery_discount is not None:
        notes.append(
            f"delist_recovery_discount={recovery_discount} ignored for scenario={s}"
        )
    return DelistExitPolicy(scenario=s, recovery_discount=d), notes
