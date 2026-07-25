# -*- coding: utf-8 -*-
"""Shared three-plane (signal / trade / corporate-action) semantics for repro.

Single source of truth for L1/L2/L3 documentation embedded in run_meta.repro.
Keep strings here; reports/UI should prefer these helpers over free-text copies.
"""

from __future__ import annotations

from typing import Any, Dict

# Plane identifiers (stable for repro / API)
PLANE_SIGNAL = "L1_signal_price"
PLANE_TRADE = "L2_trade_price"
PLANE_CORPORATE_ACTION = "L3_corporate_action_ledger"

# bagua OHLC source after P1
BAGUA_OHLC_PLANE = PLANE_TRADE  # 卦象位和用未复权周K，与信号L1分离
BAGUA_OHLC_SOURCE = "week_bars_from_raw_days"

# Formal defaults (must match backtest_context.apply_standard_qfq_raw_execution_v2)
DEFAULT_SIGNAL_PRICE_MODE = "asof_forward_qfq"
DEFAULT_SIGNAL_ANCHOR = "run_end"  # batch BT: factor_asof = factor on/before run end
DEFAULT_CORPORATE_ACTION_POLICY = "fail_closed"
ENGINE_RESULT_VERSION = "asof_qfq_signal_raw_execution_v3"


def three_plane_repro_fields(
    *,
    signal_price_mode: str,
    execution_price_mode: str = "raw",
    valuation_price_mode: str = "raw",
    corporate_action_policy: str = DEFAULT_CORPORATE_ACTION_POLICY,
    bagua_ohlc_plane: str = BAGUA_OHLC_PLANE,
    bagua_ohlc_source: str = BAGUA_OHLC_SOURCE,
    signal_anchor: str = DEFAULT_SIGNAL_ANCHOR,
) -> Dict[str, Any]:
    """Fields embedded in run_meta.repro for three-plane documentation."""
    return {
        "price_planes": {
            "L1_signal_price": {
                "id": PLANE_SIGNAL,
                "uses": signal_price_mode,
                "default_formal": DEFAULT_SIGNAL_PRICE_MODE,
                "signal_anchor": signal_anchor,
                "responsible_for": [
                    "indicators",
                    "patterns",
                    "trend",
                    "technical_signals",
                                    ],
                "note": (
                    "Continuous adjusted OHLC for signal generation only. "
                    "Formal default: asof_forward_qfq = factor_t / factor_asof. "
                    "Batch backtest anchors asof at run end (numerically equals "
                    "standard_qfq on a fixed snapshot ending at end). "
                    "Bagua query can anchor at query date (true asof). "
                    "Does not size shares or debit cash."
                ),
            },
            "L2_trade_price": {
                "id": PLANE_TRADE,
                "uses": execution_price_mode,
                "valuation_uses": valuation_price_mode,
                "responsible_for": [
                    "fills",
                    "slippage",
                    "limit_up_down",
                    "fees",
                    "position_mark_to_market",
                    "cash_ledger",
                    "bagua_ohlc_features",
                ],
                "note": (
                    "Unadjusted market OHLC only. Buy/sell price columns and equity use this plane."
                ),
            },
            "L3_corporate_action_ledger": {
                "id": PLANE_CORPORATE_ACTION,
                "policy": corporate_action_policy,
                "default_formal": DEFAULT_CORPORATE_ACTION_POLICY,
                "responsible_for": [
                    "cash_dividend",
                    "bonus_share",
                    "capitalization_issue",
                    "rights_issue",
                    "split_merge",
                    "code_change",
                    "delist",
                    "suspension",
                ],
                # Factor-jump share apply is NOT formal: cash div also moves factors.
                "implemented": False,
                "factor_jump_share_apply": False,
                "explicit_events_required": True,
                "note": (
                    "Formal default fail_closed: open position across a cumulative "
                    "factor jump → unsupported_corporate_action (no silent restatement). "
                    "event_ledger is opt-in research only and still does not invent "
                    "cash dividends from factors alone; do not treat factor jumps as "
                    "share multipliers for formal P&L."
                ),
            },
        },
        "bagua_ohlc_plane": bagua_ohlc_plane,
        "bagua_ohlc_source": bagua_ohlc_source,
        "bagua_ohlc_note": (
            "Bagua (week gua / 变卦) uses L2 unadjusted week OHLC covering the signal date. "
            "Technical signals still use L1 asof_forward_qfq. Planes are intentionally split."
        ),
        "signal_anchor": signal_anchor,
    }


THREE_PLANE_SUMMARY_ZH = (
    "【三平面价格架构】"
    "L1 信号价格层：指标/形态/趋势/技术信号/卦象OHLC 使用 asof_forward_qfq"
    "（批回测锚点=回测截止日 run_end；与同快照 standard_qfq 数值同构；"
    "查卦可选查询日 asof；research_unadjusted 时为未复权）。"
    "L2 真实交易价格层：买卖成交/滑点/涨跌停/费用/持仓估值/资金占用仅用未复权 OHLC。"
    "L3 公司行为账本层：正式默认 fail_closed（持仓跨因子跳变则 unsupported，不改股数）；"
    "真实现金分红/送转需显式事件源，禁止仅用累计因子虚构账本。"
    "主表买入价/卖出价=L2；买入价_普通前复权参考等=L1 审计。"
)

PRICE_MODE_NOTE_V3 = (
    "asof_qfq_signal_raw_execution_v3 + three planes: "
    "L1 signals+bagua use asof_forward_qfq anchored at run_end "
    "(or raw if research_unadjusted); "
    "L2 fills/equity use unadjusted market prices; "
    "L3 formal default fail_closed (no factor-jump share restatement). "
    "point_in_time_adjusted remains research/audit reference only. "
    "Legacy price_mode=adjusted / dual_price_v1+causal_qfq signal: obsolete."
)

# Back-compat alias
PRICE_MODE_NOTE_V2 = PRICE_MODE_NOTE_V3


def price_explanation_zh_for_excel(repro: Dict[str, Any]) -> str:
    """One-line price说明 for Excel 汇总 sheet."""
    pm = str(repro.get("price_mode") or "")
    er = str(repro.get("engine_result_version") or "")
    v3ish = pm in (
        "asof_qfq_signal_raw_execution_v3",
        "standard_qfq_signal_raw_execution_v2",
        "dual_price_v1",
        "dual_price",
    ) or er in (
        "asof_qfq_signal_raw_execution_v3",
        "standard_qfq_signal_raw_execution_v2",
        "dual_price_v1",
    )
    if v3ish:
        return (
            "三平面：L1信号(指标+卦象)=时点前复权asof_forward_qfq（批回测锚点=截止日；"
            "审计列 ordinary standard_qfq）；L2成交/估值=未复权；"
            "L3正式默认fail_closed（不按累计因子改股数）。"
            "买入价/卖出价=L2真实成交价（含滑点）；买入价_普通前复权参考=L1审计；"
            "买入价_起点锚定研究参考=研究审计价（不参与股数/费用/权益）。"
        )
    if repro.get("research_unadjusted"):
        return "研究未复权：信号亦用未复权K线；成交/估值仍为未复权真实价格。"
    return "见 price_mode / engine_result_version。"
