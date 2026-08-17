"""Rewrite existing Tushare daily blobs to the share/CNY unit standard.

Background: TushareProvider used to store `vol` (手) and `amount` (千元)
verbatim; the delisted pool, local_vendor and minute_vendor surfaces all
store 股/元 (x100 / x1000). The formal L2 composite was therefore unit-mixed
and a hybrid_seeded_v1 dataset would break at the seam. After
TushareProvider.fetch_bars started normalizing units, this script rewrites
the latest ready tushare/none + tushare/qfq blobs in place (volume*100,
amount*1000) and republishes new manifests so the whole warehouse is on the
share/CNY standard without a full Tushare refetch.

Old manifests/blobs are NOT deleted (rollback = restore the backed-up
manifests; their blobs still exist).

Usage:
  python scripts/rewrite_tushare_units.py --storage-root <root> [--adjustment none|qfq|both]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
)
from wtpy.apps.astock.data.sync_lock import SyncLockHeldError, SyncTaskLock

REWRITE_TAG = "unit_rewrite_share_yuan_20260815"


def _latest_ready(store: DatasetStore, source: str, adjustment: str) -> Optional[DatasetManifest]:
    cands = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=False)
        if not m or m.status != "ready":
            continue
        if m.source == source and m.adjustment == adjustment and m.period == "1d":
            cands.append(m)
    if not cands:
        return None
    return max(cands, key=lambda m: (int(m.data_cutoff_date or 0), int(m.symbol_count or 0)))


def rewrite_manifest(store: DatasetStore, m: DatasetManifest) -> Optional[str]:
    print(f"Rewriting {m.dataset_id} ({m.symbol_count} symbols)...")
    new_records: List[SymbolRecord] = []
    rewritten = 0
    for rec in m.symbols or []:
        if not rec.blob_sha256 or rec.quality != "ok":
            new_records.append(rec)
            continue
        arr = store.load_bars(rec.blob_sha256)
        new_arrays = dict(arr)
        new_arrays["volume"] = np.asarray(arr["volume"], dtype=np.float64) * 100.0
        new_arrays["amount"] = np.asarray(arr["amount"], dtype=np.float64) * 1000.0
        new_sha = store.store_bar_arrays(rec.symbol, new_arrays)
        new_records.append(SymbolRecord(
            symbol=rec.symbol, blob_sha256=new_sha,
            first_date=rec.first_date, last_date=rec.last_date,
            row_count=rec.row_count, quality="ok",
        ))
        rewritten += 1

    pre = json.dumps({
        "source": m.source, "adjustment": m.adjustment, "period": m.period,
        "cutoff": str(m.data_cutoff_date), "sync_run_id": REWRITE_TAG,
        "symbols": sorted(r.symbol for r in m.symbols or []),
    }, sort_keys=True)
    pre_sha = hashlib.sha256(pre.encode()).hexdigest()
    new_id = make_dataset_id(
        m.source.replace("_", ""), m.adjustment, m.period,
        str(m.data_cutoff_date or 0), pre_sha,
    )
    if store.load_manifest(new_id, deep_copy=False) is not None:
        print(f"  already exists: {new_id}")
        return new_id

    prov = dict(m.provenance or {})
    prov["unit_rewrite"] = {
        "from": "tushare_raw_vol_lots_amount_kcny",
        "to": "share_yuan",
        "volume_x": 100.0,
        "amount_x": 1000.0,
        "tag": REWRITE_TAG,
        "parent_manifest": m.dataset_id,
    }
    nm = DatasetManifest(
        dataset_id=new_id,
        source=m.source,
        adjustment=m.adjustment,
        period=m.period,
        weekly_bar_mode=m.weekly_bar_mode,
        anchor_date=m.anchor_date,
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=m.data_cutoff_date,
        provider_version=m.provider_version,
        sync_run_id=REWRITE_TAG,
        parent_dataset_id=m.parent_dataset_id,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        universe_type=m.universe_type,
        dataset_type=m.dataset_type,
        coverage_start_year=m.coverage_start_year,
        coverage_end_year=m.coverage_end_year,
        provenance=prov,
        symbols=new_records,
        incremental_policy_version="unit_share_yuan_v1",
    )
    nm.symbol_count = len(new_records)
    nm.row_count = m.row_count
    nm.expected_symbol_count = m.expected_symbol_count
    nm.imported_symbol_count = rewritten
    nm.no_data_symbol_count = m.no_data_symbol_count
    nm.coverage_ratio = m.coverage_ratio
    store.publish(nm)
    print(f"  published {new_id} ({rewritten} blobs rewritten) -> {nm.status}")
    return new_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storage-root", required=True)
    ap.add_argument("--adjustment", default="both", choices=("none", "qfq", "both"))
    args = ap.parse_args()

    root = Path(args.storage_root)
    if not (root / "manifests").is_dir():
        print(f"ERROR: not a market-data root: {root}")
        return 1
    store = DatasetStore(root)

    lock = SyncTaskLock(root, source="gc", adjustment="unit_rewrite", period="all")
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return 1
    try:
        for adj in ("none", "qfq"):
            if args.adjustment != "both" and args.adjustment != adj:
                continue
            m = _latest_ready(store, "tushare", adj)
            if m is None:
                print(f"No ready tushare/{adj} manifest; skip.")
                continue
            rewrite_manifest(store, m)
        print("Done.")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
