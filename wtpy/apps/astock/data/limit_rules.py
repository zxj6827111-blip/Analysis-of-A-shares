"""Limit-up / limit-down rules (extensible) and suspension helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class LimitContext:
    std_code: str
    date: int
    prev_close: float
    open: float
    high: float
    low: float
    close: float
    is_st: Optional[bool] = None  # unknown when history missing
    board: Optional[str] = None  # main / chinext / star / unknown


class LimitRuleProvider(ABC):
    """Extensible interface for A-share price-limit checks."""

    @abstractmethod
    def limit_pct(self, ctx: LimitContext) -> float:
        """Return absolute limit ratio, e.g. 0.10 for 10%."""

    def limit_up_price(self, ctx: LimitContext) -> float:
        return round(ctx.prev_close * (1.0 + self.limit_pct(ctx)), 2)

    def limit_down_price(self, ctx: LimitContext) -> float:
        return round(ctx.prev_close * (1.0 - self.limit_pct(ctx)), 2)

    def is_limit_down_untradeable(self, ctx: LimitContext) -> bool:
        """True if sell cannot execute (one-word limit-down approximation).

        Without full L2 / queue data we treat: open == low == close == limit_down
        and high <= limit_down + epsilon as untradeable limit-down.
        """
        if ctx.prev_close <= 0:
            return False
        ld = self.limit_down_price(ctx)
        # prices at/under limit-down board
        at_floor = (
            abs(ctx.open - ld) <= 0.011
            and abs(ctx.close - ld) <= 0.011
            and abs(ctx.low - ld) <= 0.011
            and ctx.high <= ld + 0.011
        )
        return at_floor

    def is_limit_up_unbuyable(self, ctx: LimitContext) -> bool:
        if ctx.prev_close <= 0:
            return False
        lu = self.limit_up_price(ctx)
        at_ceil = (
            abs(ctx.open - lu) <= 0.011
            and abs(ctx.close - lu) <= 0.011
            and abs(ctx.high - lu) <= 0.011
            and ctx.low >= lu - 0.011
        )
        return at_ceil


class DefaultAShareLimitRule(LimitRuleProvider):
    """Default 10%/20%/30% rules with explicit metadata gap.

    Historical ST status and board classification are incomplete in v1;
    when unknown we assume main-board 10%. This is documented as a boundary.
    北交所（BSE）30% 涨跌幅不设 ST 特例（北交所 *ST 仍执行 30%）。
    """

    BOUNDARY_NOTE = (
        "ST/board history incomplete: when is_st/board unknown, assume 10% main-board "
        "limit. ChiNext/STAR 20% / BSE(北交所) 30% only applied when board is known. "
        "Results near limits are approximate."
    )

    def limit_pct(self, ctx: LimitContext) -> float:
        board = (ctx.board or "").lower()
        code = ctx.std_code.split(".")[-1]
        # 北交所全代码段（43/83/87 老段 + 920 迁移段）30%，无 ST 5% 特例
        if (
            board in ("bse", "bjse", "bjs")
            or ctx.std_code.startswith("BSE.")
            or code.startswith(("92", "43", "83", "87"))
        ):
            return 0.30
        if ctx.is_st is True:
            return 0.05
        if board in ("chinext", "cyb") or code.startswith(("300", "301")):
            return 0.20
        if board in ("star", "kcb") or code.startswith(("688", "689")):
            return 0.20
        return 0.10


def infer_board(std_code: str) -> str:
    if std_code.startswith("BSE."):
        return "bse"
    code = std_code.split(".")[-1]
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("92", "43", "83", "87")):
        return "bse"
    return "main"
