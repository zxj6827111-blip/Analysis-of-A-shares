# -*- coding: utf-8 -*-
"""Experiment center MVP (Stage E): expand param grid, queue variants, aggregate results."""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..bagua.filter_rules import BEST3, GuaFilter
from ..config import AStockConfig, get_default_config
from .backtest import BacktestRequest, BacktestService
from . import db as exp_db

# Soft cap — UI must warn; hard refuse above this unless force=True
DEFAULT_MAX_VARIANTS = 50
HARD_MAX_VARIANTS = 500  # Phase2: soft DEFAULT=50; force required above max_variants; hard refuse >500

# Weekday schedule templates (UI labels → engine fields)
WEEKDAY_TEMPLATES = {
    "fri_signal_mon_buy_thu_exit": {
        "label": "仅周五信号·周一买·周四平",
        "signal_weekdays": [5],
        "buy_weekday": 1,
        "exit_weekday": 4,
        "buy_on": "open",
        "sell_on": "open",
        "entry_lag": 1,
        "hold": 1,
    },
    "all_signal_tn12": {
        "label": "不限信号·经典T+1开/T+2收",
        "signal_weekdays": None,
        "buy_weekday": None,
        "exit_weekday": None,
        "buy_on": "open",
        "sell_on": "close",
        "entry_lag": 1,
        "hold": 1,
    },
    "fri_signal_fri_buy_mon_exit": {
        "label": "仅周五信号·周五买·下周一平",
        "signal_weekdays": [5],
        "buy_weekday": 5,
        "exit_weekday": 1,
        "buy_on": "open",
        "sell_on": "open",
        "entry_lag": 1,
        "hold": 1,
    },
}

GUA_PRESETS = {
    "none": {
        "label": "无卦象",
        "gua_filter": {
            "enabled": False,
            "selection_mode": "none",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": [],
            "selected_action_signals": [],
        },
        "with_bagua": False,
    },
    "best3": {
        "label": "最佳3爻",
        "gua_filter": {
            "enabled": True,
            "selection_mode": "exact_line",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": ["24-1", "46-1", "11-1"],
            "selected_action_signals": [],
        },
        "with_bagua": True,
    },
    "bull": {
        "label": "偏多操作信号",
        "gua_filter": {
            "enabled": True,
            "selection_mode": "action_signal",
            "selected_main_hexagram_ids": [],
            "selected_state_ids": [],
            "selected_action_signals": ["新开仓", "加仓"],
        },
        "with_bagua": True,
    },
}



# ---------------------------------------------------------------------------
# Phase2 free axes (signal / buy / sell independent) + legacy template path
# Templates in WEEKDAY_TEMPLATES remain presets only; free axes ignore them.
# ---------------------------------------------------------------------------

_FREE_AXIS_KEYS = (
    "signal_weekdays_options",
    "buy_options",
    "sell_options",
    "take_profit_list",
)


def _has_free_axes(payload_or_kwargs: dict) -> bool:
    """True if any independent-axis field is explicitly provided (non-empty)."""
    for k in _FREE_AXIS_KEYS:
        if k not in payload_or_kwargs:
            continue
        v = payload_or_kwargs.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)) and len(v) == 0:
            continue
        return True
    return False


def _normalize_signal_weekdays(val: Any) -> Optional[List[int]]:
    """None / [] / list of ints — empty list means unrestricted (None)."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        if len(val) == 0:
            return None
        out: List[int] = []
        for x in val:
            if x is None:
                continue
            out.append(int(x))
        return out or None
    return [int(val)]


def _resolve_buy_option(opt: dict) -> dict:
    o = dict(opt or {})
    buy_on = str(o.get("buy_on") or "open").lower()
    if buy_on not in ("open", "close"):
        buy_on = "open"
    entry_lag = o.get("entry_lag")
    if entry_lag is None:
        entry_lag = 1
    return {
        "buy_weekday": o.get("buy_weekday"),
        "buy_on": buy_on,
        "entry_lag": int(entry_lag),
        "hold": int(o.get("hold") or 1),
    }


def _resolve_sell_option(opt: dict) -> dict:
    o = dict(opt or {})
    sell_on = str(o.get("sell_on") or "open").lower()
    if sell_on not in ("open", "close"):
        sell_on = "open"
    hold = o.get("hold")
    return {
        "exit_weekday": o.get("exit_weekday"),
        "sell_on": sell_on,
        "hold": int(hold) if hold is not None else None,
    }


def _payload_to_parameter_space(payload: dict):
    """Build research.ParameterSpace from API/experiment payload. None if research unavailable."""
    try:
        from ..research.models import ParameterSpace
        from ..research.parameter_space import axes_from_legacy_templates
    except Exception:
        return None

    axes = {
        "signal_weekdays_options": payload.get("signal_weekdays_options"),
        "buy_options": payload.get("buy_options"),
        "sell_options": payload.get("sell_options"),
        "take_profit_list": payload.get("take_profit_list"),
    }
    use_free = _has_free_axes(axes)
    rule_ids = list(payload.get("rule_ids") or [])
    gua_keys = list(payload.get("gua_keys") or ["none"])
    holiday_policy = payload.get("holiday_policy") or "next_trading_day"
    period = payload.get("period") or "DAY"
    codes = payload.get("codes")
    start = payload.get("start")
    end = payload.get("end")
    account_mode = payload.get("account_mode") or "portfolio"
    research_unadjusted = bool(payload.get("research_unadjusted"))
    stop_loss_list = payload.get("stop_loss_list")
    take_profit_list = payload.get("take_profit_list")

    if use_free:
        sig = payload.get("signal_weekdays_options")
        if sig is None:
            signal_weekdays = [None]
        else:
            signal_weekdays = list(sig) if sig else [None]
        buy_modes = list(payload.get("buy_options") or [{"entry_lag": 1, "buy_on": "open"}])
        sell_modes = list(payload.get("sell_options") or [{"sell_on": "close"}])
        return ParameterSpace(
            rule_ids=rule_ids,
            period=period,
            signal_weekdays=signal_weekdays,
            buy_modes=buy_modes,
            sell_modes=sell_modes,
            gua_keys=gua_keys,
            stop_loss_list=list(stop_loss_list if stop_loss_list is not None else [None]),
            take_profit_list=list(take_profit_list if take_profit_list is not None else [None]),
            holiday_policy=holiday_policy,
            codes=list(codes) if codes else None,
            start=start,
            end=end,
            account_mode=account_mode,
            research_unadjusted=research_unadjusted,
        )

    # Legacy weekday_keys templates
    wd = payload.get("weekday_keys") or ["all_signal_tn12"]
    try:
        return axes_from_legacy_templates(
            rule_ids=rule_ids,
            weekday_keys=list(wd),
            gua_keys=gua_keys,
            stop_loss_list=stop_loss_list,
            take_profit_list=take_profit_list,
            period=period,
            codes=codes,
            start=start,
            end=end,
            account_mode=account_mode,
            research_unadjusted=research_unadjusted,
            holiday_policy=holiday_policy,
        )
    except Exception:
        return None


def _try_research_plan(payload: dict):
    """If research.planner.plan_experiment is available, prefer it.

    Normalizes planner keys to theoretical/rejected/actual/variants/...
    """
    try:
        from ..research.planner import plan_experiment
        from ..research.planner import HARD_MAX_VARIANTS as R_HARD
    except Exception:
        return None
    space = _payload_to_parameter_space(payload)
    if space is None:
        return None
    max_v = int(payload.get("max_variants") or DEFAULT_MAX_VARIANTS)
    force = bool(payload.get("force"))
    # Align hard max with experiments module for API consistency
    hard = int(payload.get("hard_max") or HARD_MAX_VARIANTS)
    try:
        plan = plan_experiment(space, max_variants=max_v, force=force, hard_max=hard)
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    # Normalize key names
    theoretical = int(plan.get("theoretical_count") or plan.get("theoretical") or 0)
    rejected = int(plan.get("rejected_count") or plan.get("rejected") or 0)
    actual = int(plan.get("actual_count") or plan.get("actual") or 0)
    variants = list(plan.get("variants") or [])
    preview = list(plan.get("preview") or [])
    reasons = dict(plan.get("rejection_reasons") or {})
    msg = plan.get("error") or plan.get("message")
    return {
        "theoretical": theoretical,
        "rejected": rejected,
        "actual": actual,
        "rejection_reasons": reasons,
        "variants": variants,
        "preview": preview,
        "message": msg,
        "truncated": plan.get("truncated"),
        "hard_max": plan.get("hard_max", hard),
        "max_variants": plan.get("max_variants", max_v),
        "force": force,
        "source": "research.planner",
    }


def expand_param_grid_unified(
    *,
    rule_ids: Sequence[str],
    gua_keys: Sequence[str] = ("none",),
    weekday_keys: Optional[Sequence[str]] = None,
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    signal_weekdays_options: Optional[Sequence[Any]] = None,
    buy_options: Optional[Sequence[dict]] = None,
    sell_options: Optional[Sequence[dict]] = None,
    take_profit_list: Optional[Sequence[Optional[float]]] = None,
    holiday_policy: str = "next_trading_day",
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    collect_rejections: bool = False,
) -> Dict[str, Any]:
    """Expand grid from free axes OR legacy weekday_keys templates.

    When any free axis is provided, weekday_keys templates are ignored.
    Returns dict with theoretical/rejected/actual/rejection_reasons/variants.
    """
    axes_payload = {
        "signal_weekdays_options": signal_weekdays_options,
        "buy_options": buy_options,
        "sell_options": sell_options,
        "take_profit_list": take_profit_list,
    }
    use_free = _has_free_axes(axes_payload)

    rules = list(rule_ids or [])
    if not rules:
        raise ValueError("rule_ids required")
    guas = list(gua_keys or ["none"])
    sls = list(stop_loss_list if stop_loss_list is not None else [None])
    tps = list(take_profit_list if take_profit_list is not None else [None])

    for g in guas:
        if g not in GUA_PRESETS:
            raise ValueError(f"unknown gua preset: {g}")

    rejection_reasons: Dict[str, int] = {}
    variants: List[Dict[str, Any]] = []
    theoretical = 0
    rejected = 0

    def _rej(reason: str) -> None:
        nonlocal rejected
        rejected += 1
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    base_codes = list(codes) if codes else ["sh600000", "sz000001"]

    if use_free:
        sig_opts = list(signal_weekdays_options) if signal_weekdays_options is not None else [None]
        if not sig_opts:
            sig_opts = [None]
        buy_opts = list(buy_options) if buy_options is not None else [
            {"entry_lag": 1, "buy_on": "open"}
        ]
        if not buy_opts:
            buy_opts = [{"entry_lag": 1, "buy_on": "open"}]
        sell_opts = list(sell_options) if sell_options is not None else [
            {"sell_on": "close"}
        ]
        if not sell_opts:
            sell_opts = [{"sell_on": "close"}]

        for rule_id, gkey, sig, bopt, sopt, sl, tp in itertools.product(
            rules, guas, sig_opts, buy_opts, sell_opts, sls, tps
        ):
            theoretical += 1
            g = GUA_PRESETS[gkey]
            b = _resolve_buy_option(bopt if isinstance(bopt, dict) else {})
            s = _resolve_sell_option(sopt if isinstance(sopt, dict) else {})

            if isinstance(sig, dict):
                sw = _normalize_signal_weekdays(sig.get("signal_weekdays"))
            else:
                sw = _normalize_signal_weekdays(sig)

            buy_wd = b.get("buy_weekday")
            exit_wd = s.get("exit_weekday")
            buy_on = b.get("buy_on") or "open"
            sell_on = s.get("sell_on") or "open"
            entry_lag = int(b.get("entry_lag") if b.get("entry_lag") is not None else 1)
            hold = int(b.get("hold") or s.get("hold") or 1)

            if buy_wd is not None:
                try:
                    bw = int(buy_wd)
                except (TypeError, ValueError):
                    _rej("invalid_buy_weekday")
                    continue
                if bw < 1 or bw > 5:
                    _rej("buy_weekday_out_of_range")
                    continue
                buy_wd = bw
            if exit_wd is not None:
                try:
                    ew = int(exit_wd)
                except (TypeError, ValueError):
                    _rej("invalid_exit_weekday")
                    continue
                if ew < 1 or ew > 5:
                    _rej("exit_weekday_out_of_range")
                    continue
                exit_wd = ew
            if entry_lag < 0:
                _rej("entry_lag_negative")
                continue
            if hold < 1:
                _rej("hold_lt_1")
                continue
            if (
                buy_wd is not None
                and exit_wd is not None
                and int(buy_wd) == int(exit_wd)
                and buy_on == "open"
                and sell_on == "open"
                and entry_lag == 0
            ):
                _rej("same_day_open_open_entry_lag0")
                continue

            meta = {
                "rule_id": rule_id,
                "gua_key": gkey,
                "gua_label": g.get("label"),
                "weekday_key": None,
                "weekday_label": "free_axes",
                "stop_loss": sl,
                "take_profit": tp,
                "holiday_policy": holiday_policy,
                "signal_weekdays": sw,
                "buy_option": bopt if isinstance(bopt, dict) else {},
                "sell_option": sopt if isinstance(sopt, dict) else {},
            }
            params: Dict[str, Any] = {
                "rule_ids": [rule_id],
                "period": period,
                "account_mode": account_mode,
                "codes": list(base_codes),
                "start": start,
                "end": end,
                "research_unadjusted": bool(research_unadjusted),
                "hold": hold,
                "entry_lag": entry_lag,
                "buy_weekday": buy_wd,
                "exit_weekday": exit_wd,
                "signal_weekdays": sw,
                "buy_on": buy_on,
                "sell_on": sell_on,
                "with_bagua": g.get("with_bagua", False),
                "gua_filter": dict(g.get("gua_filter") or {}),
                "stop_loss": sl,
                "take_profit": tp,
                "holiday_policy": holiday_policy,
                "_meta": meta,
            }
            variants.append(params)
    else:
        wds = list(weekday_keys or ["all_signal_tn12"])
        for w in wds:
            if w not in WEEKDAY_TEMPLATES:
                raise ValueError(f"unknown weekday template: {w}")
        for rule_id, gkey, wkey, sl, tp in itertools.product(rules, guas, wds, sls, tps):
            theoretical += 1
            g = GUA_PRESETS[gkey]
            w = WEEKDAY_TEMPLATES[wkey]
            params = {
                "rule_ids": [rule_id],
                "period": period,
                "account_mode": account_mode,
                "codes": list(base_codes),
                "start": start,
                "end": end,
                "research_unadjusted": bool(research_unadjusted),
                "hold": w.get("hold", 1),
                "entry_lag": w.get("entry_lag", 1),
                "buy_weekday": w.get("buy_weekday"),
                "exit_weekday": w.get("exit_weekday"),
                "signal_weekdays": w.get("signal_weekdays"),
                "buy_on": w.get("buy_on", "open"),
                "sell_on": w.get("sell_on", "open"),
                "with_bagua": g.get("with_bagua", False),
                "gua_filter": dict(g.get("gua_filter") or {}),
                "stop_loss": sl,
                "take_profit": tp,
                "holiday_policy": holiday_policy,
                "_meta": {
                    "rule_id": rule_id,
                    "gua_key": gkey,
                    "gua_label": g.get("label"),
                    "weekday_key": wkey,
                    "weekday_label": w.get("label"),
                    "stop_loss": sl,
                    "take_profit": tp,
                    "holiday_policy": holiday_policy,
                },
            }
            variants.append(params)

    actual = len(variants)
    return {
        "theoretical": theoretical,
        "rejected": rejected,
        "actual": actual,
        "rejection_reasons": rejection_reasons,
        "variants": variants,
    }


def estimate_grid_from_payload(payload: dict) -> dict:
    """Unified estimate / preview for legacy templates OR free axes.

    Prefers research.planner.plan_experiment when available; otherwise local
    expand_param_grid_unified. Returns theoretical/rejected/actual plus UI
    aliases estimated_variants/n/count.
    """
    max_v = int(payload.get("max_variants") or DEFAULT_MAX_VARIANTS)
    force = bool(payload.get("force"))
    hard = HARD_MAX_VARIANTS

    research_plan = _try_research_plan(payload)
    if isinstance(research_plan, dict) and research_plan.get("source") == "research.planner":
        theoretical = int(research_plan.get("theoretical") or 0)
        rejected = int(research_plan.get("rejected") or 0)
        variants = list(research_plan.get("variants") or [])
        actual = int(research_plan.get("actual") or len(variants))
        reasons = dict(research_plan.get("rejection_reasons") or {})
        preview = list(research_plan.get("preview") or variants[:50])
        msg = research_plan.get("message")
        hard = int(research_plan.get("hard_max") or hard)
    else:
        plan = expand_param_grid_unified(
            rule_ids=payload.get("rule_ids") or [],
            gua_keys=payload.get("gua_keys") or ["none"],
            weekday_keys=payload.get("weekday_keys"),
            stop_loss_list=payload.get("stop_loss_list"),
            signal_weekdays_options=payload.get("signal_weekdays_options"),
            buy_options=payload.get("buy_options"),
            sell_options=payload.get("sell_options"),
            take_profit_list=payload.get("take_profit_list"),
            holiday_policy=payload.get("holiday_policy") or "next_trading_day",
            period=payload.get("period") or "DAY",
            codes=payload.get("codes"),
            start=payload.get("start"),
            end=payload.get("end"),
            account_mode=payload.get("account_mode") or "portfolio",
            research_unadjusted=bool(payload.get("research_unadjusted")),
            collect_rejections=True,
        )
        theoretical = int(plan["theoretical"])
        rejected = int(plan["rejected"])
        actual = int(plan["actual"])
        reasons = dict(plan.get("rejection_reasons") or {})
        preview = list(plan.get("variants") or [])[:50]
        msg = None

    ok = True
    if actual > hard:
        ok = False
        msg = msg or ("组合数 %s 超过硬顶 %s" % (actual, hard))
    elif actual > max_v and not force:
        ok = False
        msg = msg or (
            "组合数 %s 超过上限 max_variants=%s；请缩小空间或提高 max_variants，或 force=true（硬顶 %s）"
            % (actual, max_v, hard)
        )
    elif actual > max_v and force:
        msg = msg or ("force=true：允许 %s 组（上限 %s，硬顶 %s）" % (actual, max_v, hard))
    elif actual > 20:
        msg = msg or ("组合数 %s 较大，建议先用演示池试跑" % actual)

    if rejected and theoretical:
        soft = "理论 %s，过滤 %s，实际 %s" % (theoretical, rejected, actual)
        msg = ("%s；%s" % (msg, soft)) if msg else soft

    return {
        "theoretical": theoretical,
        "rejected": rejected,
        "actual": actual,
        "rejection_reasons": reasons,
        "preview": preview,
        "max_variants": max_v,
        "hard_max": hard,
        "ok": ok,
        "message": msg,
        "estimated_variants": actual,
        "n": actual,
        "count": actual,
        "exceeds_soft_cap": actual > max_v,
        "warning": msg if actual > max_v or (actual > 20) else None,
        "force": force,
        "mode": "free_axes" if _has_free_axes(payload) else "legacy_templates",
    }


def estimate_grid_size(
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Sequence[str],
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    *,
    take_profit_list: Optional[Sequence[Optional[float]]] = None,
    signal_weekdays_options: Optional[Sequence[Any]] = None,
    buy_options: Optional[Sequence[dict]] = None,
    sell_options: Optional[Sequence[dict]] = None,
) -> int:
    """Legacy-compatible count; free axes use product of independent options.

    Does not apply constraint filtering (use estimate_grid_from_payload).
    Caps: DEFAULT_MAX_VARIANTS=50 soft; HARD_MAX_VARIANTS=500 hard.
    """
    axes = {
        "signal_weekdays_options": signal_weekdays_options,
        "buy_options": buy_options,
        "sell_options": sell_options,
        "take_profit_list": take_profit_list,
    }
    if _has_free_axes(axes):
        n_rules = max(1, len(list(rule_ids or [])))
        n_gua = max(1, len(list(gua_keys or ["none"])))
        n_sig = max(1, len(list(signal_weekdays_options if signal_weekdays_options is not None else [None])))
        n_buy = max(1, len(list(buy_options if buy_options is not None else [{}])))
        n_sell = max(1, len(list(sell_options if sell_options is not None else [{}])))
        n_sl = max(1, len(list(stop_loss_list if stop_loss_list is not None else [None])))
        n_tp = max(1, len(list(take_profit_list if take_profit_list is not None else [None])))
        return n_rules * n_gua * n_sig * n_buy * n_sell * n_sl * n_tp
    n_rules = max(1, len(list(rule_ids or [])))
    n_gua = max(1, len(list(gua_keys or ["none"])))
    n_wd = max(1, len(list(weekday_keys or ["all_signal_tn12"])))
    n_sl = max(1, len(list(stop_loss_list if stop_loss_list is not None else [None])))
    n_tp = max(1, len(list(take_profit_list if take_profit_list is not None else [None])))
    return n_rules * n_gua * n_wd * n_sl * n_tp


def expand_param_grid(
    *,
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Sequence[str] = ("all_signal_tn12",),
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    signal_weekdays_options: Optional[Sequence[Any]] = None,
    buy_options: Optional[Sequence[dict]] = None,
    sell_options: Optional[Sequence[dict]] = None,
    take_profit_list: Optional[Sequence[Optional[float]]] = None,
    holiday_policy: str = "next_trading_day",
) -> List[Dict[str, Any]]:
    """Cartesian product of research axes -> list of BacktestRequest-like dicts.

    Free axes take precedence over weekday_keys templates when provided.
    """
    plan = expand_param_grid_unified(
        rule_ids=rule_ids,
        gua_keys=gua_keys,
        weekday_keys=weekday_keys,
        stop_loss_list=stop_loss_list,
        signal_weekdays_options=signal_weekdays_options,
        buy_options=buy_options,
        sell_options=sell_options,
        take_profit_list=take_profit_list,
        holiday_policy=holiday_policy,
        period=period,
        codes=codes,
        start=start,
        end=end,
        account_mode=account_mode,
        research_unadjusted=research_unadjusted,
        collect_rejections=False,
    )
    return list(plan["variants"])




def create_experiment_from_grid(
    cfg: Optional[AStockConfig],
    *,
    name: str,
    rule_ids: Sequence[str],
    gua_keys: Sequence[str],
    weekday_keys: Optional[Sequence[str]] = None,
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    concurrency: int = 1,
    force: bool = False,
    note: str = "",
    signal_weekdays_options: Optional[Sequence[Any]] = None,
    buy_options: Optional[Sequence[dict]] = None,
    sell_options: Optional[Sequence[dict]] = None,
    take_profit_list: Optional[Sequence[Optional[float]]] = None,
    holiday_policy: str = "next_trading_day",
    engine: str = "fast",
    artifact_level: str = "summary",
    use_signal_cache: bool = True,
    promote_top_n: int = 3,
    promote_metric: str = "total_return",
) -> Dict[str, Any]:
    """Create experiment from legacy weekday_keys templates OR free axes.

    Phase-3 defaults: engine=fast, artifact_level=summary, use_signal_cache=True
    so large grids screen quickly; promote winners with engine=full later.

    Templates are presets only: when any free axis is provided, weekday_keys
    are ignored. Caps: DEFAULT_MAX_VARIANTS=50 soft; HARD_MAX_VARIANTS=500.
    force=True required to exceed max_variants; hard max always refuses.
    """
    cfg = cfg or get_default_config()
    axes = {
        "signal_weekdays_options": signal_weekdays_options,
        "buy_options": buy_options,
        "sell_options": sell_options,
        "take_profit_list": take_profit_list,
    }
    use_free = _has_free_axes(axes)
    wd_keys = list(weekday_keys) if weekday_keys is not None else ["all_signal_tn12"]

    payload = {
        "rule_ids": list(rule_ids),
        "gua_keys": list(gua_keys),
        "weekday_keys": None if use_free else wd_keys,
        "stop_loss_list": stop_loss_list,
        "signal_weekdays_options": signal_weekdays_options,
        "buy_options": buy_options,
        "sell_options": sell_options,
        "take_profit_list": take_profit_list,
        "holiday_policy": holiday_policy,
        "period": period,
        "codes": codes,
        "start": start,
        "end": end,
        "account_mode": account_mode,
        "research_unadjusted": research_unadjusted,
        "max_variants": max_variants,
        "force": force,
        "hard_max": HARD_MAX_VARIANTS,
    }

    research_plan = _try_research_plan(payload)
    if research_plan and research_plan.get("source") == "research.planner":
        variants = list(research_plan.get("variants") or [])
        n = int(research_plan.get("actual") or len(variants))
        theoretical = research_plan.get("theoretical")
        rejected = research_plan.get("rejected")
        rejection_reasons = research_plan.get("rejection_reasons")
        # When planner truncates (error set) without force, refuse create
        if research_plan.get("message") and not variants and n > max_variants and not force:
            raise ValueError(
                "组合数 %s 超过上限 max_variants=%s；请缩小空间或提高 max_variants，或 force=true（硬顶 %s）"
                % (n, max_variants, HARD_MAX_VARIANTS)
            )
        if n > max_variants and not force:
            raise ValueError(
                "组合数 %s 超过上限 max_variants=%s；请缩小空间或提高 max_variants，或 force=true（硬顶 %s）"
                % (n, max_variants, HARD_MAX_VARIANTS)
            )
        if n > HARD_MAX_VARIANTS:
            raise ValueError("组合数 %s 超过硬顶 %s" % (n, HARD_MAX_VARIANTS))
        # If truncated due to cap, variants may be empty — expand via force path already handled
        if not variants and n > 0:
            # re-plan with force to materialize for storage only if user forced
            if force:
                payload["force"] = True
                research_plan = _try_research_plan(payload) or research_plan
                variants = list(research_plan.get("variants") or [])
                n = int(research_plan.get("actual") or len(variants))
            else:
                raise ValueError(
                    "组合数 %s 超过上限 max_variants=%s；请缩小空间或 force=true（硬顶 %s）"
                    % (n, max_variants, HARD_MAX_VARIANTS)
                )
    else:
        plan = expand_param_grid_unified(
            rule_ids=rule_ids,
            gua_keys=gua_keys,
            weekday_keys=None if use_free else wd_keys,
            stop_loss_list=stop_loss_list,
            signal_weekdays_options=signal_weekdays_options,
            buy_options=buy_options,
            sell_options=sell_options,
            take_profit_list=take_profit_list,
            holiday_policy=holiday_policy,
            period=period,
            codes=codes,
            start=start,
            end=end,
            account_mode=account_mode,
            research_unadjusted=research_unadjusted,
            collect_rejections=True,
        )
        variants = list(plan["variants"])
        n = int(plan["actual"])
        theoretical = plan.get("theoretical")
        rejected = plan.get("rejected")
        rejection_reasons = plan.get("rejection_reasons")
        if n > max_variants and not force:
            raise ValueError(
                "组合数 %s 超过上限 max_variants=%s；请缩小空间或提高 max_variants，或 force=true（硬顶 %s）"
                % (n, max_variants, HARD_MAX_VARIANTS)
            )
        if n > HARD_MAX_VARIANTS:
            raise ValueError("组合数 %s 超过硬顶 %s" % (n, HARD_MAX_VARIANTS))

    warning = None
    if n > 20:
        warning = "组合数 %s 较大，建议先用演示池试跑" % n

    config = {
        "rule_ids": list(rule_ids),
        "gua_keys": list(gua_keys),
        "weekday_keys": [] if use_free else wd_keys,
        "weekday_keys_note": "templates are presets only; ignored when free axes provided",
        "stop_loss_list": list(stop_loss_list) if stop_loss_list is not None else [None],
        "take_profit_list": list(take_profit_list) if take_profit_list is not None else [None],
        "signal_weekdays_options": list(signal_weekdays_options)
        if signal_weekdays_options is not None
        else None,
        "buy_options": list(buy_options) if buy_options is not None else None,
        "sell_options": list(sell_options) if sell_options is not None else None,
        "holiday_policy": holiday_policy,
        "engine": (engine or "fast").strip().lower(),
        "artifact_level": (artifact_level or "summary").strip().lower(),
        "use_signal_cache": bool(use_signal_cache),
        "promote_top_n": int(promote_top_n or 0),
        "promote_metric": str(promote_metric or "total_return"),
        "mode": "free_axes" if use_free else "legacy_templates",
        "period": period,
        "codes": list(codes) if codes else ["sh600000", "sz000001"],
        "start": start,
        "end": end,
        "account_mode": account_mode,
        "research_unadjusted": research_unadjusted,
        "estimated": n,
        "theoretical": theoretical,
        "rejected": rejected,
        "rejection_reasons": rejection_reasons,
        "warning": warning,
    }
    note_parts = [note or ""]
    if use_free:
        note_parts.append("[free_axes; weekday templates ignored]")
    exp = exp_db.create_experiment(
        cfg,
        name=name or ("实验·%s组合" % n),
        config=config,
        variants=variants,
        max_variants=max_variants,
        concurrency=concurrency,
        note=" ".join(x for x in note_parts if x).strip(),
    )
    if warning:
        exp["warning"] = warning
    exp["theoretical"] = theoretical
    exp["rejected"] = rejected
    exp["actual"] = n
    return exp


class ExperimentRunner:
    """In-process limited-concurrency runner for experiment variants."""

    def __init__(self, cfg: Optional[AStockConfig] = None):
        self.cfg = cfg or get_default_config()
        self._lock = threading.Lock()
        self._cancel: Dict[str, bool] = {}
        self._threads: Dict[str, threading.Thread] = {}

    def cancel(self, experiment_id: str) -> None:
        with self._lock:
            self._cancel[experiment_id] = True

    def is_cancelled(self, experiment_id: str) -> bool:
        with self._lock:
            return bool(self._cancel.get(experiment_id))

    def start(self, experiment_id: str) -> Dict[str, Any]:
        exp = exp_db.get_experiment(self.cfg, experiment_id)
        if exp.get("status") in ("running",):
            return exp
        with self._lock:
            self._cancel[experiment_id] = False
            if experiment_id in self._threads and self._threads[experiment_id].is_alive():
                return exp
            t = threading.Thread(
                target=self._run_experiment,
                args=(experiment_id,),
                daemon=True,
                name=f"exp-{experiment_id}",
            )
            self._threads[experiment_id] = t
            t.start()
        exp_db.update_experiment_status(self.cfg, experiment_id, "running")
        return exp_db.get_experiment(self.cfg, experiment_id)

    def _run_one(self, experiment_id: str, variant: dict) -> Tuple[str, str, Optional[str], Optional[str]]:
        """Returns (variant_id, status, run_id, error)."""
        vid = variant["variant_id"]
        if self.is_cancelled(experiment_id):
            return vid, "cancelled", None, "cancelled"
        params = dict(variant.get("params") or {})
        meta = params.pop("_meta", None)
        ph = variant.get("param_hash") or exp_db.param_hash(params)
        # research fingerprint is metadata only; param_hash dedup stays param-only
        research_fp = None
        try:
            from ..research.fingerprint import research_fingerprint_from_params
            research_fp = research_fingerprint_from_params(params).full_hex(16)
        except Exception:
            research_fp = None
        # de-dup
        existing = exp_db.find_run_id_by_param_hash(self.cfg, ph)
        if existing:
            exp_db.update_variant(
                self.cfg, vid, status="skipped", run_id=existing, error="param_hash dedup"
            )
            return vid, "skipped", existing, None

        exp_db.update_variant(self.cfg, vid, status="running")
        try:
            # Experiment-level defaults (Phase-3): fast screen + summary artifacts + cache
            exp_row = exp_db.get_experiment(self.cfg, experiment_id)
            exp_cfg = dict(exp_row.get("config") or {})
            eng = (
                params.get("engine")
                or exp_cfg.get("engine")
                or "fast"
            )
            art = (
                params.get("artifact_level")
                or exp_cfg.get("artifact_level")
                or "summary"
            )
            use_cache = params.get("use_signal_cache")
            if use_cache is None:
                use_cache = exp_cfg.get("use_signal_cache", True)
            req = BacktestRequest(
                rule_ids=list(params.get("rule_ids") or []),
                period=params.get("period") or "DAY",
                hold=int(params.get("hold") or 1),
                entry_lag=int(params.get("entry_lag") or 1),
                signal_weekdays=params.get("signal_weekdays"),
                buy_on=params.get("buy_on") or "open",
                sell_on=params.get("sell_on") or "open",
                buy_weekday=params.get("buy_weekday"),
                exit_weekday=params.get("exit_weekday"),
                codes=params.get("codes"),
                start=params.get("start"),
                end=params.get("end"),
                with_bagua=bool(params.get("with_bagua")),
                gua_filter=params.get("gua_filter"),
                research_unadjusted=bool(params.get("research_unadjusted")),
                stop_loss=params.get("stop_loss"),
                take_profit=params.get("take_profit"),
                account_mode=params.get("account_mode") or "portfolio",
                engine=str(eng or "fast"),
                artifact_level=str(art or "summary"),
                use_signal_cache=bool(use_cache),
                holiday_policy=params.get("holiday_policy")
                or exp_cfg.get("holiday_policy")
                or "next_trading_day",
            )
            svc = BacktestService(self.cfg)
            summary = svc.run(req)
            rid = summary.get("run_id")
            # link experiment on run row
            if rid:
                try:
                    exp_db.upsert_run_from_index_row(
                        self.cfg,
                        {
                            "run_id": rid,
                            "title": summary.get("title"),
                            "status": summary.get("status"),
                            "created_at": int(time.time()),
                            "indicator_ids": (summary.get("repro") or {}).get("indicator_ids")
                            or params.get("rule_ids"),
                            "period": params.get("period"),
                            "hold": params.get("hold"),
                            "entry_lag": params.get("entry_lag"),
                            "buy_weekday": params.get("buy_weekday"),
                            "exit_weekday": params.get("exit_weekday"),
                            "buy_on": params.get("buy_on"),
                            "sell_on": params.get("sell_on"),
                            "signal_weekdays": params.get("signal_weekdays"),
                            "account_mode": params.get("account_mode"),
                            "start": params.get("start"),
                            "end": params.get("end"),
                            "with_bagua": params.get("with_bagua"),
                            "gua_filter": summary.get("gua_filter") or params.get("gua_filter"),
                            "metrics": summary.get("metrics"),
                            "param_hash": ph,
                            "research_fingerprint": research_fp
                            or (summary.get("research_fingerprint") if isinstance(summary, dict) else None),
                            "experiment_id": experiment_id,
                            "variant_id": vid,
                        },
                    )
                except Exception:
                    pass
            st = "succeeded"
            if (summary.get("status") or "").startswith("no_go") or summary.get("status") == "failed":
                st = "failed"
            exp_db.update_variant(self.cfg, vid, status=st, run_id=rid, error=summary.get("error"))
            return vid, st, rid, summary.get("error")
        except Exception as e:
            err = f"{e}\n{traceback.format_exc()[:500]}"
            exp_db.update_variant(self.cfg, vid, status="failed", error=str(e)[:500])
            return vid, "failed", None, str(e)

    def _run_experiment(self, experiment_id: str) -> None:
        try:
            exp = exp_db.get_experiment(self.cfg, experiment_id)
            concurrency = max(1, int(exp.get("concurrency") or 1))
            pending = [
                v
                for v in (exp.get("variants") or [])
                if v.get("status") in ("pending", "failed")
            ]
            # only re-run pending; failed retry if restarted
            pending = [
                v
                for v in (exp.get("variants") or [])
                if v.get("status") == "pending"
            ]
            completed = int(exp.get("completed_variants") or 0)
            failed = int(exp.get("failed_variants") or 0)
            skipped = int(exp.get("skipped_variants") or 0)

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futs = {
                    pool.submit(self._run_one, experiment_id, v): v for v in pending
                }
                for fut in as_completed(futs):
                    if self.is_cancelled(experiment_id):
                        break
                    try:
                        _vid, st, _rid, _err = fut.result()
                    except Exception:
                        st = "failed"
                    if st == "succeeded":
                        completed += 1
                    elif st == "skipped":
                        skipped += 1
                    elif st == "cancelled":
                        pass
                    else:
                        failed += 1
                    exp_db.update_experiment_status(
                        self.cfg,
                        experiment_id,
                        "running",
                        completed_variants=completed,
                        failed_variants=failed,
                        skipped_variants=skipped,
                    )

            final = "cancelled" if self.is_cancelled(experiment_id) else "completed"
            exp_db.update_experiment_status(
                self.cfg,
                experiment_id,
                final,
                completed_variants=completed,
                failed_variants=failed,
                skipped_variants=skipped,
            )
            if final == "completed" and not self.is_cancelled(experiment_id):
                try:
                    self._promote_top_n_full(experiment_id)
                except Exception:
                    pass
        except Exception as e:
            exp_db.update_experiment_status(
                self.cfg, experiment_id, "failed"
            )
            # best-effort
            _ = e

    def _promote_top_n_full(self, experiment_id: str) -> None:
        """Re-run top-N variants with engine=full + artifact_level=full (Phase-3)."""
        exp = exp_db.get_experiment(self.cfg, experiment_id)
        conf = dict(exp.get("config") or {})
        top_n = int(conf.get("promote_top_n") or 0)
        if top_n <= 0:
            return
        metric = str(conf.get("promote_metric") or "total_return")
        table = exp_db.experiment_results_table(self.cfg, experiment_id)
        rows = list(table.get("rows") or [])
        scored = []
        for row in rows:
            if row.get("status") != "succeeded":
                continue
            m = row.get("metrics") or {}
            val = m.get(metric)
            if val is None:
                continue
            try:
                scored.append((float(val), row))
            except (TypeError, ValueError):
                continue
        if not scored:
            return
        scored.sort(key=lambda x: x[0], reverse=True)
        winners = scored[:top_n]
        for rank, (_score, row) in enumerate(winners, 1):
            if self.is_cancelled(experiment_id):
                break
            params = dict(row.get("params") or {})
            params.pop("_meta", None)
            params["engine"] = "full"
            params["artifact_level"] = "full"
            params["use_signal_cache"] = conf.get("use_signal_cache", True)
            vid = str(row.get("variant_id") or ("promote_%s" % rank))
            variant = {
                "variant_id": "%s__full" % vid,
                "params": params,
                "param_hash": None,
                "status": "pending",
            }
            try:
                _vid, st, rid, err = self._run_one(experiment_id, variant)
                exp_db.update_variant(
                    self.cfg,
                    _vid,
                    status=st,
                    run_id=rid,
                    error=(err[:500] if err else None),
                )
            except Exception:
                continue




_RUNNER: Optional[ExperimentRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner(cfg: Optional[AStockConfig] = None) -> ExperimentRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = ExperimentRunner(cfg)
        return _RUNNER


def write_experiment_excel(cfg: AStockConfig, experiment_id: str, path=None):
    """Write a simple results workbook for the experiment."""
    from pathlib import Path

    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise RuntimeError("openpyxl required for experiment excel") from e

    table = exp_db.experiment_results_table(cfg, experiment_id)
    out = Path(path) if path else Path(cfg.output_root) / experiment_id / "experiment_summary.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "实验结果"
    headers = [
        "variant_id",
        "status",
        "run_id",
        "rule",
        "gua",
        "weekday",
        "stop_loss",
        "total_return",
        "annual_return",
        "max_drawdown",
        "win_rate",
        "n_round_trips",
        "payoff_ratio",
        "param_hash",
        "error",
    ]
    ws.append(headers)
    for row in table.get("rows") or []:
        p = row.get("params") or {}
        meta = p.get("_meta") or {}
        m = row.get("metrics") or {}
        ws.append(
            [
                row.get("variant_id"),
                row.get("status"),
                row.get("run_id"),
                meta.get("rule_id") or (p.get("rule_ids") or [None])[0],
                meta.get("gua_label") or meta.get("gua_key"),
                meta.get("weekday_label") or meta.get("weekday_key"),
                meta.get("stop_loss"),
                m.get("total_return"),
                m.get("annual_return"),
                m.get("max_drawdown"),
                m.get("win_rate"),
                m.get("n_round_trips"),
                m.get("payoff_ratio") or m.get("profit_loss_ratio"),
                row.get("param_hash"),
                row.get("error"),
            ]
        )
    wb.save(out)
    return out
