"""Sync vendor-exported 60-minute CSV archives into the market-data warehouse.

Imports 60-minute bars from the Tongdaxin exported CSV archives into the
existing content-addressed DatasetStore (blobs/ + manifests/), period="60m",
adjustment="none". The import is a one-way conversion — the CSV archives
remain the source of truth until a daily minute incremental source exists.

Dataset identity:
  source=minute_vendor, adjustment=none, period=60m, dataset_type=bars
  (period "60m" is distinct from the day/week/month periods the daily
  product chain resolves, so this dataset never collides with L1/L2)

Blob layout is identical to daily bars (trade_date/open/high/low/close/
volume/amount) where trade_date carries the encoded intraday key
YYYYMMDD*100+(bucket+1), bucket 0..3 = 10:30/11:30/14:00/15:00 — the same
encoding ``minline_reader.aggregate_min60`` produces, so
``min60_bars_to_arrays`` can consume it unchanged.

Usage:
  python scripts/sync_minute_60m.py --storage-root <root> [--minute-root <root>]
                                    [--universe-file <csv>] [--dry-run]
                                    [--chunk-size 1500] [--end-date 20260717]

  --minute-root defaults to MINUTE_VENDOR_ROOT then LOCAL_VENDOR_RAW_ROOT.
  --universe-file defaults to the ready local_vendor/none/1d manifest's
  symbols (the same 5,796-symbol pool used for the daily hybrid seeding).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    evaluate_strict_publish,
    make_dataset_id,
    make_sync_run_id,
)
from wtpy.apps.astock.data.minute_vendor import MinuteVendorReader
from wtpy.apps.astock.data.sync_lock import SyncLockHeldError, SyncTaskLock

SOURCE = "minute_vendor"
ADJUSTMENT = "none"
PERIOD = "60m"


def _resolve_minute_root(args) -> Optional[Path]:
    v = getattr(args, "minute_root", None)
    if v:
        return Path(v)
    for key in ("MINUTE_VENDOR_ROOT", "LOCAL_VENDOR_RAW_ROOT"):
        env = os.environ.get(key, "").strip()
        if env:
            return Path(env)
    return None


def _universe_from_local_vendor(store: DatasetStore) -> List[str]:
    """Reuse the ready local_vendor/none/1d symbol pool (hybrid seed set)."""
    cands = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=False)
        if not m or m.status != "ready":
            continue
        if m.source == "local_vendor" and m.adjustment == "none" and m.period == "1d":
            cands.append(m)
    if not cands:
        return []
    best = max(cands, key=lambda m: int(m.data_cutoff_date or 0))
    return sorted(r.symbol for r in (best.symbols or []) if r.quality == "ok")


def main() -> int:
    ap = argparse.ArgumentParser(description="Import 60-minute CSV archives into warehouse")
    ap.add_argument("--storage-root", required=True)
    ap.add_argument("--minute-root", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--end-date", type=int, default=20260717)
    ap.add_argument("--chunk-size", type=int, default=1500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.storage_root)
    if not (root / "manifests").is_dir():
        print(f"ERROR: not a market-data root: {root}")
        return 1
    store = DatasetStore(root)

    minute_root = _resolve_minute_root(args)
    if not minute_root:
        print("ERROR: minute root not set (--minute-root or MINUTE_VENDOR_ROOT env)")
        return 1
    reader = MinuteVendorReader(minute_root)
    if not reader.health_check():
        print(f"ERROR: no 60-minute archives under {minute_root}")
        return 1
    print(f"Minute archives: {reader.archive_count()} ZIPs under {minute_root}")

    if args.universe_file:
        import csv as _csv

        with open(args.universe_file, encoding="utf-8-sig") as f:
            symbols = sorted({r["canonical_symbol"] for r in _csv.DictReader(f)})
    else:
        symbols = _universe_from_local_vendor(store)
    if not symbols:
        print("ERROR: empty universe (pass --universe-file or run local_vendor full)")
        return 1
    print(f"Universe: {len(symbols)} symbols")

    sync_run_id = make_sync_run_id("minute60m")
    lock = SyncTaskLock(root, source="minute_vendor", adjustment="none", period="60m",
                        sync_run_id=sync_run_id)
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return 1
    try:
        cutoff = args.end_date
        canonical_pre = json.dumps(
            {"source": SOURCE, "adjustment": ADJUSTMENT, "period": PERIOD,
             "sync_run_id": sync_run_id, "symbols": symbols, "cutoff": cutoff},
            sort_keys=True,
        )
        pre_sha = hashlib.sha256(canonical_pre.encode()).hexdigest()
        dataset_id = make_dataset_id(SOURCE, ADJUSTMENT, PERIOD, str(cutoff), pre_sha)

        records: List[SymbolRecord] = []
        total_rows = 0
        chunk_size = max(1, int(args.chunk_size))
        no_data_reasons: Dict[str, str] = {}
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            t0 = time.time()
            data = reader.fetch_chunk(chunk, start=None, end=cutoff)
            for sym in chunk:
                bars = data.get(sym) or []
                if not bars:
                    records.append(SymbolRecord(symbol=sym, blob_sha256="",
                                                quality="no_data", error="no_minute_data"))
                    no_data_reasons[sym] = "no_60min_csv_archive"
                    continue
                arrays = {
                    "trade_date": np.array([b.date for b in bars], dtype=np.int64),
                    "open": np.array([b.open for b in bars], dtype=np.float64),
                    "high": np.array([b.high for b in bars], dtype=np.float64),
                    "low": np.array([b.low for b in bars], dtype=np.float64),
                    "close": np.array([b.close for b in bars], dtype=np.float64),
                    "volume": np.array([b.volume for b in bars], dtype=np.float64),
                    "amount": np.array([b.amount for b in bars], dtype=np.float64),
                }
                if args.dry_run:
                    sha = "dryrun"
                else:
                    sha = store.store_bar_arrays(sym, arrays)
                records.append(SymbolRecord(
                    symbol=sym, blob_sha256=sha,
                    first_date=int(bars[0].date), last_date=int(bars[-1].date),
                    row_count=len(bars), quality="ok",
                ))
                total_rows += len(bars)
            print(f"  chunk {i // chunk_size + 1}: {len(chunk)} syms, "
                  f"{sum(1 for r in records if r.quality == 'ok')} ok, "
                  f"{(time.time() - t0):.1f}s", flush=True)

        strict = evaluate_strict_publish(
            records,
            expected_symbol_count=len(symbols),
            no_data_allowlist=no_data_reasons,
            max_allow_ratio=0.10,
        )
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source=SOURCE,
            adjustment=ADJUSTMENT,
            period=PERIOD,
            anchor_date=cutoff,
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=cutoff,
            provider_version="minute_vendor_csv_v1",
            sync_run_id=sync_run_id,
            status="building",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            universe_type="minute_60m_vendor_csv",
            dataset_type="bars",
            coverage_start_year=2000,
            coverage_end_year=2026,
            symbols=records,
        )
        for k, v in strict.items():
            if k == "target_status":
                manifest.status = v
            elif hasattr(manifest, k):
                setattr(manifest, k, v)

        print(json.dumps({
            "dataset_id": dataset_id,
            "status": manifest.status,
            "symbol_count": len(symbols),
            "ok": strict["imported_symbol_count"],
            "no_data": strict["no_data_symbol_count"],
            "total_rows": total_rows,
            "coverage_ratio": strict["coverage_ratio"],
        }, ensure_ascii=False, indent=2))

        if not args.dry_run:
            store.publish(manifest)
            print(f"PUBLISHED {dataset_id} -> {manifest.status}")
        else:
            print("DRY-RUN — nothing written.")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
