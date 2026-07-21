# -*- coding: utf-8 -*-
"""Experiment planner: expand axes, filter constraints, preview / budget."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Union

from .constraints import filter_variants, summarize_rejections
from .models import ParameterSpace
from .parameter_space import expand_axes

# Soft UI default; refuse above this unless force=True
DEFAULT_MAX_VARIANTS = 50
# Hard research-mode ceiling (align with experiments.HARD_MAX_VARIANTS spirit)
HARD_MAX_VARIANTS = 2000

# Heavy / internal keys omitted from preview rows
_PREVIEW_DROP = frozenset(
    {
        "gua_filter",
        "codes",
        "_meta",
    }
)


def _space_from_payload(
    space_or_payload: Union[ParameterSpace, Dict[str, Any], Sequence[Dict[str, Any]]],
) -> Union[ParameterSpace, List[Dict[str, Any]]]:
    """Normalize input to ParameterSpace or pre-expanded variant list."""
    if isinstance(space_or_payload, ParameterSpace):
        return space_or_payload
    if isinstance(space_or_payload, list):
        return list(space_or_payload)
    if isinstance(space_or_payload, dict):
        # Pre-expanded variants payload
        if "variants" in space_or_payload and isinstance(
            space_or_payload["variants"], list
        ):
            return list(space_or_payload["variants"])
        # Nested space
        if "space" in space_or_payload and isinstance(space_or_payload["space"], dict):
            return ParameterSpace.from_dict(space_or_payload["space"])
        return ParameterSpace.from_dict(space_or_payload)
    raise TypeError(
        "space_or_payload must be ParameterSpace, dict, or list of variants"
    )


def _preview_row(v: Dict[str, Any]) -> Dict[str, Any]:
    meta = v.get("_meta") or {}
    row = {
        "rule_ids": v.get("rule_ids"),
        "period": v.get("period"),
        "signal_weekdays": v.get("signal_weekdays"),
        "buy_weekday": v.get("buy_weekday"),
        "exit_weekday": v.get("exit_weekday"),
        "entry_lag": v.get("entry_lag"),
        "hold": v.get("hold"),
        "buy_on": v.get("buy_on"),
        "sell_on": v.get("sell_on"),
        "stop_loss": v.get("stop_loss"),
        "take_profit": v.get("take_profit"),
        "with_bagua": v.get("with_bagua"),
        "account_mode": v.get("account_mode"),
        "holiday_policy": v.get("holiday_policy"),
        "gua_key": meta.get("gua_key"),
        "weekday_key": meta.get("weekday_key"),
        "weekday_label": meta.get("weekday_label"),
    }
    return row


def plan_experiment(
    space_or_payload: Union[ParameterSpace, Dict[str, Any], Sequence[Dict[str, Any]]],
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    force: bool = False,
    hard_max: int = HARD_MAX_VARIANTS,
) -> Dict[str, Any]:
    """Expand + filter a parameter space and return plan summary.

    Keys:
      theoretical_count, rejected_count, actual_count, rejection_reasons,
      preview (first 50 light rows), variants (full kept if allowed),
      truncated, max_variants, force, error (optional).
    """
    raw_in = _space_from_payload(space_or_payload)
    if isinstance(raw_in, list):
        raw = raw_in
        theoretical = len(raw)
    else:
        raw = expand_axes(raw_in)
        theoretical = len(raw)
        # empty product: expand still may produce rows with empty rule_ids
        if theoretical == 0:
            return {
                "theoretical_count": 0,
                "rejected_count": 0,
                "actual_count": 0,
                "rejection_reasons": {"empty_product": 1},
                "preview": [],
                "variants": [],
                "truncated": False,
                "max_variants": max_variants,
                "force": force,
                "error": "empty_product",
            }

    kept, rejected = filter_variants(raw)
    rejection_reasons = summarize_rejections(rejected)
    actual = len(kept)

    if actual == 0 and theoretical > 0 and not rejection_reasons:
        rejection_reasons = {"empty_product": 1}

    preview = [_preview_row(v) for v in kept[:50]]

    error: Optional[str] = None
    truncated = False
    variants_out: List[Dict[str, Any]] = []

    if actual > hard_max:
        error = f"actual_count {actual} exceeds hard_max {hard_max}"
        truncated = True
    elif actual > max_variants and not force:
        error = (
            f"actual_count {actual} exceeds max_variants={max_variants}; "
            f"narrow the space, raise max_variants, or force=true "
            f"(hard_max={hard_max})"
        )
        truncated = True
    else:
        variants_out = kept
        if actual > max_variants and force:
            truncated = False  # force accepts full list within hard_max

    return {
        "theoretical_count": theoretical,
        "rejected_count": len(rejected),
        "actual_count": actual,
        "rejection_reasons": rejection_reasons,
        "preview": preview,
        "variants": variants_out,
        "truncated": truncated,
        "max_variants": max_variants,
        "force": force,
        "hard_max": hard_max,
        "error": error,
    }
