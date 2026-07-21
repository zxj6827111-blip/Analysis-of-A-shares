# -*- coding: utf-8 -*-
"""Research / experiment center building blocks (phase-1+)."""

from .constraints import filter_variants, summarize_rejections, validate_variant
from .fingerprint import (
    ResearchFingerprint,
    build_execution_fingerprint,
    build_filter_fingerprint,
    build_research_fingerprint,
    build_signal_fingerprint,
    fingerprint_hex,
    research_fingerprint_from_params,
    short_fingerprint,
)
from .models import AxisSpec, ParameterSpace, as_jsonable
from .matrix import build_result_matrix, matrix_table_to_rows
from .parameter_space import (
    GUA_PRESETS,
    PRESET_735_EXIT_WEEKDAYS,
    PRESET_735_GUA_KEYS,
    PRESET_735_SELL_ONS,
    PRESET_WEEKDAY_TEMPLATES,
    axes_from_legacy_templates,
    expand_axes,
    preset_735_hold_matrix,
)
from .planner import (
    DEFAULT_MAX_VARIANTS,
    HARD_MAX_VARIANTS,
    plan_experiment,
)

__all__ = [
    # fingerprint
    "ResearchFingerprint",
    "build_research_fingerprint",
    "build_signal_fingerprint",
    "build_filter_fingerprint",
    "build_execution_fingerprint",
    "fingerprint_hex",
    "short_fingerprint",
    "research_fingerprint_from_params",
    # models / space
    "AxisSpec",
    "ParameterSpace",
    "as_jsonable",
    "PRESET_WEEKDAY_TEMPLATES",
    "GUA_PRESETS",
    "axes_from_legacy_templates",
    "expand_axes",
    "preset_735_hold_matrix",
    "PRESET_735_EXIT_WEEKDAYS",
    "PRESET_735_SELL_ONS",
    "PRESET_735_GUA_KEYS",
    "build_result_matrix",
    "matrix_table_to_rows",
    # constraints / planner
    "validate_variant",
    "filter_variants",
    "summarize_rejections",
    "plan_experiment",
    "DEFAULT_MAX_VARIANTS",
    "HARD_MAX_VARIANTS",
]
