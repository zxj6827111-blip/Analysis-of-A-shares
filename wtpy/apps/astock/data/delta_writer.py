# -*- coding: utf-8 -*-
"""EOD delta write coordination - atomic commit + watermark publish.

The EOD delta pipeline replaces the legacy "rewrite full history NPZ blobs
every day" behavior:

  1. fetch the 20-day correction window from the provider,
  2. commit changed rows as ONE batch per kind (raw bars / adj_factor) in a
     single-writer DuckDB transaction,
  3. run a health check against the committed watermark,
  4. only then atomically publish the overlay registry (advance
     delta_watermark / factor_watermark).

A batch whose DB commit succeeded but whose overlay publish never ran stays
invisible (watermark unchanged) and is swept by the 72h governance job.

This module is intentionally network-free: it only commits rows and publishes
state. The sync scripts own provider fetching and row assembly.
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Sequence, Tuple

from .dataset_store import DatasetStore
from .delta_store import (
    DeltaStore,
    KIND_BARS,
    KIND_FACTOR,
    OverlayState,
    delta_write_lock,
    load_overlay_state,
    save_overlay_state,
)
from .overlay import OverlayView


class DeltaEodWriter:
    """Coordinates a single EOD delta update (raw + factor) for one root."""

    def __init__(self, store: DatasetStore, delta: Optional[DeltaStore] = None):
        self.store = store
        if delta is None:
            overlay = load_overlay_state(store.root)
            delta = DeltaStore(store.root, overlay.delta_store_id)
        self.delta = delta

    # ------------------------------------------------------------------
    def commit_bars(
        self,
        *,
        sync_run_id: str,
        source: str,
        base_dataset_id: str,
        cutoff: int,
        rows: Dict[str, Sequence[Tuple]],
        batch_suffix: str = "bars",
    ) -> Dict:
        """Commit one raw-bars delta batch (idempotent by batch_id)."""
        from .delta_store import make_batch_id

        batch_id = make_batch_id(
            sync_run_id=sync_run_id, kind=KIND_BARS, cutoff=cutoff,
            suffix=batch_suffix,
        )
        return self.delta.commit_batch(
            batch_id=batch_id,
            kind=KIND_BARS,
            source=source,
            adjustment="none",
            period="1d",
            base_dataset_id=base_dataset_id,
            watermark=int(cutoff),
            rows=rows,
        )

    def commit_factors(
        self,
        *,
        sync_run_id: str,
        source: str,
        factor_base_dataset_id: str,
        cutoff: int,
        rows: Dict[str, Sequence[Tuple]],
        batch_suffix: str = "factors",
    ) -> Dict:
        """Commit one adj_factor delta batch (idempotent by batch_id)."""
        from .delta_store import make_batch_id

        batch_id = make_batch_id(
            sync_run_id=sync_run_id, kind=KIND_FACTOR, cutoff=cutoff,
            suffix=batch_suffix,
        )
        return self.delta.commit_batch(
            batch_id=batch_id,
            kind=KIND_FACTOR,
            source=source,
            adjustment="adj_factor",
            period="1d",
            base_dataset_id=factor_base_dataset_id,
            watermark=int(cutoff),
            rows=rows,
        )

    # ------------------------------------------------------------------
    def publish(
        self,
        *,
        delta_watermark: Optional[int] = None,
        factor_watermark: Optional[int] = None,
        base_dataset_id: Optional[str] = None,
        require_health: bool = True,
    ) -> Dict:
        """Health-check then atomically advance the overlay registry.

        Both watermarks move together so readers never observe a factor
        surface newer than the raw surface (or vice versa). Returns a summary;
        raises DeltaWriteError when the health check fails (the overlay stays
        at its previous watermarks, keeping the batch invisible).
        """
        from .delta_store import DeltaWriteError

        overlay = load_overlay_state(self.store.root)
        if not overlay.enabled:
            raise DeltaWriteError(
                "overlay_v1 is not enabled on this data root; cannot publish"
            )
        new_delta = (
            int(delta_watermark) if delta_watermark is not None
            else overlay.delta_watermark
        )
        new_factor = (
            int(factor_watermark) if factor_watermark is not None
            else overlay.factor_watermark
        )
        if new_delta < int(overlay.delta_watermark or 0):
            raise DeltaWriteError(
                f"delta watermark regression: {new_delta} < "
                f"{overlay.delta_watermark}"
            )
        if new_factor < int(overlay.factor_watermark or 0):
            raise DeltaWriteError(
                f"factor watermark regression: {new_factor} < "
                f"{overlay.factor_watermark}"
            )
        if require_health and new_delta:
            expected_factor = (
                new_factor
                if factor_watermark is not None
                and new_factor > int(overlay.factor_watermark or 0)
                else None
            )
            health = self.delta.health_check(
                new_delta, factor_watermark=expected_factor
            )
            if not health["ok"]:
                raise DeltaWriteError(
                    f"delta health check failed at watermark {new_delta}: "
                    f"{health['problems']}"
                )
        overlay.delta_watermark = new_delta
        overlay.factor_watermark = new_factor
        if delta_watermark is not None:
            overlay.delta_commit_seq = self.delta.current_commit_seq(
                KIND_BARS, watermark=new_delta
            )
        if factor_watermark is not None:
            overlay.factor_commit_seq = self.delta.current_commit_seq(
                KIND_FACTOR, watermark=new_factor
            )
        if base_dataset_id:
            overlay.base_dataset_id = base_dataset_id
        save_overlay_state(self.store.root, overlay)
        return {
            "published": True,
            "delta_watermark": new_delta,
            "factor_watermark": new_factor,
            "delta_commit_seq": overlay.delta_commit_seq,
            "factor_commit_seq": overlay.factor_commit_seq,
        }

    # ------------------------------------------------------------------
    def run_locked(self, fn) -> Dict:
        """Run ``fn()`` while holding the delta write lock (cross-process)."""
        with delta_write_lock(self.store.root):
            return fn()
