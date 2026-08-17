# -*- coding: utf-8 -*-
"""Local two-generation retention and real-trading-day tests."""

from __future__ import annotations

import datetime as dt

from wtpy.apps.astock.data.blob_gc import apply_gc_plan, build_gc_plan
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    KIND_BARS,
    OverlayState,
    load_overlay_state,
    save_overlay_state,
)
from wtpy.apps.astock.data.generation_catalog import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_RETIRED,
    load_generation_catalog,
    reconcile_generation_catalog,
)
from wtpy.apps.astock.data.generation_retention import (
    apply_legacy_manifest_retention_plan,
    apply_retention_plan,
    build_legacy_manifest_retention_plan,
    build_retention_plan,
)

from .conftest import _mk_overlay_bar


def _publish_base(
    store: DatasetStore,
    dataset_id: str,
    trade_date: int,
    close: float,
) -> DatasetManifest:
    symbol = "SSE.STK.600000"
    sha = store.store_bars(
        symbol,
        [_mk_overlay_bar(symbol, trade_date, close)],
    )
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="none",
        period="1d",
        data_cutoff_date=trade_date,
        snapshot_date=trade_date,
        provider_version="test_generation",
        status="ready",
        created_at=f"{str(trade_date)[:4]}-01-01T00:00:00",
        dataset_type="bars",
        storage_mode="blob_snapshot",
    )
    manifest.symbols = [
        SymbolRecord(
            symbol=symbol,
            blob_sha256=sha,
            first_date=trade_date,
            last_date=trade_date,
            row_count=1,
            quality="ok",
        )
    ]
    manifest.symbol_count = 1
    manifest.row_count = 1
    manifest.expected_symbol_count = 1
    manifest.imported_symbol_count = 1
    manifest.coverage_ratio = 1.0
    return store.publish(manifest)


def _publish_legacy_snapshot(
    store: DatasetStore,
    dataset_id: str,
    *,
    source: str = "tushare",
    adjustment: str = "qfq",
    trade_date: int,
    close: float,
    symbol_count: int = 5000,
    parent_dataset_id: str | None = None,
) -> DatasetManifest:
    symbol = f"SSE.STK.{int(close * 1000):06d}"
    sha = store.store_bars(
        symbol,
        [_mk_overlay_bar(symbol, trade_date, close)],
    )
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period="1d",
        data_cutoff_date=trade_date,
        snapshot_date=trade_date,
        provider_version="legacy_test",
        parent_dataset_id=parent_dataset_id,
        status="ready",
        created_at=f"{str(trade_date)[:4]}-01-01T00:00:00",
        dataset_type="bars",
        storage_mode="blob_snapshot",
    )
    manifest.symbols = [
        SymbolRecord(
            symbol=symbol,
            blob_sha256=sha,
            first_date=trade_date,
            last_date=trade_date,
            row_count=1,
            quality="ok",
        )
    ]
    manifest.symbol_count = symbol_count
    manifest.row_count = symbol_count
    manifest.expected_symbol_count = symbol_count
    manifest.imported_symbol_count = symbol_count
    manifest.coverage_ratio = 1.0
    return store.publish(manifest)


def _set_overlay_created_at(store: DatasetStore, value: str) -> None:
    state = load_overlay_state(store.root)
    state.created_at = value
    save_overlay_state(store.root, state)


def _activate(
    store: DatasetStore,
    generation_id: str,
    base: DatasetManifest,
) -> None:
    delta = DeltaStore(store.root, generation_id)
    delta.init_schema()
    state = OverlayState(
        enabled=True,
        delta_store_id=generation_id,
        base_dataset_id=base.dataset_id,
        base_manifest_sha256=base.manifest_sha256,
        delta_watermark=int(base.data_cutoff_date or 0),
        factor_watermark=int(base.data_cutoff_date or 0),
        created_at=base.created_at,
    )
    save_overlay_state(store.root, state)
    reconcile_generation_catalog(store.root, state)


def _three_generations(tmp_path):
    store = DatasetStore(tmp_path)
    bases = [
        _publish_base(store, "base_generation_1", 20240105, 10.0),
        _publish_base(store, "base_generation_2", 20240405, 11.0),
        _publish_base(store, "base_generation_3", 20240705, 12.0),
    ]
    _activate(store, "main", bases[0])
    _activate(store, "gen_2", bases[1])
    _activate(store, "gen_3", bases[2])
    return store, bases


def test_retention_plan_can_reconcile_without_persisting_catalog(tmp_path):
    from .conftest import build_overlay_warehouse

    store = build_overlay_warehouse(tmp_path)
    catalog_file = tmp_path / "delta" / "generation_catalog.json"
    assert not catalog_file.exists()

    plan = build_retention_plan(store, persist_catalog=False)

    assert plan["active_generation_ids"] == ["main"]
    assert not catalog_file.exists()


def test_catalog_tracks_active_and_retired_generations(tmp_path):
    store, _ = _three_generations(tmp_path)
    catalog = load_generation_catalog(store.root)
    assert catalog["generations"]["main"]["status"] == STATUS_RETIRED
    assert catalog["generations"]["gen_2"]["status"] == STATUS_RETIRED
    assert catalog["generations"]["gen_3"]["status"] == STATUS_ACTIVE


def test_two_generation_retention_expires_oldest_and_gc_reclaims_blob(tmp_path):
    store, bases = _three_generations(tmp_path)
    old_blob = store.blobs_dir / f"{bases[0].symbols[0].blob_sha256}.npz"
    assert old_blob.exists()

    plan = build_retention_plan(
        store,
        keep_generations=2,
        grace_days=0,
        now=dt.datetime.now() + dt.timedelta(days=1),
    )
    assert plan["expire_generation_ids"] == ["main"]
    assert bases[0].dataset_id in plan["expire_manifest_ids"]
    assert plan["rollback_generation_ids"] == ["gen_2"]

    result = apply_retention_plan(store, plan)
    assert result["expired_generation_ids"] == ["main"]
    assert store.load_manifest(bases[0].dataset_id) is None
    assert store.load_manifest(bases[1].dataset_id) is not None
    assert store.load_manifest(bases[2].dataset_id) is not None
    assert not DeltaStore(store.root, "main").db_path.exists()

    gc_plan = build_gc_plan(
        store,
        protection_hours=0,
        respect_live_locks=False,
    )
    apply_gc_plan(store, gc_plan)
    assert not old_blob.exists()
    catalog = load_generation_catalog(store.root)
    assert catalog["generations"]["main"]["status"] == STATUS_EXPIRED


def test_pin_and_live_task_each_protect_old_generation(tmp_path):
    store, bases = _three_generations(tmp_path)
    common = {
        "keep_generations": 2,
        "grace_days": 0,
        "now": dt.datetime.now() + dt.timedelta(days=1),
    }

    pinned = build_retention_plan(
        store,
        pins={bases[0].dataset_id: {"reason": "formal baseline"}},
        **common,
    )
    assert pinned["expire_generation_ids"] == []
    assert "pinned" in pinned["keep_reasons"]["main"]

    live = build_retention_plan(
        store,
        live_dataset_ids=[bases[0].dataset_id],
        **common,
    )
    assert live["expire_generation_ids"] == []
    assert "live_task" in live["keep_reasons"]["main"]


def test_retained_manifest_dependency_blocks_generation_expiry(tmp_path):
    store, bases = _three_generations(tmp_path)
    dependent = DatasetManifest(
        dataset_id="legacy_product_using_generation_1",
        source="internal",
        adjustment="composite_none",
        period="1d",
        data_cutoff_date=20240105,
        snapshot_date=20240105,
        provider_version="legacy",
        raw_dataset_id=bases[0].dataset_id,
        status="ready",
        created_at="2024-01-05T00:00:00",
    )
    store.publish(dependent)

    plan = build_retention_plan(
        store,
        keep_generations=2,
        grace_days=0,
        now=dt.datetime.now() + dt.timedelta(days=1),
    )
    assert plan["expire_generation_ids"] == []
    assert "referenced_by_retained_manifest" in plan["keep_reasons"]["main"]


def test_visible_trade_dates_counts_exchange_rows_not_weekdays(tmp_path):
    delta = DeltaStore(tmp_path, "weekly")
    delta.commit_batch(
        batch_id="weekly_1",
        kind=KIND_BARS,
        source="tushare",
        adjustment="none",
        period="1d",
        base_dataset_id="base",
        watermark=20240112,
        rows={
            "SSE.STK.600000": [
                (20240105, 10.0, 10.1, 9.9, 10.0, 1.0, 10.0),
                (20240108, 10.1, 10.2, 10.0, 10.1, 1.0, 10.0),
                (20240112, 10.2, 10.3, 10.1, 10.2, 1.0, 10.0),
            ],
            "SZSE.STK.000001": [
                (20240108, 8.0, 8.1, 7.9, 8.0, 1.0, 8.0),
            ],
        },
    )
    assert delta.visible_trade_dates(KIND_BARS) == [
        20240105,
        20240108,
        20240112,
    ]

def test_legacy_retention_waits_for_migration_grace(tmp_path):
    from .conftest import build_overlay_warehouse

    store = build_overlay_warehouse(tmp_path)
    _set_overlay_created_at(store, "2026-08-16T10:00:00")
    old = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_old", trade_date=20260807, close=10.0
    )
    latest = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_latest", trade_date=20260814, close=11.0
    )

    plan = build_legacy_manifest_retention_plan(
        store,
        migration_grace_days=14,
        manifest_min_age_days=0,
        now=dt.datetime(2026, 8, 20, 10, 0, 0),
        persist_catalog=False,
    )

    assert plan["eligible"] is False
    assert plan["blocked_reason"] == "migration_grace_period"
    assert plan["expire_manifest_ids"] == []
    assert old.dataset_id in plan["deferred_candidate_manifest_ids"]
    assert latest.dataset_id not in plan["deferred_candidate_manifest_ids"]
    assert plan["cleanup_eligible_at"] == "2026-08-30T10:00:00"


def test_legacy_retention_keeps_complete_fallback_not_newer_tiny_set(tmp_path):
    from .conftest import build_overlay_warehouse

    store = build_overlay_warehouse(tmp_path)
    _set_overlay_created_at(store, "2026-07-01T00:00:00")
    complete = _publish_legacy_snapshot(
        store,
        "tushare_qfq_1d_complete",
        trade_date=20260807,
        close=12.0,
        symbol_count=5200,
    )
    tiny = _publish_legacy_snapshot(
        store,
        "tushare_qfq_1d_tiny_newer",
        trade_date=20260814,
        close=13.0,
        symbol_count=1,
    )

    plan = build_legacy_manifest_retention_plan(
        store,
        migration_grace_days=0,
        manifest_min_age_days=0,
        now=dt.datetime(2026, 8, 16, 12, 0, 0),
    )

    fallback = plan["family_fallback_manifest_ids"]["tushare/qfq/1d"]
    assert fallback == [complete.dataset_id]
    assert tiny.dataset_id in plan["expire_manifest_ids"]
    assert complete.dataset_id not in plan["expire_manifest_ids"]


def test_legacy_retention_protects_pin_live_task_and_dependency(tmp_path):
    from .conftest import build_overlay_warehouse

    store = build_overlay_warehouse(tmp_path)
    _set_overlay_created_at(store, "2026-07-01T00:00:00")
    dependent_parent = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_dependency", trade_date=20260701, close=14.0
    )
    pinned = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_pinned", trade_date=20260708, close=15.0
    )
    live = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_live", trade_date=20260715, close=16.0
    )
    latest = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_latest_safe", trade_date=20260814, close=17.0
    )
    non_legacy = DatasetManifest(
        dataset_id="localvendor_dependency_guard",
        source="local_vendor",
        adjustment="none",
        period="1d",
        raw_dataset_id=dependent_parent.dataset_id,
        status="ready",
        created_at="2026-08-01T00:00:00",
    )
    store.publish(non_legacy)

    plan = build_legacy_manifest_retention_plan(
        store,
        pins={pinned.dataset_id: {"reason": "formal replay"}},
        live_dataset_ids=[live.dataset_id],
        migration_grace_days=0,
        manifest_min_age_days=0,
        now=dt.datetime(2026, 8, 16, 12, 0, 0),
    )

    assert plan["expire_manifest_ids"] == []
    assert "pinned" in plan["keep_reasons"][pinned.dataset_id]
    assert "live_task" in plan["keep_reasons"][live.dataset_id]
    assert any(
        reason.startswith("dependency_of:localvendor_dependency_guard")
        for reason in plan["keep_reasons"][dependent_parent.dataset_id]
    )
    assert "family_fallback:tushare/qfq/1d" in plan["keep_reasons"][latest.dataset_id]


def test_legacy_retention_apply_enables_blob_gc(tmp_path):
    from .conftest import build_overlay_warehouse

    store = build_overlay_warehouse(tmp_path)
    _set_overlay_created_at(store, "2026-07-01T00:00:00")
    old = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_gc_old", trade_date=20260701, close=18.0
    )
    latest = _publish_legacy_snapshot(
        store, "tushare_qfq_1d_gc_latest", trade_date=20260814, close=19.0
    )
    old_blob = store.blobs_dir / f"{old.symbols[0].blob_sha256}.npz"

    plan = build_legacy_manifest_retention_plan(
        store,
        migration_grace_days=0,
        manifest_min_age_days=0,
        now=dt.datetime(2026, 8, 16, 12, 0, 0),
    )
    assert plan["expire_manifest_ids"] == [old.dataset_id]
    result = apply_legacy_manifest_retention_plan(store, plan)
    assert result["expired_manifest_ids"] == [old.dataset_id]
    assert store.load_manifest(old.dataset_id) is None
    assert store.load_manifest(latest.dataset_id) is not None

    gc_plan = build_gc_plan(
        store,
        protection_hours=0,
        respect_live_locks=False,
    )
    apply_gc_plan(store, gc_plan)
    assert not old_blob.exists()

