"""CLI for market-data blob garbage collection.

Usage:
  python scripts/gc_market_data.py --storage-root <root> [--dry-run|--apply]
                                    [--protection-hours 72]

Safety:
  - Defaults to --dry-run: prints the deletion plan, deletes nothing.
  - Requires the GC global exclusive lock; refuses to run while any live
    sync task holds a sync lock.
  - Blobs younger than --protection-hours (default 72) are never touched.
  - After --apply, re-verifies every retained manifest's blobs still exist.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wtpy.apps.astock.data.blob_gc import (
    DEFAULT_PROTECTION_HOURS,
    apply_gc_plan,
    build_gc_plan,
)
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.sync_lock import SyncTaskLock


def _resolve_root(arg: str) -> Path:
    root = Path(arg)
    if not (root / "manifests").is_dir():
        raise SystemExit(f"ERROR: not a market-data root (no manifests/): {root}")
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description="Blob garbage collection")
    ap.add_argument("--storage-root", required=True,
                    help="market data root (dir containing blobs/ and manifests/)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="actually delete (default is dry-run only)")
    ap.add_argument("--protection-hours", type=int, default=DEFAULT_PROTECTION_HOURS,
                    help=f"never delete blobs younger than this (default {DEFAULT_PROTECTION_HOURS})")
    ap.add_argument("--no-live-lock-check", action="store_true",
                    help="skip the live sync-lock guard (DANGEROUS; for emergencies)")
    args = ap.parse_args()

    root = _resolve_root(args.storage_root)
    store = DatasetStore(root)

    lock = SyncTaskLock(root, source="gc", adjustment="blob", period="all")
    try:
        lock.acquire()
    except Exception as e:
        print(f"ERROR: could not acquire GC lock: {e}")
        return 1
    try:
        plan = build_gc_plan(
            store,
            protection_hours=args.protection_hours,
            respect_live_locks=not args.no_live_lock_check,
        )
        print(json.dumps(plan.summarize(), ensure_ascii=False, indent=2))

        if plan.blocked_by_live_lock:
            print(f"\nREFUSED: live sync task holds a lock: {plan.blocked_by_live_lock}")
            print("Wait for the sync to finish, or use --no-live-lock-check.")
            return 2

        if not plan.candidates:
            print("\nNothing to collect.")
            return 0

        total = plan.reclaimable_bytes
        print(f"\n{len(plan.candidates)} blobs / {total/1024**3:.3f} GiB eligible "
              f"(protection={args.protection_hours}h).")
        if not args.apply:
            print("DRY-RUN — delete nothing. Re-run with --apply to execute.")
            for c in plan.candidates[:10]:
                print(f"  {c.sha256[:16]}  {c.size_bytes/1024:.1f} KiB  age={c.age_hours:.1f}h")
            if len(plan.candidates) > 10:
                print(f"  ... and {len(plan.candidates)-10} more")
            return 0

        result = apply_gc_plan(store, plan)
        print(f"\nDELETED {result['deleted']} blobs / {result['deleted_gib']:.3f} GiB")
        print(f"Post-delete manifest verification: "
              f"{'OK' if result['verified_manifests_ok'] else 'FAILED'}")
        if result["warnings"]:
            for w in result["warnings"]:
                print(f"  WARN: {w}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
