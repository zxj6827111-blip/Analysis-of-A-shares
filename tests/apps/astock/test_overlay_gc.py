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

from wtpy.apps.astock.data.blob_gc import apply_gc_plan, build_gc_plan
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
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

    def test_gc_fails_closed_on_corrupt_overlay_state(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        (tmp_path / "delta" / "overlay_state.json").write_text(
            "{broken", encoding="utf-8"
        )
        plan = build_gc_plan(store, respect_live_locks=False)
        assert plan.blocked
        assert (
            "invalid_overlay_state"
            in plan.blocked_by_overlay_manifest_missing
        )
        assert plan.candidates == []

    def test_gc_fails_closed_on_overlay_manifest_sha_mismatch(self, tmp_path):
        import json

        store = build_overlay_warehouse(tmp_path)
        overlay = __import__(
            "wtpy.apps.astock.data.delta_store",
            fromlist=["load_overlay_state"],
        ).load_overlay_state(tmp_path)
        path = store.manifests_dir / f"{overlay.base_dataset_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provider_version"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        plan = build_gc_plan(store, respect_live_locks=False)
        assert plan.blocked
        assert plan.blocked_by_overlay_manifest_missing == (
            f"sha_mismatch:{overlay.base_dataset_id}"
        )
        assert plan.candidates == []

    def test_missing_build_parent_does_not_emit_lineage_warning(self, tmp_path):
        store = DatasetStore(tmp_path)
        store.publish(
            DatasetManifest(
                dataset_id="materialized_snapshot",
                source="tushare",
                adjustment="none",
                period="1d",
                parent_dataset_id="retired_build_parent",
            )
        )

        plan = build_gc_plan(store, respect_live_locks=False)

        assert "retired_build_parent" not in "\n".join(plan.warnings)

    @pytest.mark.parametrize("field", ["raw_dataset_id", "factor_dataset_id"])
    def test_missing_runtime_parent_still_emits_lineage_warning(
        self,
        tmp_path,
        field,
    ):
        store = DatasetStore(tmp_path)
        missing_id = f"missing_{field}"
        manifest = DatasetManifest(
            dataset_id=f"dependent_{field}",
            source="internal",
            adjustment="qfq",
            period="1d",
        )
        setattr(manifest, field, missing_id)
        store.publish(manifest)

        plan = build_gc_plan(store, respect_live_locks=False)

        matching_warnings = [
            warning for warning in plan.warnings if missing_id in warning
        ]
        assert len(matching_warnings) == 1


    def test_existing_universe_file_satisfies_runtime_dependency(self, tmp_path):
        store = DatasetStore(tmp_path)
        universe_id = "pit_universe_1d_test"
        universe_dir = store.root / "universes"
        universe_dir.mkdir(parents=True, exist_ok=True)
        (universe_dir / f"{universe_id}.json").write_text(
            "{}", encoding="utf-8"
        )
        store.publish(
            DatasetManifest(
                dataset_id="universe_consumer",
                source="internal",
                adjustment="qfq",
                period="1d",
                provenance={"universe_dataset_id": universe_id},
            )
        )

        plan = build_gc_plan(store, respect_live_locks=False)

        assert universe_id not in "\n".join(plan.warnings)

    def test_apply_fails_before_delete_when_retained_blob_is_missing(
        self, tmp_path
    ):
        store = build_overlay_warehouse(tmp_path)
        orphan = _make_orphan(store)
        _age_blobs(store)
        plan = build_gc_plan(
            store, protection_hours=0, respect_live_locks=False
        )
        overlay = __import__(
            "wtpy.apps.astock.data.delta_store",
            fromlist=["load_overlay_state"],
        ).load_overlay_state(tmp_path)
        retained = store.load_manifest(overlay.base_dataset_id)
        retained_path = (
            store.blobs_dir / f"{retained.symbols[0].blob_sha256}.npz"
        )
        retained_path.unlink()

        with pytest.raises(RuntimeError, match="PRE-DELETE"):
            apply_gc_plan(store, plan)

        assert (store.blobs_dir / f"{orphan}.npz").exists()

    def test_apply_rejects_candidate_referenced_after_plan(self, tmp_path):
        store = DatasetStore(tmp_path)
        orphan = _make_orphan(store)
        _age_blobs(store)
        plan = build_gc_plan(
            store, protection_hours=0, respect_live_locks=False
        )
        store.publish(
            DatasetManifest(
                dataset_id="late_manifest",
                source="test",
                adjustment="none",
                period="1d",
                status="ready",
                symbols=[
                    SymbolRecord(
                        symbol="SSE.STK.600000",
                        blob_sha256=orphan,
                        row_count=1,
                    )
                ],
            )
        )

        with pytest.raises(RuntimeError, match="GC PLAN STALE"):
            apply_gc_plan(store, plan)

        assert (store.blobs_dir / f"{orphan}.npz").exists()
