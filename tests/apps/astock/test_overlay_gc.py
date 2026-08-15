# -*- coding: utf-8 -*-
"""GC retention tests for the overlay storage mode.

Covers the plan's GC acceptance:
  - overlay-registered base blobs survive GC even when their manifests were
    expired (virtual views are periodically cleaned)
  - GC fails closed when the overlay registry references a missing base
  - orphans (unreferenced blobs) are still collected
  - any still-resolvable dataset keeps its blobs
"""

from __future__ import annotations

import hashlib
import io
import os
import time

import numpy as np
import pytest

from wtpy.apps.astock.data.blob_gc import build_gc_plan
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.delta_store import OverlayState, save_overlay_state

from .conftest import build_overlay_warehouse


def _age_blobs(store: DatasetStore, hours: float = 100.0):
    now = time.time()
    for f in store.blobs_dir.glob("*.npz"):
        os.utime(f, (now - hours * 3600, now - hours * 3600))


def _make_orphan(store: DatasetStore) -> str:
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        trade_date=np.array([1], dtype=np.int64),
        open=np.array([1.0]), high=np.array([1.0]), low=np.array([1.0]),
        close=np.array([1.0]), volume=np.array([1.0]),
        amount=np.array([1.0]),
    )
    sha = hashlib.sha256(buf.getvalue()).hexdigest()
    (store.blobs_dir / f"{sha}.npz").write_bytes(buf.getvalue())
    return sha


class TestOverlayGcRetention:
    def test_base_blobs_kept_and_orphans_collected(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        orphan = _make_orphan(store)
        _age_blobs(store)
        plan = build_gc_plan(store, respect_live_locks=False)
        assert not plan.blocked
        # only the orphan is a candidate; every base blob is retained
        assert [c.sha256 for c in plan.candidates] == [orphan]

    def test_base_blobs_kept_even_after_manifest_expiry(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        overlay = __import__(
            "wtpy.apps.astock.data.delta_store",
            fromlist=["load_overlay_state"],
        ).load_overlay_state(tmp_path)
        base_id = overlay.base_dataset_id
        base_blob = store.load_manifest(base_id).symbols[0].blob_sha256
        # expire the base manifest (virtual-view cleanup step)
        (store.manifests_dir / f"{base_id}.json").unlink()
        _age_blobs(store)
        plan = build_gc_plan(store, respect_live_locks=False)
        assert not any(c.sha256 == base_blob for c in plan.candidates)

    def test_gc_fails_closed_on_missing_overlay_base(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        overlay = __import__(
            "wtpy.apps.astock.data.delta_store",
            fromlist=["load_overlay_state"],
        ).load_overlay_state(tmp_path)
        (store.manifests_dir / f"{overlay.base_dataset_id}.json").unlink()
        _age_blobs(store)
        plan = build_gc_plan(store, respect_live_locks=False)
        assert plan.blocked_by_overlay_manifest_missing == overlay.base_dataset_id
        assert plan.candidates == []

    def test_pinned_base_is_kept_by_overlay_registry(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        overlay = __import__(
            "wtpy.apps.astock.data.delta_store",
            fromlist=["load_overlay_state"],
        ).load_overlay_state(tmp_path)
        base_id = overlay.base_dataset_id
        base_blob = store.load_manifest(base_id).symbols[0].blob_sha256
        # a pin entry alone (manifest still present) never makes the blob a
        # candidate
        pins = {base_id: {"task": "manual", "reason": "keep", "created_at": "x"}}
        from wtpy.apps.astock.data.delta_store import PINS_FILE_NAME
        from wtpy.apps.astock.data.io_util import atomic_write_json

        pin_path = tmp_path / "delta" / PINS_FILE_NAME
        pin_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(pin_path, pins)
        _age_blobs(store)
        plan = build_gc_plan(store, respect_live_locks=False)
        assert not any(c.sha256 == base_blob for c in plan.candidates)
