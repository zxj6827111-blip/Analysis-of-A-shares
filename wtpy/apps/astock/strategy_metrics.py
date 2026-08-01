# -*- coding: utf-8 -*-
"""Portfolio backtest metrics from equity curve and fills."""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .config import CostConfig
from .strategy_models import EquityPoint, Fill

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
                position_basis = getattr(f, "position_cost_basis", None)
                ca_cash = float(
                    getattr(f, "corporate_action_cash_received", 0.0) or 0.0
                )
                if position_basis is not None:
                    pnl = (
                        float(f.amount)
                        - f.commission
                        - f.stamp_tax
                        - float(position_basis)
                        + ca_cash
                    )
                else:
                    pnl = (
                        (f.price - b.price) * f.shares
                        - f.commission
                        - f.stamp_tax
                        - b.commission
                    )
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
