#!/usr/bin/env python
"""Market-data governance tools for the overlay_v1 warehouse.

Commands:
  --audit                 storage audit (disk, growth, generations, delta,
                          orphans, watermark lag)
  --pin DATASET_ID --reason TEXT
                          pin a dataset (formal product surfaces and manual
                          tasks only; a pinned dataset is never expired)
  --unpin DATASET_ID      remove a pin
  --list-pins             list the pin registry
  --expire-virtual        drop old virtual L1/L2 manifests beyond the latest
                          + 1 retained watermark (pinned ones survive)
  --consolidate           merge the whole delta into a new base blob dataset,
                          publish it, then start a new delta generation
  --retention-plan        read-only current/rollback generation retention plan
  --expire-generations    expire generations allowed by retention policy
  --legacy-retention-plan read-only plan for pre-overlay materialized snapshots
  --expire-legacy-manifests
                          expire eligible legacy manifests, then run blob GC
  --maintain              consolidate when due, expire virtual/legacy manifests
                          and generations, then run blob GC
  --gc-plan               print the GC retention plan (blob level)

Pins are recorded in ``delta/pins.json`` (dataset -> {task, reason,
created_at}). GC retention for blob garbage is computed by
``data/blob_gc.build_gc_plan`` which keeps the closure of ALL manifests;
pinning only guards against manifest expiry here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    KIND_BARS,
    KIND_FACTOR,
    OverlayState,
    PINS_FILE_NAME,
    load_overlay_state,
    save_overlay_state,
)
from wtpy.apps.astock.data.overlay import (
    VIRTUAL_FACTOR_PREFIX,
    VIRTUAL_L1_PREFIX,
    VIRTUAL_L2_PREFIX,
    VIRTUAL_RAW_PREFIX,
)
from wtpy.apps.astock.data.generation_catalog import (
    reconcile_generation_catalog,
    retained_delta_store_ids,
)
from wtpy.apps.astock.data.generation_retention import (
    DEFAULT_GRACE_DAYS,
    DEFAULT_KEEP_GENERATIONS,
    DEFAULT_LEGACY_KEEP_PER_FAMILY,
    DEFAULT_LEGACY_MANIFEST_MIN_AGE_DAYS,
    DEFAULT_LEGACY_MIGRATION_GRACE_DAYS,
    apply_legacy_manifest_retention_plan,
    apply_retention_plan,
    build_legacy_manifest_retention_plan,
    build_retention_plan,
    collect_live_dataset_references,
)

#: virtual manifests older than this many watermarks behind the latest are
#: eligible for expiry (the latest and the one before it stay for rollback).
KEEP_VIRTUAL_GENERATIONS = 2

#: consolidation trigger defaults (environment variables override them)
CONSOLIDATE_TRADING_DAYS = 60
CONSOLIDATE_DELTA_BYTES = 512 * 1024 * 1024


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


# ---------------------------------------------------------------------------
# pin registry
# ---------------------------------------------------------------------------


def _pins_path(root: Path) -> Path:
    return Path(root) / "delta" / PINS_FILE_NAME


def load_pins(root: Path) -> Dict[str, dict]:
    p = _pins_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pins(root: Path, pins: Dict[str, dict]) -> None:
    p = _pins_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    from wtpy.apps.astock.data.io_util import atomic_write_json

    atomic_write_json(p, pins)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def cmd_audit(store: DatasetStore) -> int:
    overlay = load_overlay_state(store.root)
    delta = DeltaStore(
        store.root, overlay.delta_store_id if overlay.enabled else "main"
    )
    now = time.time()

    blob_dir = store.blobs_dir
    blob_files = list(blob_dir.glob("*.npz")) if blob_dir.is_dir() else []
    blob_bytes = sum(f.stat().st_size for f in blob_files)

    # recent 24h new NPZ count
    cutoff = now - 86400
    new_npz = sum(1 for f in blob_files if f.stat().st_mtime >= cutoff)
    new_bytes = sum(
        f.stat().st_size for f in blob_files if f.stat().st_mtime >= cutoff
    )

    # delta size
    delta_bytes = delta.db_file_size()
    delta_rows_bars = delta.delta_row_count(KIND_BARS)
    delta_rows_factor = delta.delta_row_count(KIND_FACTOR)
    delta_files = sorted((store.root / "delta").glob("market_delta*.duckdb"))
    archived_delta_files = [p for p in delta_files if p != delta.db_path]
    archived_delta_bytes = sum(p.stat().st_size for p in archived_delta_files)
    all_delta_bytes = delta_bytes + archived_delta_bytes

    # orphans = blob set not referenced by any manifest (same as GC plan)
    from wtpy.apps.astock.data.blob_gc import build_gc_plan

    gc = build_gc_plan(store, respect_live_locks=False)
    keep_generations = _env_int(
        "ASTOCK_RETENTION_GENERATIONS",
        DEFAULT_KEEP_GENERATIONS,
        minimum=2,
    )
    grace_days = _env_int(
        "ASTOCK_RETENTION_GRACE_DAYS",
        DEFAULT_GRACE_DAYS,
        minimum=0,
    )
    live = collect_live_dataset_references()
    retention = build_retention_plan(
        store,
        pins=load_pins(store.root),
        live_dataset_ids=live["dataset_ids"],
        keep_generations=keep_generations,
        grace_days=grace_days,
        persist_catalog=False,
    )
    legacy_enabled, legacy_keep, legacy_grace, legacy_min_age = (
        _legacy_retention_settings()
    )
    legacy_retention = build_legacy_manifest_retention_plan(
        store,
        pins=load_pins(store.root),
        live_dataset_ids=live["dataset_ids"],
        keep_per_family=legacy_keep,
        migration_grace_days=legacy_grace,
        manifest_min_age_days=legacy_min_age,
        enabled=legacy_enabled,
        persist_catalog=False,
    )
    catalog = retention["catalog"]
    generation_counts = {
        "active": 0,
        "retired": 0,
        "expired": 0,
    }
    for record in catalog.get("generations", {}).values():
        status = str(record.get("status") or "")
        if status in generation_counts:
            generation_counts[status] += 1

    # watermark lag vs today
    today = int(time.strftime("%Y%m%d"))
    lag_days = (
        max(0, (today - overlay.delta_watermark)) if overlay.enabled else None
    )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "overlay_enabled": overlay.enabled,
        "disk": {
            "blob_count": len(blob_files),
            "blob_bytes": blob_bytes,
            "blob_gib": round(blob_bytes / 1024**3, 4),
            "delta_bytes": delta_bytes,
            "delta_mib": round(delta_bytes / 1024**2, 2),
            "delta_generation_count": len(delta_files),
            "archived_delta_generation_count": len(archived_delta_files),
            "archived_delta_bytes": archived_delta_bytes,
            "all_delta_bytes": all_delta_bytes,
            "all_delta_mib": round(all_delta_bytes / 1024**2, 2),
            "total_market_data_bytes": _dir_size_bytes(store.root),
        },
        "growth_24h": {
            "new_npz_count": new_npz,
            "new_npz_bytes": new_bytes,
            "delta_rows_bars": delta_rows_bars,
            "delta_rows_factor": delta_rows_factor,
        },
        "delta": {
            "store_id": delta.store_id,
            "bars_rows": delta_rows_bars,
            "factor_rows": delta_rows_factor,
            "batches": len(delta.list_batches()),
            "watermark": overlay.delta_watermark if overlay.enabled else 0,
            "factor_watermark": overlay.factor_watermark if overlay.enabled else 0,
            "delta_commit_seq": overlay.delta_commit_seq if overlay.enabled else 0,
            "factor_commit_seq": overlay.factor_commit_seq if overlay.enabled else 0,
            "lag_days_since_watermark": lag_days,
            "archived_generations": [p.name for p in archived_delta_files],
        },
        "legacy_manifests": {
            key: value
            for key, value in legacy_retention.items()
            if key != "catalog"
        },
        "generations": {
            "counts": generation_counts,
            "active_generation_ids": retention["active_generation_ids"],
            "rollback_generation_ids": retention["rollback_generation_ids"],
            "retention_candidate_ids": retention["expire_generation_ids"],
            "retention_candidate_count": len(
                retention["expire_generation_ids"]
            ),
            "estimated_reclaimable_blob_bytes": retention[
                "estimated_reclaimable_blob_bytes"
            ],
            "estimated_reclaimable_delta_bytes": retention[
                "estimated_reclaimable_delta_bytes"
            ],
            "estimated_reclaimable_total_bytes": (
                retention["estimated_reclaimable_blob_bytes"]
                + retention["estimated_reclaimable_delta_bytes"]
            ),
            "keep_generations": keep_generations,
            "grace_days": grace_days,
            "reference_scan": {
                "db_path": live["db_path"],
                "warnings": live["warnings"],
            },
        },
        "gc": gc.summarize(),
        "alerts": [],
    }
    if overlay.enabled and new_npz > 0:
        report["alerts"].append(
            f"routine EOD produced {new_npz} new NPZ blobs in 24h "
            "(overlay mode should produce none)"
        )
    if overlay.enabled and delta_bytes > 50 * 1024 * 1024:
        report["alerts"].append(
            f"delta growth high: {round(delta_bytes/1024**2, 1)} MiB"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# pin
# ---------------------------------------------------------------------------


def cmd_pin(store: DatasetStore, dataset_id: str, task: str, reason: str) -> int:
    m = store.load_manifest(dataset_id)
    if m is None:
        print(json.dumps({"error": f"dataset not found: {dataset_id}"},
                         ensure_ascii=False, indent=2))
        return 2
    pins = load_pins(store.root)
    pins[dataset_id] = {
        "task": task,
        "reason": reason,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": m.status,
    }
    save_pins(store.root, pins)
    print(json.dumps({"pinned": dataset_id, "pins": pins},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_unpin(store: DatasetStore, dataset_id: str) -> int:
    pins = load_pins(store.root)
    if dataset_id not in pins:
        print(json.dumps({"error": f"not pinned: {dataset_id}"},
                         ensure_ascii=False, indent=2))
        return 2
    del pins[dataset_id]
    save_pins(store.root, pins)
    print(json.dumps({"unpinned": dataset_id}, ensure_ascii=False, indent=2))
    return 0


def cmd_list_pins(store: DatasetStore) -> int:
    print(json.dumps({"pins": load_pins(store.root)},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# manifest expiry (virtual L1/L2 only)
# ---------------------------------------------------------------------------


def _expire_virtual_impl(store: DatasetStore, *, dry_run: bool) -> int:
    overlay = load_overlay_state(store.root)
    pins = load_pins(store.root)
    live = collect_live_dataset_references()
    if not dry_run and live["warnings"]:
        print(json.dumps({
            "error": "live dataset reference scan failed; expiry blocked",
            "reference_scan": {
                "db_path": live["db_path"],
                "warnings": live["warnings"],
            },
        }, ensure_ascii=False, indent=2))
        return 4
    protected_dataset_ids = set(pins).union(live["dataset_ids"])

    l2_ids = []
    l1_ids = []
    raw_ids = []
    factor_ids = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=False)
        if m is None:
            continue
        if getattr(m, "storage_mode", "") != "overlay_v1":
            continue
        if mid.startswith(VIRTUAL_L2_PREFIX):
            l2_ids.append((mid, m))
        elif mid.startswith(VIRTUAL_L1_PREFIX):
            l1_ids.append((mid, m))
        elif mid.startswith(VIRTUAL_RAW_PREFIX):
            raw_ids.append((mid, m))
        elif mid.startswith(VIRTUAL_FACTOR_PREFIX):
            factor_ids.append((mid, m))

    def _version_key(mid: str, m) -> tuple:
        # New r3 ids include both wm<YYYYMMDD> and seq<commit_seq>.
        try:
            tail = mid.rsplit("_wm", 1)[1]
            watermark = int(tail[:8])
        except Exception:
            watermark = int(m.delta_watermark or m.factor_watermark or 0)
        try:
            commit_seq = int(mid.rsplit("_seq", 1)[1].split("_", 1)[0])
        except Exception:
            commit_seq = max(
                int(m.delta_commit_seq or 0), int(m.factor_commit_seq or 0)
            )
        return watermark, commit_seq, str(m.created_at or "")

    expired: List[str] = []
    for family in (l2_ids, l1_ids, raw_ids, factor_ids):
        family.sort(key=lambda t: _version_key(t[0], t[1]), reverse=True)
        for idx, (mid, m) in enumerate(family):
            if mid in protected_dataset_ids:
                continue
            if idx < KEEP_VIRTUAL_GENERATIONS:
                continue
            if dry_run:
                print(f"  would expire {mid} (version={_version_key(mid, m)[:2]})")
            else:
                try:
                    (store.manifests_dir / f"{mid}.json").unlink(missing_ok=True)
                    print(f"  expired {mid}")
                except OSError as e:
                    print(f"  expire failed {mid}: {e}")
            expired.append(mid)
    referenced_store_ids = {overlay.delta_store_id} if overlay.enabled else set()
    catalog = reconcile_generation_catalog(
        store.root, overlay, persist=not dry_run
    )
    referenced_store_ids.update(retained_delta_store_ids(catalog))
    for manifest_id in store.list_manifests():
        manifest = store.load_manifest(manifest_id, deep_copy=False)
        if manifest is None or manifest.storage_mode != "overlay_v1":
            continue
        if manifest.delta_store_id:
            referenced_store_ids.add(manifest.delta_store_id)
    removed_delta_files: List[str] = []
    for db_path in sorted((store.root / "delta").glob("market_delta*.duckdb")):
        keep = any(
            DeltaStore(store.root, store_id).db_path == db_path
            for store_id in referenced_store_ids
        )
        if keep:
            continue
        removed_delta_files.append(db_path.name)
        if not dry_run:
            db_path.unlink(missing_ok=True)
            db_path.with_suffix(db_path.suffix + ".wal").unlink(missing_ok=True)

    print(json.dumps({
        "dry_run": dry_run,
        "expired": expired,
        "removed_delta_files": removed_delta_files,
        "kept_l2": len(l2_ids),
        "kept_l1": len(l1_ids),
        "kept_raw": len(raw_ids),
        "kept_factor": len(factor_ids),
        "protected_live_dataset_count": len(live["dataset_ids"]),
        "reference_scan": {
            "db_path": live["db_path"],
            "warnings": live["warnings"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_expire_virtual(store: DatasetStore, *, dry_run: bool = True) -> int:
    """Expire virtual manifests and archived generations under the writer lock."""
    if dry_run:
        return _expire_virtual_impl(store, dry_run=True)

    from wtpy.apps.astock.data.delta_store import delta_write_lock

    with delta_write_lock(store.root):
        # Re-read references under the same lock used by EOD/consolidation so
        # a newly initialized but not-yet-published generation cannot be removed.
        return _expire_virtual_impl(store, dry_run=False)


# ---------------------------------------------------------------------------
# consolidate: merge the whole delta into a new base dataset
# ---------------------------------------------------------------------------


def _consolidation_due(
    overlay: OverlayState, delta: DeltaStore, store: DatasetStore
) -> Dict:
    if not overlay.enabled:
        return {"due": False, "reason": "overlay_disabled"}
    if overlay.delta_watermark <= 0:
        return {"due": False, "reason": "no_delta"}

    delta_bytes = delta.db_file_size()
    byte_limit = _env_int(
        "ASTOCK_CONSOLIDATE_DELTA_BYTES", CONSOLIDATE_DELTA_BYTES, minimum=1
    )
    if delta_bytes >= byte_limit:
        return {
            "due": True,
            "reason": "delta_size",
            "delta_bytes": delta_bytes,
            "delta_byte_limit": byte_limit,
        }

    base = store.load_manifest(overlay.base_dataset_id, deep_copy=False)
    if base is None:
        return {"due": False, "reason": "base_manifest_missing"}
    base_cutoff = int(base.data_cutoff_date or 0)
    visible_dates = [
        value
        for value in delta.visible_trade_dates(
            KIND_BARS,
            watermark=int(overlay.delta_watermark),
            commit_seq=(
                int(overlay.delta_commit_seq)
                if overlay.delta_commit_seq
                else None
            ),
        )
        if value > base_cutoff
    ]
    trading_days = len(visible_dates)
    day_limit = _env_int(
        "ASTOCK_CONSOLIDATE_TRADING_DAYS",
        CONSOLIDATE_TRADING_DAYS,
        minimum=1,
    )
    if trading_days >= day_limit:
        return {
            "due": True,
            "reason": "trading_days",
            "trading_days_since_base": trading_days,
            "trading_day_limit": day_limit,
            "base_cutoff": base_cutoff,
        }
    return {
        "due": False,
        "reason": "not_due",
        "trading_days_since_base": trading_days,
        "trading_day_limit": day_limit,
        "base_cutoff": base_cutoff,
        "delta_bytes": delta_bytes,
        "delta_byte_limit": byte_limit,
    }


def _base_blob_bytes(store: DatasetStore, overlay: OverlayState) -> int:
    shas = set()
    for dataset_id in (
        overlay.base_dataset_id,
        overlay.delisted_base_dataset_id,
        overlay.factor_base_dataset_id,
        overlay.supplement_factor_base_dataset_id,
    ):
        if not dataset_id:
            continue
        manifest = store.load_manifest(dataset_id, deep_copy=False)
        if manifest is None:
            continue
        shas.update(
            record.blob_sha256
            for record in manifest.symbols
            if record.blob_sha256
        )
    total = 0
    for sha in shas:
        try:
            total += (store.blobs_dir / f"{sha}.npz").stat().st_size
        except OSError:
            pass
    return total


def _consolidation_disk_plan(
    store: DatasetStore,
    overlay: OverlayState,
    delta: DeltaStore,
) -> Dict:
    usage = shutil.disk_usage(store.root)
    base_bytes = _base_blob_bytes(store, overlay)
    delta_bytes = delta.db_file_size()
    estimated_new_bytes = int((base_bytes + delta_bytes) * 1.25)
    # Tiny test/prototype warehouses do not need production disk gating.
    enforce = estimated_new_bytes >= 64 * 1024 * 1024
    min_free_bytes = int(
        _env_float("ASTOCK_CONSOLIDATE_MIN_FREE_GB", 5.0) * 1024**3
    )
    max_used_pct = min(
        99.0,
        _env_float("ASTOCK_CONSOLIDATE_MAX_DISK_USAGE_PCT", 80.0),
    )
    projected_free = usage.free - estimated_new_bytes
    projected_used_pct = (
        ((usage.used + estimated_new_bytes) / usage.total) * 100.0
        if usage.total
        else 100.0
    )
    allowed = (
        not enforce
        or (
            projected_free >= min_free_bytes
            and projected_used_pct <= max_used_pct
        )
    )
    return {
        "allowed": allowed,
        "enforced": enforce,
        "base_bytes": base_bytes,
        "delta_bytes": delta_bytes,
        "estimated_new_base_bytes": estimated_new_bytes,
        "free_bytes": usage.free,
        "projected_free_bytes": projected_free,
        "projected_used_pct": round(projected_used_pct, 2),
        "min_free_bytes": min_free_bytes,
        "max_used_pct": max_used_pct,
    }


def _publish_consolidated_raw(
    store: DatasetStore,
    view,
    overlay: OverlayState,
    watermark: int,
    *,
    pool: Optional[List[str]] = None,
    arrays_map: Optional[Dict[str, dict]] = None,
) -> Tuple[DatasetManifest, Optional[DatasetManifest]]:
    import hashlib

    from wtpy.apps.astock.data.dataset_store import SymbolRecord, make_dataset_id

    pool = list(pool) if pool is not None else view.pool_symbols()
    arrays_map = (
        arrays_map
        if arrays_map is not None
        else view.merged_raw_arrays_batch(pool, watermark=watermark)
    )

    active_base = view.active_base()
    active_symbols = {
        record.symbol for record in active_base.symbols if record.blob_sha256
    }
    delisted_base = view.delisted_base()
    delisted_symbols = {
        record.symbol
        for record in (delisted_base.symbols if delisted_base is not None else [])
        if record.blob_sha256 and record.symbol not in active_symbols
    }
    # Routine delta-only symbols are new listings. Keep them active while
    # retaining every known delisted symbol in a separate immutable base.
    active_symbols.update(set(pool) - delisted_symbols)

    def _records_for(symbols) -> Tuple[List[SymbolRecord], int]:
        records: List[SymbolRecord] = []
        total_rows = 0
        for symbol in sorted(symbols):
            arrays = arrays_map.get(symbol)
            if arrays is None or len(arrays["trade_date"]) == 0:
                raise RuntimeError(
                    f"consolidation raw source unavailable: {symbol}"
                )
            sha = store.store_bar_arrays(symbol, arrays)
            records.append(SymbolRecord(
                symbol=symbol,
                blob_sha256=sha,
                first_date=int(arrays["trade_date"][0]),
                last_date=int(arrays["trade_date"][-1]),
                row_count=len(arrays["trade_date"]),
                quality="ok",
            ))
            total_rows += len(arrays["trade_date"])
        return records, total_rows

    records, total_rows = _records_for(active_symbols)
    canonical = json.dumps(
        {
            "kind": "raw",
            "pool": "active_only_v1",
            "base": overlay.base_dataset_id,
            "store": overlay.delta_store_id,
            "watermark": watermark,
        },
        sort_keys=True,
    )
    dataset_id = make_dataset_id(
        "tushare", "none", "1d", str(watermark),
        hashlib.sha256(canonical.encode()).hexdigest(),
    )
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="none",
        period="1d",
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=watermark,
        provider_version="overlay_consolidate_v2",
        sync_run_id=f"consolidate_{time.strftime('%Y%m%dT%H%M%S')}",
        parent_dataset_id=None,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        universe_type="overlay_consolidated_active_base",
        storage_mode="blob_snapshot",
        provenance={
            "storage_mode": "overlay_v1",
            "consolidated_from": overlay.base_dataset_id,
            "delta_store_id": overlay.delta_store_id,
            "delta_watermark": watermark,
            "includes_delta_only": True,
            "includes_delisted": False,
        },
    )
    manifest.symbols = records
    manifest.symbol_count = len(records)
    manifest.row_count = total_rows
    manifest.expected_symbol_count = len(records)
    manifest.imported_symbol_count = len(records)
    manifest.coverage_ratio = 1.0
    active_manifest = store.publish(manifest)

    if not delisted_symbols:
        return active_manifest, None

    delisted_records, delisted_rows = _records_for(delisted_symbols)
    if not delisted_records:
        return active_manifest, None
    delisted_cutoff = max(int(record.last_date or 0) for record in delisted_records)
    delisted_canonical = json.dumps(
        {
            "kind": "delisted",
            "pool": "delisted_only_v1",
            "base": overlay.delisted_base_dataset_id,
            "store": overlay.delta_store_id,
            "watermark": watermark,
        },
        sort_keys=True,
    )
    delisted_dataset_id = make_dataset_id(
        "internal",
        "delisted_complement",
        "1d",
        str(delisted_cutoff),
        hashlib.sha256(delisted_canonical.encode()).hexdigest(),
    )
    delisted_manifest = DatasetManifest(
        dataset_id=delisted_dataset_id,
        source="internal",
        adjustment="delisted_complement",
        period="1d",
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=delisted_cutoff,
        provider_version="overlay_consolidate_v2",
        sync_run_id=f"consolidate_delisted_{time.strftime('%Y%m%dT%H%M%S')}",
        parent_dataset_id=None,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        universe_type=(
            delisted_base.universe_type
            if delisted_base is not None and delisted_base.universe_type
            else "delisted_complement"
        ),
        storage_mode="blob_snapshot",
        survivorship_bias=False,
        historical_universe_complete=True,
        delisted_coverage_complete=True,
        provenance={
            "storage_mode": "overlay_v1",
            "consolidated_from": overlay.delisted_base_dataset_id,
            "delta_store_id": overlay.delta_store_id,
            "delta_watermark": watermark,
            "pool": "delisted_only",
        },
    )
    delisted_manifest.symbols = delisted_records
    delisted_manifest.symbol_count = len(delisted_records)
    delisted_manifest.row_count = delisted_rows
    delisted_manifest.expected_symbol_count = len(delisted_records)
    delisted_manifest.imported_symbol_count = len(delisted_records)
    delisted_manifest.coverage_ratio = 1.0
    return active_manifest, store.publish(delisted_manifest)

def _publish_consolidated_factors(
    store: DatasetStore,
    view,
    overlay: OverlayState,
    watermark: int,
    *,
    symbols: Optional[List[str]] = None,
    arrays_map: Optional[Dict[str, dict]] = None,
) -> DatasetManifest:
    import hashlib

    from wtpy.apps.astock.data.dataset_store import SymbolRecord, make_dataset_id

    factor_view = view.factor_virtual_manifest()
    symbols = (
        list(symbols)
        if symbols is not None
        else [record.symbol for record in factor_view.symbols]
    )
    arrays_map = (
        arrays_map
        if arrays_map is not None
        else view.factor_arrays_batch(symbols, watermark=watermark)
    )
    records = []
    total_rows = 0
    for symbol in sorted(symbols):
        arrays = arrays_map.get(symbol)
        if arrays is None or len(arrays["trade_date"]) == 0:
            raise RuntimeError(
                f"consolidation factor source unavailable: {symbol}"
            )
        sha = store.store_factors(
            symbol, arrays["trade_date"], arrays["adj_factor"]
        )
        records.append(SymbolRecord(
            symbol=symbol,
            blob_sha256=sha,
            first_date=int(arrays["trade_date"][0]),
            last_date=int(arrays["trade_date"][-1]),
            row_count=len(arrays["trade_date"]),
            quality="ok",
        ))
        total_rows += len(arrays["trade_date"])
    canonical = json.dumps(
        {
            "kind": "factor",
            "base": overlay.factor_base_dataset_id,
            "supplement": overlay.supplement_factor_base_dataset_id,
            "store": overlay.delta_store_id,
            "watermark": watermark,
        },
        sort_keys=True,
    )
    dataset_id = make_dataset_id(
        "tushare", "adj_factor", "1d", str(watermark),
        hashlib.sha256(canonical.encode()).hexdigest(),
    )
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="adj_factor",
        period="1d",
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=watermark,
        provider_version="overlay_consolidate_v2",
        sync_run_id=f"consolidate_factor_{time.strftime('%Y%m%dT%H%M%S')}",
        parent_dataset_id=None,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="factor",
        universe_type="overlay_consolidated_factor_base",
        storage_mode="blob_snapshot",
        provenance={
            "storage_mode": "overlay_v1",
            "consolidated_from": overlay.factor_base_dataset_id,
            "supplement_factor_base_dataset_id": (
                overlay.supplement_factor_base_dataset_id
            ),
            "delta_store_id": overlay.delta_store_id,
            "factor_watermark": watermark,
        },
    )
    manifest.symbols = records
    manifest.symbol_count = len(records)
    manifest.row_count = total_rows
    manifest.expected_symbol_count = len(records)
    manifest.imported_symbol_count = len(records)
    manifest.coverage_ratio = 1.0
    return store.publish(manifest)


def cmd_consolidate(
    store: DatasetStore, *, dry_run: bool = True, force: bool = False
) -> int:
    overlay = load_overlay_state(store.root)
    if not overlay.enabled:
        print(json.dumps({"error": "overlay not enabled"}, ensure_ascii=False,
                         indent=2))
        return 2
    reconcile_generation_catalog(
        store.root,
        overlay,
        persist=not dry_run,
    )
    delta = DeltaStore(store.root, overlay.delta_store_id)
    due = _consolidation_due(overlay, delta, store)
    disk = _consolidation_disk_plan(store, overlay, delta)
    if not due["due"] and not force:
        print(json.dumps({"status": "skipped", "reason": due},
                         ensure_ascii=False, indent=2))
        return 0
    if dry_run:
        print(json.dumps({
            "status": "dry_run",
            "would_consolidate": True,
            "watermark": overlay.delta_watermark,
            "factor_watermark": overlay.factor_watermark,
            "delta_store_id": overlay.delta_store_id,
            "delta_bytes": delta.db_file_size(),
            "reason": due,
            "disk": disk,
        }, ensure_ascii=False, indent=2))
        return 0
    if not disk["allowed"]:
        print(json.dumps({
            "error": "consolidation blocked by disk guard",
            "disk": disk,
        }, ensure_ascii=False, indent=2))
        return 3

    from wtpy.apps.astock.data.delta_store import delta_write_lock
    from wtpy.apps.astock.data.overlay import OverlayView

    with delta_write_lock(store.root):
        overlay = load_overlay_state(store.root)
        delta = DeltaStore(store.root, overlay.delta_store_id)
        due = _consolidation_due(overlay, delta, store)
        if not due["due"] and not force:
            print(json.dumps({"status": "skipped", "reason": due},
                             ensure_ascii=False, indent=2))
            return 0
        disk = _consolidation_disk_plan(store, overlay, delta)
        if not disk["allowed"]:
            print(json.dumps({
                "error": "consolidation blocked by disk guard",
                "disk": disk,
            }, ensure_ascii=False, indent=2))
            return 3
        view = OverlayView.from_root(store.root, required=True)
        raw_watermark = int(overlay.delta_watermark)
        factor_watermark = int(overlay.factor_watermark)
        print(
            f"  consolidating store={overlay.delta_store_id} "
            f"raw_wm={raw_watermark} factor_wm={factor_watermark} ..."
        )

        # Validate every source before writing any new immutable base blobs.
        # Missing/corrupt blobs must never be converted into a smaller base.
        raw_pool = view.pool_symbols()
        raw_arrays_map = view.merged_raw_arrays_batch(
            raw_pool, watermark=raw_watermark
        )
        missing_raw = [
            symbol for symbol in raw_pool
            if raw_arrays_map.get(symbol) is None
            or len(raw_arrays_map[symbol]["trade_date"]) == 0
        ]
        factor_view = view.factor_virtual_manifest()
        factor_symbols = [record.symbol for record in factor_view.symbols]
        factor_arrays_map = view.factor_arrays_batch(
            factor_symbols, watermark=factor_watermark
        )
        missing_factors = [
            symbol for symbol in factor_symbols
            if factor_arrays_map.get(symbol) is None
            or len(factor_arrays_map[symbol]["trade_date"]) == 0
        ]
        if not raw_pool or not factor_symbols or missing_raw or missing_factors:
            print(json.dumps({
                "error": "consolidation source validation failed",
                "missing_raw_count": len(missing_raw),
                "missing_raw_sample": missing_raw[:20],
                "missing_factor_count": len(missing_factors),
                "missing_factor_sample": missing_factors[:20],
                "raw_symbol_count": len(raw_pool),
                "factor_symbol_count": len(factor_symbols),
            }, ensure_ascii=False, indent=2))
            return 4

        new_raw, new_delisted = _publish_consolidated_raw(
            store,
            view,
            overlay,
            raw_watermark,
            pool=raw_pool,
            arrays_map=raw_arrays_map,
        )
        new_factor = _publish_consolidated_factors(
            store,
            view,
            overlay,
            factor_watermark,
            symbols=factor_symbols,
            arrays_map=factor_arrays_map,
        )
        generation_id = (
            f"gen_{raw_watermark}_{new_raw.dataset_id[-8:]}_"
            f"{(new_delisted.dataset_id[-8:] if new_delisted else 'none')}_"
            f"{new_factor.dataset_id[-8:]}"
        )
        next_delta = DeltaStore(store.root, generation_id)
        next_delta.init_schema()

        old_store_id = overlay.delta_store_id
        overlay.delta_store_id = generation_id
        overlay.base_dataset_id = new_raw.dataset_id
        overlay.base_manifest_sha256 = new_raw.manifest_sha256
        overlay.delisted_base_dataset_id = (
            new_delisted.dataset_id if new_delisted is not None else ""
        )
        overlay.delisted_base_manifest_sha256 = (
            new_delisted.manifest_sha256 if new_delisted is not None else ""
        )
        overlay.factor_base_dataset_id = new_factor.dataset_id
        overlay.factor_base_manifest_sha256 = new_factor.manifest_sha256
        overlay.supplement_factor_base_dataset_id = ""
        overlay.supplement_factor_base_manifest_sha256 = ""
        overlay.delta_commit_seq = 0
        overlay.factor_commit_seq = 0
        save_overlay_state(store.root, overlay)
        reconcile_generation_catalog(store.root, overlay)

    print(json.dumps({
        "status": "consolidated",
        "new_base_dataset_id": new_raw.dataset_id,
        "new_delisted_base_dataset_id": (
            new_delisted.dataset_id if new_delisted is not None else None
        ),
        "new_factor_base_dataset_id": new_factor.dataset_id,
        "archived_delta_store_id": old_store_id,
        "active_delta_store_id": generation_id,
        "symbols": new_raw.symbol_count,
        "rows": new_raw.row_count,
        "delisted_symbols": (
            new_delisted.symbol_count if new_delisted is not None else 0
        ),
        "delisted_rows": (
            new_delisted.row_count if new_delisted is not None else 0
        ),
        "factor_symbols": new_factor.symbol_count,
        "factor_rows": new_factor.row_count,
        "watermark": raw_watermark,
        "factor_watermark": factor_watermark,
    }, ensure_ascii=False, indent=2))
    return 0




# ---------------------------------------------------------------------------
# generation retention + one-shot maintenance
# ---------------------------------------------------------------------------


def _retention_settings() -> Tuple[int, int]:
    return (
        _env_int(
            "ASTOCK_RETENTION_GENERATIONS",
            DEFAULT_KEEP_GENERATIONS,
            minimum=2,
        ),
        _env_int(
            "ASTOCK_RETENTION_GRACE_DAYS",
            DEFAULT_GRACE_DAYS,
            minimum=0,
        ),
    )


def _legacy_retention_settings() -> Tuple[bool, int, int, int]:
    return (
        _env_bool("ASTOCK_LEGACY_RETENTION_ENABLED", True),
        _env_int(
            "ASTOCK_LEGACY_KEEP_PER_FAMILY",
            DEFAULT_LEGACY_KEEP_PER_FAMILY,
            minimum=1,
        ),
        _env_int(
            "ASTOCK_LEGACY_MIGRATION_GRACE_DAYS",
            DEFAULT_LEGACY_MIGRATION_GRACE_DAYS,
            minimum=0,
        ),
        _env_int(
            "ASTOCK_LEGACY_MANIFEST_MIN_AGE_DAYS",
            DEFAULT_LEGACY_MANIFEST_MIN_AGE_DAYS,
            minimum=0,
        ),
    )


def _apply_blob_gc(store: DatasetStore) -> Tuple[int, Optional[dict]]:
    from wtpy.apps.astock.data.blob_gc import apply_gc_plan, build_gc_plan

    gc_plan = build_gc_plan(store)
    if gc_plan.blocked:
        return 4, gc_plan.summarize()
    return 0, apply_gc_plan(store, gc_plan)


def cmd_legacy_retention(
    store: DatasetStore,
    *,
    dry_run: bool = True,
    run_gc: bool = True,
) -> int:
    enabled, keep_per_family, migration_grace, manifest_min_age = (
        _legacy_retention_settings()
    )

    def _plan(live: dict) -> dict:
        return build_legacy_manifest_retention_plan(
            store,
            pins=load_pins(store.root),
            live_dataset_ids=live["dataset_ids"],
            keep_per_family=keep_per_family,
            migration_grace_days=migration_grace,
            manifest_min_age_days=manifest_min_age,
            enabled=enabled,
            persist_catalog=not dry_run,
        )

    live = collect_live_dataset_references()
    if dry_run:
        plan = _plan(live)
        output = {key: value for key, value in plan.items() if key != "catalog"}
        output["dry_run"] = True
        output["reference_scan"] = {
            "db_path": live["db_path"],
            "warnings": live["warnings"],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    from wtpy.apps.astock.data.delta_store import delta_write_lock

    with delta_write_lock(store.root):
        live = collect_live_dataset_references()
        if live["warnings"]:
            print(json.dumps({
                "error": "live dataset reference scan failed; legacy expiry blocked",
                "reference_scan": {
                    "db_path": live["db_path"],
                    "warnings": live["warnings"],
                },
            }, ensure_ascii=False, indent=2))
            return 4
        plan = _plan(live)
        result = apply_legacy_manifest_retention_plan(store, plan)

    gc_result = None
    if run_gc and result["expired_manifest_ids"]:
        rc, gc_result = _apply_blob_gc(store)
        if rc:
            print(json.dumps({
                "error": "legacy manifest expiry applied but blob GC was blocked",
                "legacy_retention": result,
                "gc": gc_result,
            }, ensure_ascii=False, indent=2))
            return rc

    print(json.dumps({
        "dry_run": False,
        "legacy_retention": result,
        "gc": gc_result,
        "reference_scan": {
            "db_path": live["db_path"],
            "warnings": live["warnings"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_retention(store: DatasetStore, *, dry_run: bool = True) -> int:
    keep_generations, grace_days = _retention_settings()
    live = collect_live_dataset_references()

    def _plan() -> dict:
        return build_retention_plan(
            store,
            pins=load_pins(store.root),
            live_dataset_ids=live["dataset_ids"],
            keep_generations=keep_generations,
            grace_days=grace_days,
            persist_catalog=not dry_run,
        )

    if dry_run:
        plan = _plan()
        output = {key: value for key, value in plan.items() if key != "catalog"}
        output["dry_run"] = True
        output["reference_scan"] = {
            "db_path": live["db_path"],
            "warnings": live["warnings"],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    from wtpy.apps.astock.data.delta_store import delta_write_lock

    with delta_write_lock(store.root):
        # Re-read every reference while holding the writer lock. This prevents
        # a new EOD generation from being published between plan and expiry.
        live = collect_live_dataset_references()
        if live["warnings"]:
            print(json.dumps({
                "error": "live dataset reference scan failed; generation expiry blocked",
                "reference_scan": {
                    "db_path": live["db_path"],
                    "warnings": live["warnings"],
                },
            }, ensure_ascii=False, indent=2))
            return 4
        plan = _plan()
        result = apply_retention_plan(store, plan)

    rc, gc_result = _apply_blob_gc(store)
    if rc:
        print(json.dumps({
            "error": "retention applied but blob GC was blocked",
            "retention": result,
            "gc": gc_result,
        }, ensure_ascii=False, indent=2))
        return rc

    print(json.dumps({
        "dry_run": False,
        "retention": result,
        "gc": gc_result,
        "reference_scan": {
            "db_path": live["db_path"],
            "warnings": live["warnings"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_maintain(store: DatasetStore, *, dry_run: bool = True) -> int:
    rc = cmd_consolidate(store, dry_run=dry_run, force=False)
    if rc:
        return rc
    rc = cmd_expire_virtual(store, dry_run=dry_run)
    if rc:
        return rc
    rc = cmd_legacy_retention(store, dry_run=dry_run, run_gc=False)
    if rc:
        return rc
    return cmd_retention(store, dry_run=dry_run)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Market-data governance")
    p.add_argument("--storage-root", default=None)
    p.add_argument("--audit", action="store_true")
    p.add_argument("--pin", default=None, dest="pin_dataset")
    p.add_argument("--task", default="manual")
    p.add_argument("--reason", default="")
    p.add_argument("--unpin", default=None)
    p.add_argument("--list-pins", action="store_true")
    p.add_argument("--expire-virtual", action="store_true")
    p.add_argument("--consolidate", action="store_true")
    p.add_argument("--force-consolidate", action="store_true")
    p.add_argument("--retention-plan", action="store_true")
    p.add_argument("--expire-generations", action="store_true")
    p.add_argument("--legacy-retention-plan", action="store_true")
    p.add_argument("--expire-legacy-manifests", action="store_true")
    p.add_argument("--maintain", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true",
                   help="actually perform expiry / consolidation / maintenance "
                        "(default is dry-run)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        from wtpy.apps.astock.config import load_env_file

        load_env_file()
    except Exception:
        pass
    env_root = os.environ.get("MARKET_DATA_ROOT", "").strip()
    root = Path(args.storage_root or env_root or "storage/astock/market_data")
    store = DatasetStore(root)

    if args.audit:
        return cmd_audit(store)
    if args.pin_dataset:
        return cmd_pin(store, args.pin_dataset, args.task, args.reason)
    if args.unpin:
        return cmd_unpin(store, args.unpin)
    if args.list_pins:
        return cmd_list_pins(store)
    if args.expire_virtual:
        return cmd_expire_virtual(store, dry_run=not args.apply)
    if args.consolidate:
        return cmd_consolidate(
            store, dry_run=not args.apply, force=args.force_consolidate
        )
    if args.retention_plan:
        return cmd_retention(store, dry_run=True)
    if args.expire_generations:
        return cmd_retention(store, dry_run=not args.apply)
    if args.legacy_retention_plan:
        return cmd_legacy_retention(store, dry_run=True)
    if args.expire_legacy_manifests:
        return cmd_legacy_retention(store, dry_run=not args.apply)
    if args.maintain:
        return cmd_maintain(store, dry_run=not args.apply)
    print(json.dumps({"error": "no command given; see --help"},
                     ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
