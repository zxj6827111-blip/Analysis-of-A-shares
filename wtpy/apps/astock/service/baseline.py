# -*- coding: utf-8 -*-
"""Survivorship-safe baseline resolution — Gate B7.

Resolves the pinned survivorship-safe dataset combination for new tasks:
  L1 signal    : internal / composite_tushare_factor_qfq (latest ready)
  L2 execution : internal / composite_none               (latest ready)
  universe     : the point-in-time universe recorded in the signal dataset's
                 provenance (pins signal/universe consistency)
  delist rule  : standard scenario (last_tradable_price) unless overridden

Fail-closed: if any piece is missing the resolver raises
BaselineUnavailableError — callers must surface the error, NEVER fall back
to legacy datasets silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AStockConfig
from ..data.dataset_store import DatasetStore
from ..data.pit_universe import PointInTimeUniverse
from ..data.repository import DatasetNotFoundError, MarketDataRepository
from ..delist_policy import SCENARIO_LAST_TRADABLE_PRICE

SURVIVORSHIP_SAFE_BASELINE_VERSION = "ss_baseline_v1"

BASELINE_SURVIVORSHIP_SAFE = "survivorship_safe"
BASELINE_LEGACY = "legacy"


class BaselineUnavailableError(RuntimeError):
    """The survivorship-safe baseline cannot be resolved (missing datasets).

    Callers must surface this error; silently falling back to legacy
    datasets is prohibited (Gate B7)."""


def resolve_survivorship_safe_baseline(
    cfg: AStockConfig,
    *,
    delist_exit_scenario: Optional[str] = None,
    delist_recovery_discount: Optional[float] = None,
) -> Dict[str, Any]:
    """Resolve the pinned survivorship-safe baseline combination.

    Returns a dict of BacktestRequest-compatible fields. Raises
    BaselineUnavailableError when any component is missing.
    """
    repo = MarketDataRepository(DatasetStore(cfg.market_data_root))

    try:
        sig = repo.resolve_latest_ready(
            source="internal", adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
    except DatasetNotFoundError:
        raise BaselineUnavailableError(
            "survivorship-safe baseline unavailable: no ready "
            "internal/composite_tushare_factor_qfq signal dataset (Gate B6)"
        ) from None
    try:
        exe = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d",
        )
    except DatasetNotFoundError:
        raise BaselineUnavailableError(
            "survivorship-safe baseline unavailable: no ready "
            "internal/composite_none execution dataset (Gate B3)"
        ) from None

    prov = sig.provenance or {}
    universe_id = str(prov.get("universe_dataset_id") or "")
    if not universe_id:
        raise BaselineUnavailableError(
            f"signal dataset {sig.dataset_id} records no universe_dataset_id "
            f"in provenance; cannot pin the point-in-time universe"
        )
    try:
        pit = PointInTimeUniverse.from_root(
            Path(cfg.market_data_root), universe_id
        )
    except FileNotFoundError:
        raise BaselineUnavailableError(
            f"point-in-time universe {universe_id} referenced by "
            f"{sig.dataset_id} is missing from the data root"
        ) from None

    # consistency: the signal dataset's raw parent must be the execution
    # dataset (same composite raw) — otherwise L1/L2 would diverge
    if sig.raw_dataset_id and sig.raw_dataset_id != exe.dataset_id:
        raise BaselineUnavailableError(
            f"baseline inconsistency: signal raw parent {sig.raw_dataset_id} "
            f"!= latest ready composite execution {exe.dataset_id}; refusing "
            f"to mix generations"
        )

    return {
        "baseline_version": SURVIVORSHIP_SAFE_BASELINE_VERSION,
        "signal_data_source": "internal",
        "signal_adjustment": "composite_tushare_factor_qfq",
        "dataset_id": sig.dataset_id,
        "execution_data_source": "internal",
        "execution_dataset_id": exe.dataset_id,
        "universe_dataset_id": pit.universe_dataset_id,
        "universe_rule_version": pit.universe_rule_version,
        "delist_exit_scenario": (
            delist_exit_scenario or SCENARIO_LAST_TRADABLE_PRICE
        ),
        "delist_recovery_discount": delist_recovery_discount,
        "baseline_generation": BASELINE_SURVIVORSHIP_SAFE,
    }
