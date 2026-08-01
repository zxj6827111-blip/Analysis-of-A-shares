# -*- coding: utf-8 -*-
"""Deterministic natural-language interpretation for one backtest run.

The summary is deliberately rule based.  It explains the metrics that the
engine already calculates, without introducing an opaque model or changing
the numerical result of a backtest.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional


SUMMARY_VERSION = 1


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _metric(metrics: Mapping[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = _number(metrics.get(name))
        if value is not None:
            return value
    return None


def _context_value(context: Mapping[str, Any], *names: str) -> Any:
    """Read flattened repro fields and the nested repro/request fallbacks."""
    for name in names:
        if context.get(name) not in (None, ""):
            return context.get(name)
    repro = context.get("repro")
    if isinstance(repro, Mapping):
        for name in names:
            if repro.get(name) not in (None, ""):
                return repro.get(name)
        request = repro.get("request")
        if isinstance(request, Mapping):
            for name in names:
                if request.get(name) not in (None, ""):
                    return request.get(name)
    request = context.get("request")
    if isinstance(request, Mapping):
        for name in names:
            if request.get(name) not in (None, ""):
                return request.get(name)
    return None


def _percent(value: Optional[float], *, absolute: bool = False) -> Optional[str]:
    if value is None:
        return None
    number = abs(value) if absolute else value
    if abs(number) < 0.00005:
        number = 0.0
    return f"{number * 100:+.2f}%" if not absolute else f"{number * 100:.2f}%"


def _count(value: Optional[float]) -> Optional[int]:
    if value is None:
        return None
    return max(0, int(round(value)))


def _unique(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _blocked_summary(
    *,
    status: str,
    reason: Optional[str],
    notes: List[str],
) -> Dict[str, Any]:
    status_labels = {
        "no_go": "未通过",
        "failed": "失败",
        "rejected_unconfirmed_formula": "公式未确认",
    }
    label = status_labels.get(status, status or "未知")
    detail = str(reason or "").strip()
    if len(detail) > 180:
        detail = detail[:177] + "..."
    text = f"本次回测状态为“{label}”，暂未形成可用于评价策略收益的有效结果。"
    if detail:
        text += f"原因：{detail}。"
    text += "请先处理阻断原因后再解读收益、回撤和胜率。"
    warnings = ["当前结果不应被当作策略表现结论。"]
    if notes:
        warnings.append(str(notes[0])[:180])
    return {
        "version": SUMMARY_VERSION,
        "status": status,
        "level": "blocked",
        "headline": "本次回测暂不可评价",
        "text": text,
        "summary": text,
        "highlights": [],
        "warnings": _unique(warnings)[:4],
        "next_step": "先修复数据、公式或复权校验问题，再重新运行回测。",
        "confidence": "none",
        "confidence_label": "无有效样本",
    }


def build_backtest_summary(
    metrics: Optional[Mapping[str, Any]] = None,
    *,
    status: str = "ok",
    notes: Optional[Iterable[Any]] = None,
    context: Optional[Mapping[str, Any]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a short explanation plus structured supporting facts.

    Metric values follow the engine convention: return, drawdown and win rate
    are ratios (``0.12`` means 12%), while ``final_equity`` and ``cost_total``
    are currency amounts.
    """
    m: Dict[str, Any] = dict(metrics or {})
    c: Dict[str, Any] = dict(context or {})
    status = str(status or "ok")
    note_list = [str(x) for x in (notes or []) if str(x).strip()]

    if status in {"no_go", "failed", "rejected_unconfirmed_formula"}:
        return _blocked_summary(
            status=status,
            reason=reason or c.get("reason") or c.get("error"),
            notes=note_list,
        )

    total_return = _metric(m, "total_return", "mean_symbol_return")
    annual_return = _metric(m, "annual_return")
    max_drawdown = _metric(m, "max_drawdown")
    drawdown_abs = abs(max_drawdown) if max_drawdown is not None else None
    sharpe = _metric(m, "sharpe")
    win_rate = _metric(m, "win_rate")
    payoff = _metric(m, "payoff_ratio", "profit_loss_ratio")
    profit_factor = _metric(m, "profit_factor")
    n_round_trips = _count(_metric(m, "n_round_trips"))
    n_buys = _count(_metric(m, "n_buys"))
    n_sells = _count(_metric(m, "n_sells"))
    n_days = _count(_metric(m, "n_days"))
    final_equity = _metric(m, "final_equity")
    cost_total = _metric(m, "cost_total")
    cost_impact = _metric(m, "cost_impact")
    n_open = _count(_metric(m, "n_open_positions"))
    n_forced = _count(_metric(m, "n_forced_exits"))

    requested = _count(
        _number(_context_value(c, "codes_requested_count"))
    )
    excluded = _count(
        _number(
            _context_value(c, "coverage_excluded_count")
            or _metric(m, "coverage_excluded_count")
        )
    )
    research_unadjusted = bool(
        _context_value(c, "research_unadjusted", "research_raw")
    )
    research_unconfirmed = bool(
        _context_value(c, "research_unconfirmed_formula")
    )
    legacy_execution = bool(_context_value(c, "legacy_adjusted_execution"))

    # A run with no closed trades cannot support a win-rate or profitability
    # judgement, even when the equity curve happens to contain points.
    no_trades = (n_round_trips == 0) or (
        n_round_trips is None and (n_buys or 0) == 0 and (n_sells or 0) == 0
    )
    if no_trades:
        signal_count = _count(_number(_context_value(c, "n_events", "n_signals")))
        text = "本次回测没有形成有效平仓交易，暂时无法判断策略的盈利能力、胜率或风险收益比。"
        if signal_count is not None:
            text += f"虽然产生了 {signal_count} 个信号，但信号没有转化为可评价的成交结果。"
        text += "建议先检查信号日期、交易时段、持有期和股票池覆盖。"
        warnings = ["有效交易样本为 0，收益数字不具备解释意义。"]
        if excluded:
            suffix = f"（请求 {requested} 只，剔除 {excluded} 只）" if requested else ""
            warnings.append(f"股票池存在覆盖剔除{suffix}，请确认剩余样本是否符合预期。")
        return {
            "version": SUMMARY_VERSION,
            "status": status,
            "level": "insufficient",
            "headline": "交易样本不足，暂无法评价",
            "text": text,
            "summary": text,
            "highlights": [
                {"key": "round_trips", "label": "有效平仓", "value": "0", "tone": "muted"}
            ],
            "warnings": _unique(warnings)[:4],
            "next_step": "先确认策略确实产生了成交，再扩大区间做稳定性验证。",
            "confidence": "none",
            "confidence_label": "无有效交易样本",
        }

    # Overall judgement is intentionally coarse: it describes what to inspect
    # next instead of pretending that a backtest can prove future returns.
    if total_return is None:
        headline = "结果不完整，暂无法形成收益结论"
        level = "insufficient"
    elif total_return > 0 and (drawdown_abs is None or drawdown_abs <= 0.15) and (
        (sharpe is not None and sharpe >= 1.0)
        or (profit_factor is not None and profit_factor >= 1.3)
    ):
        headline = "收益为正，风险控制相对较好"
        level = "good"
    elif total_return > 0:
        headline = "收益为正，但稳定性仍需验证"
        level = "caution"
    elif total_return < 0:
        headline = "本次回测未盈利，需要优先排查"
        level = "weak"
    else:
        headline = "收益基本持平，尚未体现明显优势"
        level = "caution"

    parts: List[str] = []
    if total_return is not None:
        ret = _percent(total_return)
        annual = _percent(annual_return)
        sentence = f"累计收益 {ret}"
        if annual is not None:
            sentence += f"，年化收益 {annual}"
        if final_equity is not None:
            sentence += f"，期末权益 {final_equity:,.2f}"
        parts.append(sentence + "。")
    risk_sentence: List[str] = []
    if drawdown_abs is not None:
        risk_sentence.append(f"最大回撤 {_percent(drawdown_abs, absolute=True)}")
    if sharpe is not None:
        risk_sentence.append(f"Sharpe {sharpe:.2f}")
    if risk_sentence:
        parts.append("风险表现为" + "，".join(risk_sentence) + "。")

    trade_sentence = []
    if n_round_trips is not None:
        trade_sentence.append(f"完成 {n_round_trips} 笔平仓交易")
    if win_rate is not None:
        trade_sentence.append(f"胜率 {_percent(win_rate, absolute=True)}")
    if payoff is not None:
        trade_sentence.append(f"盈亏比 {payoff:.2f}")
    elif profit_factor is not None:
        trade_sentence.append(f"盈利因子 {profit_factor:.2f}")
    if trade_sentence:
        parts.append("交易层面" + "，".join(trade_sentence) + "。")

    if cost_impact is not None and abs(cost_impact) >= 0.0005:
        parts.append(
            f"按零成本对照，显性成本约使收益减少 {abs(cost_impact) * 100:.2f} 个百分点。"
        )
    elif cost_total is not None and cost_total > 0:
        parts.append(f"累计显性交易成本 {cost_total:,.2f}。")

    warnings: List[str] = []
    if n_round_trips is not None and n_round_trips < 30:
        warnings.append(f"交易样本仅 {n_round_trips} 笔，统计稳定性偏低。")
    if n_days is not None and n_days < 120:
        warnings.append(f"回测仅覆盖 {n_days} 个交易日，尚未覆盖足够市场阶段。")
    if drawdown_abs is not None and drawdown_abs >= 0.20:
        warnings.append(f"最大回撤达到 {_percent(drawdown_abs, absolute=True)}，回撤压力较高。")
    if sharpe is not None and sharpe < 0.5:
        warnings.append("风险调整后收益偏弱，单看累计收益可能高估策略质量。")
    if cost_impact is not None and cost_impact >= 0.02:
        warnings.append("交易成本侵蚀较明显，实盘成交和费率假设需要复核。")
    if n_forced:
        warnings.append(f"有 {n_forced} 笔强制退出，建议核对退出规则是否主导了结果。")
    if n_open:
        warnings.append(f"期末仍有 {n_open} 个未平仓头寸，期末权益含未实现波动。")
    if excluded:
        suffix = f"（请求 {requested} 只）" if requested else ""
        warnings.append(f"股票池剔除 {excluded} 只未覆盖标的{suffix}，结果代表剩余样本。")
    if research_unadjusted:
        warnings.append("当前为未复权研究口径，不应直接作为正式收益结论。")
    if research_unconfirmed:
        warnings.append("指标公式尚未完成正式确认，当前结果仅适合研究参考。")
    if legacy_execution:
        warnings.append("该任务使用历史成交口径，不能与新版 raw 成交口径直接比较。")
    if status == "unsupported_corporate_action":
        warnings.append("存在未支持的公司行为，正式收益结论需要先完成公司行为校验。")
    if n_round_trips is not None and n_round_trips >= 100 and (n_days or 0) >= 500:
        confidence = "high"
        confidence_label = "样本相对充分"
    elif n_round_trips is not None and n_round_trips >= 30 and (n_days or 0) >= 120:
        confidence = "medium"
        confidence_label = "样本基本可用"
    else:
        confidence = "low"
        confidence_label = "样本偏少"

    if total_return is not None and total_return < 0:
        next_step = "先拆解亏损来源和交易成本，再用样本外区间复测，暂不扩大仓位。"
    elif drawdown_abs is not None and drawdown_abs >= 0.20:
        next_step = "优先做仓位、止损和退出规则的压力测试，再比较收益是否值得承担该回撤。"
    elif confidence == "low":
        next_step = "扩大回测区间并覆盖不同市场阶段，确认结果不是少数交易造成的偶然现象。"
    else:
        next_step = "继续做样本外、基准对照和参数扰动测试，确认收益能否稳定复现。"

    highlights = []
    if total_return is not None:
        highlights.append({
            "key": "total_return", "label": "累计收益", "value": _percent(total_return),
            "tone": "positive" if total_return > 0 else "negative" if total_return < 0 else "muted",
        })
    if drawdown_abs is not None:
        highlights.append({
            "key": "max_drawdown", "label": "最大回撤", "value": _percent(drawdown_abs, absolute=True),
            "tone": "negative" if drawdown_abs >= 0.20 else "muted",
        })
    if win_rate is not None:
        highlights.append({
            "key": "win_rate", "label": "胜率", "value": _percent(win_rate, absolute=True),
            "tone": "positive" if win_rate >= 0.5 else "muted",
        })
    if n_round_trips is not None:
        highlights.append({
            "key": "round_trips", "label": "平仓交易", "value": str(n_round_trips), "tone": "muted",
        })

    if not parts:
        parts.append("当前指标不足以生成完整的收益解释。")
    text = "".join(parts)
    if warnings:
        text += " " + warnings[0]
    return {
        "version": SUMMARY_VERSION,
        "status": status,
        "level": level,
        "headline": headline,
        "text": text,
        "summary": text,
        "highlights": highlights,
        "warnings": _unique(warnings)[:5],
        "next_step": next_step,
        "confidence": confidence,
        "confidence_label": confidence_label,
    }


__all__ = ["SUMMARY_VERSION", "build_backtest_summary"]
