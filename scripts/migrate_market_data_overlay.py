#!/usr/bin/env python
"""Register an existing ready warehouse as the overlay_v1 baseline.

The overlay storage mode keeps the CURRENT latest healthy ready datasets as
immutable base blobs and writes all future EOD changes to the DuckDB delta.
This tool selects those bases and records them in ``delta/overlay_state.json``.

Selection policy (fail-closed):
  - the raw base is the latest READY complete tushare/none full-market stock
    dataset (reuses ``tushare_product.select_tushare_base``: partial / orphan
    windows / index-ETF-only sets are never candidates);
  - the delisted base is the latest ready delisted-missing complement
    (internal/delisted_complement) so the virtual L2 pool covers listed +
    delisted symbols; falls back to the newest ready tushare/none delisted
    pool when no complement has been derived yet;
  - the factor base is the latest ready tushare/adj_factor set and the
    supplement factor base is the ready factor set extending its coverage
    (delisted factors);
  - manifests are recorded with their sha256 so a later governance job can
    detect that the base moved (and refuse to silently overlay a different
    baseline).

Usage:
  python scripts/migrate_market_data_overlay.py --plan
  python scripts/migrate_market_data_overlay.py --apply [--storage-root ROOT]
  python scripts/migrate_market_data_overlay.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    KIND_BARS,
    KIND_FACTOR,
    OverlayState,
    load_overlay_state,
    save_overlay_state,
)
from wtpy.apps.astock.data.generation_catalog import (
    reconcile_generation_catalog,
)
from wtpy.apps.astock.data.tushare_product import (
    select_delisted_pool,
    select_supplement_factor,
    select_tushare_base,
    select_tushare_factor,
)


def _manifest_sha256(store: DatasetStore, dataset_id: str) -> str:
    m = store.load_manifest(dataset_id, deep_copy=False)
    if m is None:
        return ""
    return str(getattr(m, "manifest_sha256", "") or "")


def _latest_delisted_complement(store: DatasetStore) -> Optional[DatasetManifest]:
    """Latest ready internal/delisted_complement (disjoint from active base)."""
    best: Optional[DatasetManifest] = None
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=False)
        if m is None or m.status != "ready":
            continue
        if m.source != "internal" or m.adjustment != "delisted_complement":
            continue
        if best is None or int(m.data_cutoff_date or 0) > int(
            best.data_cutoff_date or 0
        ):
            best = m
    return best


def select_overlay_bases(store: DatasetStore) -> Dict:
    """Pick the bases for the overlay registry (never mutates anything)."""
    base = select_tushare_base(store)
    factor = select_tushare_factor(store)
    delisted_complement = _latest_delisted_complement(store)
    if delisted_complement is None:
        pool = select_delisted_pool(store)
        delisted = pool[0] if pool else None
    else:
        delisted = delisted_complement
    supplement_factor = None
    if factor is not None:
        supplement_factor = select_supplement_factor(store, factor)

    out: Dict = {
        "base": base.dataset_id if base else "",
        "base_cutoff": int(base.data_cutoff_date or 0) if base else 0,
        "base_symbol_count": int(base.symbol_count or 0) if base else 0,
        "base_row_count": int(base.row_count or 0) if base else 0,
        "base_manifest_sha256": (
            _manifest_sha256(store, base.dataset_id) if base else ""
        ),
        "delisted_base": delisted.dataset_id if delisted else "",
        "delisted_base_cutoff": (
            int(delisted.data_cutoff_date or 0) if delisted else 0
        ),
        "delisted_base_manifest_sha256": (
            _manifest_sha256(store, delisted.dataset_id) if delisted else ""
        ),
        "factor_base": factor.dataset_id if factor else "",
        "factor_base_cutoff": int(factor.data_cutoff_date or 0) if factor else 0,
        "factor_base_manifest_sha256": (
            _manifest_sha256(store, factor.dataset_id) if factor else ""
        ),
        "supplement_factor_base": (
            supplement_factor.dataset_id if supplement_factor else ""
        ),
        "supplement_factor_base_manifest_sha256": (
            _manifest_sha256(store, supplement_factor.dataset_id)
            if supplement_factor
            else ""
        ),
    }
    return out


def build_overlay_state(store: DatasetStore, plan: Dict) -> OverlayState:
    st = OverlayState(
        enabled=True,
        base_dataset_id=plan["base"],
        base_manifest_sha256=plan["base_manifest_sha256"],
        delisted_base_dataset_id=plan["delisted_base"],
        delisted_base_manifest_sha256=plan["delisted_base_manifest_sha256"],
        factor_base_dataset_id=plan["factor_base"],
        factor_base_manifest_sha256=plan["factor_base_manifest_sha256"],
        supplement_factor_base_dataset_id=plan["supplement_factor_base"],
        supplement_factor_base_manifest_sha256=(
            plan["supplement_factor_base_manifest_sha256"]
        ),
        delta_watermark=plan["base_cutoff"],
        factor_watermark=plan["factor_base_cutoff"],
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    return st


def cmd_plan(store: DatasetStore) -> int:
    plan = select_overlay_bases(store)
    problems = []
    if not plan["base"]:
        problems.append("no ready tushare/none full-market base dataset")
    if not plan["factor_base"]:
        problems.append("no ready tushare/adj_factor base dataset")
    print(json.dumps({"overlay_plan": plan, "problems": problems},
                     ensure_ascii=False, indent=2))
    return 1 if problems else 0


def cmd_apply(store: DatasetStore, *, force: bool = False) -> int:
    plan = select_overlay_bases(store)
    problems = []
    if not plan["base"]:
        problems.append("no ready tushare/none full-market base dataset")
    if not plan["factor_base"]:
        problems.append("no ready tushare/adj_factor base dataset")
    if problems:
        print(json.dumps({"error": "overlay cannot be applied",
                          "problems": problems}, ensure_ascii=False, indent=2))
        return 2

    existing = load_overlay_state(store.root)
    if existing.enabled and not force:
        same_base = existing.base_dataset_id == plan["base"]
        same_factor = existing.factor_base_dataset_id == plan["factor_base"]
        if same_base and same_factor:
            print("overlay already enabled with the same bases; use --force "
                  "to re-register (not recommended)")
            return 0
        print("WARNING: overlay already enabled with DIFFERENT bases:")
        print(f"  existing base: {existing.base_dataset_id}")
        print(f"  planned  base: {plan['base']}")
        print("Pass --force to overwrite the registry (base blobs are never "
              "modified by this tool).")
        return 2

    st = build_overlay_state(store, plan)
    delta = DeltaStore(store.root)
    delta.init_schema()
    save_overlay_state(store.root, st)
    reconcile_generation_catalog(store.root, st)
    print(json.dumps({"applied": True, "overlay_state": st.to_dict()},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_status(store: DatasetStore) -> int:
    st = load_overlay_state(store.root)
    delta = DeltaStore(store.root, st.delta_store_id if st.enabled else "main")
    health = None
    if st.enabled:
        bars_committed = delta.current_watermark(KIND_BARS)
        factors_committed = delta.current_watermark(KIND_FACTOR)
        health = delta.health_check(
            st.delta_watermark if bars_committed else 0,
            factor_watermark=(st.factor_watermark if factors_committed else None),
            commit_seq=st.delta_commit_seq or None,
            factor_commit_seq=st.factor_commit_seq or None,
        )
        health["view_delta_watermark"] = st.delta_watermark
        health["view_factor_watermark"] = st.factor_watermark
    print(json.dumps({"overlay": st.to_dict(), "delta_health": health},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Register the overlay_v1 baseline (migration tool)"
    )
    parser.add_argument("--storage-root", default=None,
                        help="market data root (default: MARKET_DATA_ROOT env "
                             "or storage/astock/market_data)")
    parser.add_argument("--plan", action="store_true",
                        help="show the selected baseline without writing")
    parser.add_argument("--apply", action="store_true",
                        help="register the baseline and enable overlay_v1")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing overlay registry")
    parser.add_argument("--status", action="store_true",
                        help="show current overlay state and delta health")
    args = parser.parse_args(argv)

    try:
        from wtpy.apps.astock.config import load_env_file
        load_env_file()
    except Exception:
        pass

    import os

    env_root = os.environ.get("MARKET_DATA_ROOT", "").strip()
    root = Path(args.storage_root or env_root or "storage/astock/market_data")
    store = DatasetStore(root)

    if args.status:
        return cmd_status(store)
    if args.apply:
        return cmd_apply(store, force=args.force)
    return cmd_plan(store)


if __name__ == "__main__":
    raise SystemExit(main())
