#!/usr/bin/env python
"""Market-data governance tools for the overlay_v1 warehouse.

Commands:
  --audit                 daily storage audit (disk, growth, new NPZ, delta,
                          orphans, watermark lag)
  --pin DATASET_ID --reason TEXT
                          pin a dataset (formal product surfaces and manual
                          tasks only; a pinned dataset is never expired)
  --unpin DATASET_ID      remove a pin
  --list-pins             list the pin registry
  --expire-virtual        drop old virtual L1/L2 manifests beyond the latest
                          + 1 retained watermark (pinned ones survive)
  --consolidate           merge the whole delta into a new base blob dataset,
                          publish it, then reset the delta store
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
import time
from pathlib import Path
from typing import Dict, List, Optional

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
from wtpy.apps.astock.data.overlay import VIRTUAL_L1_PREFIX, VIRTUAL_L2_PREFIX

#: virtual manifests older than this many watermarks behind the latest are
#: eligible for expiry (the latest and the one before it stay for rollback).
KEEP_VIRTUAL_GENERATIONS = 2

#: consolidation triggers
CONSOLIDATE_TRADING_DAYS = 60
CONSOLIDATE_DELTA_BYTES = 512 * 1024 * 1024


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
    delta = DeltaStore(store.root)
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

    # orphans = blob set not referenced by any manifest (same as GC plan)
    from wtpy.apps.astock.data.blob_gc import build_gc_plan

    gc = build_gc_plan(store, respect_live_locks=False)

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
            "total_market_data_bytes": _dir_size_bytes(store.root),
        },
        "growth_24h": {
            "new_npz_count": new_npz,
            "new_npz_bytes": new_bytes,
            "delta_rows_bars": delta_rows_bars,
            "delta_rows_factor": delta_rows_factor,
        },
        "delta": {
            "bars_rows": delta_rows_bars,
            "factor_rows": delta_rows_factor,
            "batches": len(delta.list_batches()),
            "watermark": overlay.delta_watermark if overlay.enabled else 0,
            "factor_watermark": overlay.factor_watermark if overlay.enabled else 0,
            "lag_days_since_watermark": lag_days,
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


def cmd_expire_virtual(store: DatasetStore, *, dry_run: bool = True) -> int:
    overlay = load_overlay_state(store.root)
    pins = load_pins(store.root)

    l2_ids = []
    l1_ids = []
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

    def _watermark(mid: str, m) -> int:
        # parse wm<YYYYMMDD> suffix from the id
        try:
            return int(mid.rsplit("_wm", 1)[1][:8])
        except Exception:
            return int(m.delta_watermark or 0)

    expired: List[str] = []
    for family in (l2_ids, l1_ids):
        family.sort(key=lambda t: _watermark(t[0], t[1]), reverse=True)
        for idx, (mid, m) in enumerate(family):
            if mid in pins:
                continue
            if idx < KEEP_VIRTUAL_GENERATIONS:
                continue
            if dry_run:
                print(f"  would expire {mid} (wm={_watermark(mid, m)})")
            else:
                try:
                    (store.manifests_dir / f"{mid}.json").unlink(missing_ok=True)
                    print(f"  expired {mid}")
                except OSError as e:
                    print(f"  expire failed {mid}: {e}")
            expired.append(mid)
    print(json.dumps({
        "dry_run": dry_run,
        "expired": expired,
        "kept_l2": len(l2_ids),
        "kept_l1": len(l1_ids),
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# consolidate: merge the whole delta into a new base dataset
# ---------------------------------------------------------------------------


def _consolidation_due(overlay: OverlayState, delta: DeltaStore) -> Dict:
    if not overlay.enabled:
        return {"due": False, "reason": "overlay_disabled"}
    if overlay.delta_watermark <= 0:
        return {"due": False, "reason": "no_delta"}
    if delta.db_file_size() >= CONSOLIDATE_DELTA_BYTES:
        return {"due": True, "reason": "delta_size",
                "delta_bytes": delta.db_file_size()}
    # calendar-day estimate since the base cutoff watermark
    import datetime

    try:
        d0 = datetime.datetime.strptime(
            str(overlay.delta_watermark), "%Y%m%d"
        ).date()
        days = (datetime.date.today() - d0).days
    except Exception:
        days = 0
    if days >= CONSOLIDATE_TRADING_DAYS:
        return {"due": True, "reason": "trading_days",
                "calendar_days_since_base": days}
    return {"due": False, "reason": "not_due", "calendar_days_since_base": days}


def cmd_consolidate(store: DatasetStore, *, dry_run: bool = True) -> int:
    overlay = load_overlay_state(store.root)
    if not overlay.enabled:
        print(json.dumps({"error": "overlay not enabled"}, ensure_ascii=False,
                         indent=2))
        return 2
    delta = DeltaStore(store.root)
    due = _consolidation_due(overlay, delta)
    if not due["due"]:
        print(json.dumps({"status": "skipped", "reason": due},
                         ensure_ascii=False, indent=2))
        return 0
    if dry_run:
        print(json.dumps({"status": "dry_run", "would_consolidate": True,
                          "watermark": overlay.delta_watermark,
                          "delta_bytes": delta.db_file_size()},
                         ensure_ascii=False, indent=2))
        return 0

    from wtpy.apps.astock.data.overlay import OverlayView

    view = OverlayView.from_root(store.root, required=True)
    pool = view.pool_symbols()
    wm = overlay.delta_watermark
    print(f"  consolidating {len(pool)} symbols at watermark {wm} ...")

    arrays_map = view.merged_raw_arrays_batch(pool, watermark=wm)
    records = []
    total_rows = 0
    for sym in sorted(pool):
        arr = arrays_map.get(sym)
        if arr is None or len(arr["trade_date"]) == 0:
            continue
        sha = store.store_bar_arrays(sym, arr)
        records.append({
            "symbol": sym, "blob_sha256": sha,
            "first_date": int(arr["trade_date"][0]),
            "last_date": int(arr["trade_date"][-1]),
            "row_count": len(arr["trade_date"]), "quality": "ok", "error": "",
        })
        total_rows += len(arr["trade_date"])

    import hashlib

    canonical_pre = json.dumps(
        {"source": "tushare", "adjustment": "none", "period": "1d",
         "consolidated": True, "watermark": wm, "base": overlay.base_dataset_id},
        sort_keys=True,
    )
    from wtpy.apps.astock.data.dataset_store import make_dataset_id

    new_id = make_dataset_id(
        "tushare", "none", "1d", str(wm),
        hashlib.sha256(canonical_pre.encode()).hexdigest(),
    )
    from wtpy.apps.astock.data.dataset_store import SymbolRecord

    new_base = DatasetManifest(
        dataset_id=new_id,
        source="tushare",
        adjustment="none",
        period="1d",
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=wm,
        provider_version="overlay_consolidate",
        sync_run_id=f"consolidate_{time.strftime('%Y%m%dT%H%M%S')}",
        parent_dataset_id=overlay.base_dataset_id,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        universe_type="overlay_consolidated_base",
        storage_mode="blob_snapshot",
        provenance={
            "storage_mode": "overlay_v1",
            "consolidated_from": overlay.base_dataset_id,
            "delta_watermark": wm,
        },
    )
    new_base.symbols = [SymbolRecord(**r) for r in records]
    new_base.symbol_count = len(records)
    new_base.row_count = total_rows
    new_base.expected_symbol_count = len(records)
    new_base.imported_symbol_count = len(records)
    new_base.coverage_ratio = 1.0
    store.publish(new_base)

    # reset the delta store (keep schema) and advance the overlay to the new
    # base; watermark stays = wm because the new base already contains it
    from wtpy.apps.astock.data.delta_store import delta_write_lock

    with delta_write_lock(store.root):
        with delta.connect() as conn:
            conn.execute("DELETE FROM daily_bars")
            conn.execute("DELETE FROM adj_factors")
            conn.execute("DELETE FROM sync_batches")
        overlay.base_dataset_id = new_id
        overlay.base_manifest_sha256 = new_base.manifest_sha256
        overlay.delta_watermark = wm
        save_overlay_state(store.root, overlay)

    print(json.dumps({
        "status": "consolidated",
        "new_base_dataset_id": new_id,
        "symbols": len(records),
        "rows": total_rows,
        "watermark": wm,
    }, ensure_ascii=False, indent=2))
    return 0


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
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true",
                   help="actually perform --expire-virtual / --consolidate "
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
        return cmd_consolidate(store, dry_run=not args.apply)
    print(json.dumps({"error": "no command given; see --help"},
                     ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
