# -*- coding: utf-8 -*-
"""Parameter space definition and cartesian expansion (no constraint filtering)."""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Sequence, Union

from .models import ParameterSpace

# Convenience mirror of service.experiments.WEEKDAY_TEMPLATES labels/keys.
# Keep structure local so research layer does not hard-depend on experiments import cycles.
PRESET_WEEKDAY_TEMPLATES: Dict[str, Dict[str, Any]] = {
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
        "label": "不限信号·经典T+1开/T+2平",
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

# Local GUA preset payloads (mirror experiments.GUA_PRESETS for expansion independence).
GUA_PRESETS: Dict[str, Dict[str, Any]] = {
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

# 735 hold-matrix axes (P2.7): Fri signal → Mon open buy → exit Tue–Fri × open/close × none/best3
PRESET_735_EXIT_WEEKDAYS = (2, 3, 4, 5)
PRESET_735_SELL_ONS = ("open", "close")
PRESET_735_GUA_KEYS = ("none", "best3")


def axes_from_legacy_templates(
    *,
    rule_ids: Sequence[str],
    weekday_keys: Sequence[str],
    gua_keys: Sequence[str],
    stop_loss_list: Optional[Sequence[Optional[float]]] = None,
    take_profit_list: Optional[Sequence[Optional[float]]] = None,
    period: str = "DAY",
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    holiday_policy: str = "next_trading_day",
) -> ParameterSpace:
    """Build a ParameterSpace from legacy WEEKDAY_TEMPLATES / GUA_PRESETS keys."""
    rules = list(rule_ids or [])
    wds = list(weekday_keys or ["all_signal_tn12"])
    guas = list(gua_keys or ["none"])
    sls = list(stop_loss_list if stop_loss_list is not None else [None])
    tps = list(take_profit_list if take_profit_list is not None else [None])

    for w in wds:
        if w not in PRESET_WEEKDAY_TEMPLATES:
            raise ValueError(f"unknown weekday template: {w}")
    for g in guas:
        if g not in GUA_PRESETS:
            raise ValueError(f"unknown gua preset: {g}")

    # Collapse templates into buy/sell/signal axes while preserving product size
    # equal to product of template keys × rules × gua × stop × take.
    # Each weekday template is one combined "schedule mode" stored as paired
    # buy_mode + sell_mode + signal option with shared template key in meta.
    signal_opts: List[Optional[List[int]]] = []
    buy_modes: List[Dict[str, Any]] = []
    sell_modes: List[Dict[str, Any]] = []
    # Use a single schedule axis via buy_modes tagged with full template fields
    schedule_modes: List[Dict[str, Any]] = []
    for wkey in wds:
        w = PRESET_WEEKDAY_TEMPLATES[wkey]
        schedule_modes.append(
            {
                "_weekday_key": wkey,
                "_weekday_label": w.get("label"),
                "signal_weekdays": w.get("signal_weekdays"),
                "buy_weekday": w.get("buy_weekday"),
                "exit_weekday": w.get("exit_weekday"),
                "buy_on": w.get("buy_on", "open"),
                "sell_on": w.get("sell_on", "open"),
                "entry_lag": w.get("entry_lag", 1),
                "hold": w.get("hold", 1),
            }
        )

    # Represent weekday templates as buy_modes that carry full schedule; sell_modes
    # fixed to a single placeholder so product size = len(wds) not len(wds)^2.
    return ParameterSpace(
        rule_ids=rules,
        period=period,
        signal_weekdays=[None],  # overridden by schedule in expand when present
        buy_modes=schedule_modes,
        sell_modes=[{"_from_buy_schedule": True}],
        gua_keys=guas,
        stop_loss_list=sls,
        take_profit_list=tps,
        holiday_policy=holiday_policy,
        codes=list(codes) if codes else None,
        start=start,
        end=end,
        account_mode=account_mode,
        research_unadjusted=bool(research_unadjusted),
    )


def _resolve_gua(key_or_filter: Union[str, Dict[str, Any], None]) -> Dict[str, Any]:
    if key_or_filter is None or key_or_filter == "none":
        g = GUA_PRESETS["none"]
        return {
            "gua_key": "none",
            "gua_label": g["label"],
            "with_bagua": False,
            "gua_filter": dict(g["gua_filter"]),
        }
    if isinstance(key_or_filter, str):
        if key_or_filter not in GUA_PRESETS:
            raise ValueError(f"unknown gua preset: {key_or_filter}")
        g = GUA_PRESETS[key_or_filter]
        return {
            "gua_key": key_or_filter,
            "gua_label": g["label"],
            "with_bagua": bool(g.get("with_bagua", False)),
            "gua_filter": dict(g.get("gua_filter") or {}),
        }
    # inline filter dict
    gf = dict(key_or_filter)
    enabled = bool(gf.get("enabled", True))
    return {
        "gua_key": "custom",
        "gua_label": "custom",
        "with_bagua": enabled,
        "gua_filter": gf,
    }


def _merge_buy_sell(
    buy: Dict[str, Any], sell: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge buy_mode and sell_mode into engine fields."""
    out: Dict[str, Any] = {}
    # Legacy schedule pack (from axes_from_legacy_templates)
    if buy.get("_weekday_key") is not None or "signal_weekdays" in buy and (
        "exit_weekday" in buy or "hold" in buy
    ):
        for k in (
            "signal_weekdays",
            "buy_weekday",
            "exit_weekday",
            "buy_on",
            "sell_on",
            "entry_lag",
            "hold",
        ):
            if k in buy:
                out[k] = buy[k]
        out["_weekday_key"] = buy.get("_weekday_key")
        out["_weekday_label"] = buy.get("_weekday_label")
        if sell and not sell.get("_from_buy_schedule"):
            # allow sell_mode override
            if "exit_weekday" in sell:
                out["exit_weekday"] = sell["exit_weekday"]
            if "hold" in sell:
                out["hold"] = sell["hold"]
            if "sell_on" in sell:
                out["sell_on"] = sell["sell_on"]
        return out

    # Independent axes
    if "buy_weekday" in buy and buy.get("buy_weekday") is not None:
        out["buy_weekday"] = buy["buy_weekday"]
        out["entry_lag"] = buy.get("entry_lag", 1)
    elif "entry_lag" in buy:
        out["entry_lag"] = buy["entry_lag"]
        out["buy_weekday"] = buy.get("buy_weekday")
    else:
        out["entry_lag"] = buy.get("entry_lag", 1)
        out["buy_weekday"] = buy.get("buy_weekday")

    out["buy_on"] = buy.get("buy_on", "open")

    if sell.get("_from_buy_schedule"):
        out.setdefault("hold", 1)
        out.setdefault("sell_on", "open")
        return out

    if "exit_weekday" in sell and sell.get("exit_weekday") is not None:
        out["exit_weekday"] = sell["exit_weekday"]
        out["hold"] = sell.get("hold", 1)
    elif "hold" in sell:
        out["hold"] = sell["hold"]
        out["exit_weekday"] = sell.get("exit_weekday")
    else:
        out["hold"] = sell.get("hold", 1)
        out["exit_weekday"] = sell.get("exit_weekday")

    out["sell_on"] = sell.get("sell_on", "open")
    return out


def expand_axes(space: Union[ParameterSpace, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cartesian product of ParameterSpace axes → raw BacktestRequest-like dicts + ``_meta``.

    Does **not** apply constraint filters.
    """
    if isinstance(space, dict):
        space = ParameterSpace.from_dict(space)

    rules = list(space.rule_ids or [])
    if not rules:
        # Still expand for planner/constraints to reject with missing rule_ids
        rules = [""]

    sig_opts = list(space.signal_weekdays if space.signal_weekdays is not None else [None])
    if not sig_opts:
        sig_opts = [None]

    buy_modes = list(space.buy_modes or [])
    if not buy_modes:
        buy_modes = [{"entry_lag": 1, "buy_on": "open"}]

    sell_modes = list(space.sell_modes or [])
    if not sell_modes:
        sell_modes = [{"hold": 1, "sell_on": "open"}]

    gua_options: List[Any] = []
    for gk in space.gua_keys or []:
        gua_options.append(gk)
    for gf in space.gua_filters or []:
        gua_options.append(gf)
    if not gua_options:
        gua_options = ["none"]

    sls = list(space.stop_loss_list if space.stop_loss_list is not None else [None])
    if not sls:
        sls = [None]
    tps = list(space.take_profit_list if space.take_profit_list is not None else [None])
    if not tps:
        tps = [None]

    extra_names = sorted((space.extra_axes or {}).keys())
    extra_lists = [
        list((space.extra_axes or {}).get(n) or [None]) for n in extra_names
    ]
    if not extra_lists:
        extra_product = [()]
    else:
        extra_product = list(itertools.product(*extra_lists))

    default_codes = list(space.codes) if space.codes else ["sh600000", "sz000001"]
    out: List[Dict[str, Any]] = []

    for rule_id, sig, buy, sell, gua_opt, sl, tp, extra_vals in itertools.product(
        rules, sig_opts, buy_modes, sell_modes, gua_options, sls, tps, extra_product
    ):
        merged = _merge_buy_sell(dict(buy), dict(sell))
        # signal_weekdays from axis unless schedule already set it
        if merged.get("signal_weekdays") is None and "signal_weekdays" not in buy:
            merged["signal_weekdays"] = sig
        elif "signal_weekdays" not in merged:
            merged["signal_weekdays"] = sig

        gua = _resolve_gua(gua_opt)
        params: Dict[str, Any] = {
            "rule_ids": [rule_id] if rule_id else [],
            "period": space.period or "DAY",
            "account_mode": space.account_mode or "portfolio",
            "codes": list(default_codes),
            "start": space.start,
            "end": space.end,
            "research_unadjusted": bool(space.research_unadjusted),
            "holiday_policy": space.holiday_policy or "next_trading_day",
            "hold": merged.get("hold", 1),
            "entry_lag": merged.get("entry_lag", 1),
            "buy_weekday": merged.get("buy_weekday"),
            "exit_weekday": merged.get("exit_weekday"),
            "signal_weekdays": merged.get("signal_weekdays"),
            "buy_on": merged.get("buy_on", "open"),
            "sell_on": merged.get("sell_on", "open"),
            "with_bagua": gua["with_bagua"],
            "gua_filter": gua["gua_filter"],
            "stop_loss": sl,
            "take_profit": tp,
            "_meta": {
                "rule_id": rule_id or None,
                "gua_key": gua["gua_key"],
                "gua_label": gua["gua_label"],
                "weekday_key": merged.get("_weekday_key"),
                "weekday_label": merged.get("_weekday_label"),
                "stop_loss": sl,
                "take_profit": tp,
                "buy_mode": dict(buy),
                "sell_mode": dict(sell),
                "signal_weekdays": merged.get("signal_weekdays"),
            },
        }
        if extra_names:
            for n, v in zip(extra_names, extra_vals):
                params[n] = v
                params["_meta"][f"extra:{n}"] = v
        out.append(params)
    return out


def preset_735_hold_matrix(
    rule_id: str = "735",
    *,
    expand: bool = True,
    codes: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    period: str = "DAY",
    account_mode: str = "portfolio",
    research_unadjusted: bool = False,
    holiday_policy: str = "next_trading_day",
) -> Union[ParameterSpace, List[Dict[str, Any]]]:
    """735 hold matrix: Fri signal, Mon open buy, exit Tue–Fri × sell open/close × gua none/best3.

    Product size: 4 exit_weekdays × 2 sell_on × 2 gua = **16** variants.
    """
    space = ParameterSpace(
        rule_ids=[rule_id],
        period=period,
        signal_weekdays=[[5]],  # Friday only
        buy_modes=[{"buy_weekday": 1, "buy_on": "open", "entry_lag": 1}],
        sell_modes=[
            {"exit_weekday": d, "sell_on": so, "hold": 1}
            for d in PRESET_735_EXIT_WEEKDAYS
            for so in PRESET_735_SELL_ONS
        ],
        gua_keys=list(PRESET_735_GUA_KEYS),
        stop_loss_list=[None],
        take_profit_list=[None],
        holiday_policy=holiday_policy,
        codes=list(codes) if codes else None,
        start=start,
        end=end,
        account_mode=account_mode,
        research_unadjusted=research_unadjusted,
    )
    if expand:
        return expand_axes(space)
    return space
