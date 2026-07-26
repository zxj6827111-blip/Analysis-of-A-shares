#!/usr/bin/env python
"""Market data synchronization program.

Usage:
  python scripts/sync_market_data.py --source tdxquant --mode full
  python scripts/sync_market_data.py --source tushare --mode full
  python scripts/sync_market_data.py --source tdx_local --mode full
  python scripts/sync_market_data.py --source all --mode full
  python scripts/sync_market_data.py --source tdxquant --mode incremental
  python scripts/sync_market_data.py --source tushare --mode incremental
  python scripts/sync_market_data.py --source tdxquant --mode audit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
    make_sync_run_id,
)
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketBar,
    MarketDataRequest,
    ProviderError,
)
from wtpy.apps.astock.data.repository import MarketDataRepository


def _normalize_symbol(symbol: str) -> str:
    """Normalize any symbol format to SSE.STK.600000 / SZSE.STK.000001 / BSE.STK.430047.

    Supported input formats:
      SSE.STK.600000 / SZSE.STK.000001 / BSE.STK.430047  (canonical, pass-through)
      600000.SH / 000001.SZ / 430047.BJ
      sh600000 / sz000001 / bj430047
      600000 / 000001 / 430047  (bare 6-digit, exchange inferred)
    """
    parts = symbol.split(".")
    if len(parts) == 3:
        return symbol
    if len(parts) == 2:
        code, suffix = parts
        suffix_upper = suffix.upper()
        exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix_upper)
        if exch:
            return f"{exch}.STK.{code}"
    lower = symbol.lower()
    if len(lower) == 8 and lower[:2] in ("sh", "sz", "bj") and lower[2:].isdigit():
        prefix_map = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}
        return f"{prefix_map[lower[:2]]}.STK.{lower[2:]}"
    if symbol.isdigit() and len(symbol) == 6:
        if symbol.startswith(("5", "6", "9")):
            return f"SSE.STK.{symbol}"
        if symbol.startswith(("4", "8")):
            return f"BSE.STK.{symbol}"
        return f"SZSE.STK.{symbol}"
    return symbol


def get_storage_root() -> Path:
    """Resolve market data root: env MARKET_DATA_ROOT > default internal path."""
    import os
    env_val = os.environ.get("MARKET_DATA_ROOT", "").strip()
    if env_val:
        return Path(env_val)
    return Path("storage/astock/market_data")


def sync_tdxquant_full(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider

    provider = TdxQuantProvider(
        tdx_root=args.tdx_root, batch_size=args.batch_size
    )
    if not provider.health_check():
        print("ERROR: TdxQuant client is not available (not running or not logged in)")
        return {"status": "failed", "error": "client_unavailable"}

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        print("ERROR: No symbols to sync")
        return {"status": "failed", "error": "no_symbols"}

    print(f"Syncing {len(symbols)} symbols from TdxQuant (full mode)...")
    sync_run_id = make_sync_run_id("tdxquant")
    results = {}

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY),
        (AdjustmentMode.FRONT, BarPeriod.DAY),
        (AdjustmentMode.FRONT, BarPeriod.WEEK),
    ]

    for adj, period in configs:
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TDXQUANT.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=args.start_date,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
        )
        results[f"{adj.value}_{period.value}"] = ds_result
        print(
            f"  {adj.value}/{period.value}: "
            f"{ds_result['success']}/{ds_result['total']} ok, "
            f"dataset={ds_result.get('dataset_id', 'N/A')}"
        )

    return {"status": "success", "sync_run_id": sync_run_id, "datasets": results}


def sync_tdxquant_incremental(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider

    provider = TdxQuantProvider(
        tdx_root=args.tdx_root, batch_size=args.batch_size
    )
    if not provider.health_check():
        print("ERROR: TdxQuant client is not available")
        return {"status": "failed", "error": "client_unavailable"}

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        return {"status": "failed", "error": "no_symbols"}

    print(f"Incremental sync for {len(symbols)} symbols from TdxQuant...")
    sync_run_id = make_sync_run_id("tdxquant")

    repo = MarketDataRepository(store)
    rebuild_symbols = set()

    try:
        latest_front = repo.resolve_latest_ready(
            source=DataSource.TDXQUANT.value,
            adjustment=AdjustmentMode.FRONT.value,
            period=BarPeriod.DAY.value,
        )
        overlap_start = _recent_trading_days_ago(60)
        for sym in symbols:
            try:
                local_bars = repo.load_bars(
                    dataset_id=latest_front.dataset_id,
                    symbol=sym,
                    start_date=overlap_start,
                )
                remote_req = MarketDataRequest(
                    symbols=[sym],
                    period=BarPeriod.DAY,
                    adjustment=AdjustmentMode.FRONT,
                    start_date=overlap_start,
                )
                remote_bars = provider.fetch_bars(remote_req)
                if _history_changed(local_bars, remote_bars):
                    rebuild_symbols.add(sym)
            except Exception:
                rebuild_symbols.add(sym)
    except Exception:
        rebuild_symbols = set(symbols)

    print(f"  {len(rebuild_symbols)} symbols need full rebuild (CA detected)")
    results = {}

    incremental_start = _recent_trading_days_ago(60)
    parent_ds_id = None
    try:
        parent_ds_id = latest_front.dataset_id
    except NameError:
        pass

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY, incremental_start),
        (AdjustmentMode.FRONT, BarPeriod.DAY, None),
        (AdjustmentMode.FRONT, BarPeriod.WEEK, None),
    ]
    for adj, period, sym_start in configs:
        if adj == AdjustmentMode.NONE:
            sync_symbols = symbols
            use_start = sym_start
        else:
            sync_symbols = list(rebuild_symbols) if rebuild_symbols else symbols
            use_start = args.start_date

        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=sync_symbols,
            source=DataSource.TDXQUANT.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=use_start,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
            parent_dataset_id=parent_ds_id if adj == AdjustmentMode.FRONT else None,
        )
        results[f"{adj.value}_{period.value}"] = ds_result

    return {"status": "success", "sync_run_id": sync_run_id, "datasets": results}


def sync_tushare_full(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    provider = TushareProvider(token=args.token)
    if not provider.health_check():
        print("ERROR: Tushare API not available (check token/network)")
        return {"status": "failed", "error": "api_unavailable"}

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        universe = provider.fetch_universe(
            include_delisted=args.include_delisted,
            include_bse=args.include_bse,
        )
        symbols = [e.symbol for e in universe]

    if not symbols:
        print("ERROR: No symbols to sync")
        return {"status": "failed", "error": "no_symbols"}

    print(f"Syncing {len(symbols)} symbols from Tushare (full mode)...")
    sync_run_id = make_sync_run_id("tushare")
    results = {}

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY),
        (AdjustmentMode.QFQ, BarPeriod.DAY),
    ]
    for adj, period in configs:
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TUSHARE.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=args.start_date,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
        )
        results[f"{adj.value}_{period.value}"] = ds_result
        print(
            f"  {adj.value}/{period.value}: "
            f"{ds_result['success']}/{ds_result['total']} ok, "
            f"dataset={ds_result.get('dataset_id', 'N/A')}"
        )

    return {"status": "success", "sync_run_id": sync_run_id, "datasets": results}


def sync_tushare_incremental(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    provider = TushareProvider(token=args.token)
    if not provider.health_check():
        print("ERROR: Tushare API not available")
        return {"status": "failed", "error": "api_unavailable"}

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        universe = provider.fetch_universe(include_delisted=args.include_delisted)
        symbols = [e.symbol for e in universe]

    print(f"Incremental sync for {len(symbols)} symbols from Tushare...")
    sync_run_id = make_sync_run_id("tushare")

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY),
        (AdjustmentMode.QFQ, BarPeriod.DAY),
    ]
    results = {}
    for adj, period in configs:
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TUSHARE.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=args.start_date,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
        )
        results[f"{adj.value}_{period.value}"] = ds_result

    return {"status": "success", "sync_run_id": sync_run_id, "datasets": results}


def sync_tdx_local_full(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tdx_local import TdxLocalProvider

    provider = TdxLocalProvider(tdx_root=args.tdx_root)
    if not provider.health_check():
        print(f"ERROR: TDX root not found: {args.tdx_root}")
        return {"status": "failed", "error": "tdx_root_missing"}

    symbols = _resolve_symbols(args, provider)
    if not symbols:
        universe = provider.fetch_universe(include_bse=args.include_bse)
        symbols = [e.symbol for e in universe]

    print(f"Syncing {len(symbols)} symbols from TDX local (full mode)...")
    sync_run_id = make_sync_run_id("tdxlocal")

    ds_result = _sync_dataset(
        provider=provider,
        store=store,
        symbols=symbols,
        source=DataSource.TDX_LOCAL.value,
        adjustment=AdjustmentMode.NONE,
        period=BarPeriod.DAY,
        sync_run_id=sync_run_id,
        start_date=args.start_date,
        end_date=args.end_date,
        anchor_date=args.anchor_date,
    )
    print(
        f"  none/1d: {ds_result['success']}/{ds_result['total']} ok, "
        f"dataset={ds_result.get('dataset_id', 'N/A')}"
    )
    return {"status": "success", "sync_run_id": sync_run_id, "datasets": {"none_1d": ds_result}}


KNOWN_MISSING_DELISTED_EVIDENCE = [
    # Independent acceptance 2026-07-26: well-known delisted A-shares verified
    # ABSENT from every vendor year ZIP. Evidence sample, NOT exhaustive —
    # the true missing-delisted count is unknown and larger.
    "SZSE.STK.300104",  # 乐视网
    "SZSE.STK.002680",  # 长生生物
    "SSE.STK.601558",   # 华锐风电
    "SZSE.STK.002450",  # 康得新
    "SSE.STK.600001",   # 邯郸钢铁
    "SSE.STK.600002",   # 齐鲁石化
]

SURVIVORSHIP_WARNING_TEXT = (
    "该数据集缺少部分历史退市股票，长期全市场回测存在幸存者偏差。"
)

UNIVERSE_DEFINITION_VERSION = "v1"


def _resolve_incoming_root(args) -> Optional[str]:
    """Incoming ZIP root: --incoming-root > LOCAL_VENDOR_RAW_ROOT env. No hardcoded paths."""
    import os
    val = getattr(args, "incoming_root", None) or os.environ.get("LOCAL_VENDOR_RAW_ROOT", "").strip()
    return val or None


def _load_universe_file(path: Path) -> List[str]:
    """Read included canonical symbols from a universe CSV (vendor_universe format)."""
    import csv as _csv
    symbols: List[str] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            status = (row.get("inclusion_status") or "included").strip().lower()
            sym = (row.get("canonical_symbol") or row.get("symbol") or "").strip()
            if not sym:
                continue
            if status == "included":
                symbols.append(sym)
    return sorted(set(symbols))


def _load_allowlist_file(path: Optional[str]) -> Dict[str, str]:
    """no_data allowlist CSV: columns symbol,reason. Explicit only — no wildcards."""
    if not path:
        return {}
    import csv as _csv
    allow: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            sym = (row.get("symbol") or row.get("canonical_symbol") or "").strip()
            if sym:
                allow[sym] = (row.get("reason") or "").strip() or "allowlisted"
    return allow


def _checkpoint_path(store: DatasetStore) -> Path:
    return store.sync_logs_dir / "checkpoint_local_vendor_none_1d.json"


def _load_checkpoint(store: DatasetStore) -> Optional[dict]:
    p = _checkpoint_path(store)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_checkpoint(store: DatasetStore, ck: dict) -> None:
    from wtpy.apps.astock.data.io_util import atomic_write_json
    ck["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_write_json(_checkpoint_path(store), ck)


def sync_local_vendor_full(args, store: DatasetStore) -> dict:
    """Sync daily K-line data from purchased vendor ZIP archives.

    Gate A design:
      - cross-platform SyncTaskLock (fcntl only imported on POSIX);
      - universe = --symbol > --universe-file > dynamic historical union
        (vendor_universe: A-share + BSE stocks only, non-A excluded);
      - chunked ZIP-first fetch with a resumable checkpoint
        (interrupt -> rerun with --resume skips completed chunks);
      - strict publish policy: ready requires failed==0 and every no_data
        symbol explicitly allowlisted; otherwise partial;
      - survivorship-bias metadata recorded on the manifest.
    """
    from wtpy.apps.astock.data.providers.local_vendor import LocalVendorProvider
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError
    from wtpy.apps.astock.data.dataset_store import evaluate_strict_publish

    if (args.adjustment or "none") != "none" or (args.period or "1d") != "1d":
        print("ERROR: local_vendor only supports adjustment=none period=1d")
        return {"status": "failed", "error": "unsupported_adjustment_or_period"}

    incoming = _resolve_incoming_root(args)
    if not incoming:
        print("ERROR: incoming root not set. Pass --incoming-root or set "
              "LOCAL_VENDOR_RAW_ROOT in .env (no hardcoded default).")
        return {"status": "failed", "error": "incoming_root_not_configured"}

    sync_run_id = make_sync_run_id("localvendor")
    lock = SyncTaskLock(
        store.root, source="local_vendor", adjustment="none", period="1d",
        sync_run_id=sync_run_id,
    )
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        print("Another sync task for (local_vendor, none, 1d) on this data root "
              "is running. Concurrent tasks with the same scope are forbidden.")
        return {"status": "failed", "error": "concurrent_lock", "holder": e.holder}

    if lock.recovered_stale:
        print(f"NOTE: recovered stale lock from previous holder: "
              f"pid={lock.recovered_stale.get('pid')} "
              f"alive={lock.recovered_stale.get('holder_alive')} "
              f"start={lock.recovered_stale.get('start_time')}")

    try:
        provider = LocalVendorProvider(incoming)
        if not provider.health_check():
            print(f"ERROR: Local vendor incoming root not found or no year ZIPs: {incoming}")
            return {"status": "failed", "error": "incoming_root_missing"}

        years = provider.available_years()

        # ---- universe resolution (dynamic; no hardcoded counts) ----
        universe_source = "explicit_symbols"
        excluded_count = 0
        symbols = _resolve_symbols(args, provider)
        if not symbols and getattr(args, "universe_file", None):
            symbols = _load_universe_file(Path(args.universe_file))
            universe_source = f"universe_file:{args.universe_file}"
        if not symbols:
            from wtpy.apps.astock.data.vendor_universe import build_vendor_universe
            print("Building dynamic vendor universe (historical union, A-share + BSE stocks "
                  "only, equity metadata confirmed)...")
            uni = build_vendor_universe(provider, with_metadata=True)
            symbols = list(uni.included_symbols)
            excluded_count = int(uni.summary.get("excluded_count", 0))
            universe_source = "dynamic_historical_union_v1_metadata"
        symbols = sorted(set(symbols))
        if not symbols:
            print("ERROR: No symbols to sync")
            return {"status": "failed", "error": "no_symbols"}

        import hashlib as _hl
        universe_hash = _hl.sha256(",".join(symbols).encode()).hexdigest()
        chunk_size = max(1, int(getattr(args, "chunk_size", None) or 500))
        n_chunks = (len(symbols) + chunk_size - 1) // chunk_size

        print(f"Syncing {len(symbols)} symbols from local vendor (full mode)...")
        print(f"  Incoming root : {incoming}")
        print(f"  Universe      : {universe_source} (hash={universe_hash[:12]})")
        print(f"  Years         : {years[0]}-{years[-1]} ({len(years)} ZIPs, one per year, "
              f"exact-duplicate copies skipped)")
        print(f"  Chunks        : {n_chunks} x {chunk_size} (ZIP-first per chunk)")

        # ---- checkpoint / resume ----
        ck = _load_checkpoint(store)
        ck_matches = bool(
            ck
            and ck.get("universe_hash") == universe_hash
            and ck.get("chunk_size") == chunk_size
            and ck.get("start_date") == args.start_date
            and ck.get("end_date") == args.end_date
        )
        if ck and not getattr(args, "resume", False) and not getattr(args, "fresh", False):
            print("ERROR: An unfinished checkpoint exists for local_vendor/none/1d.")
            print(f"  checkpoint: {_checkpoint_path(store)}")
            print(f"  created_at={ck.get('created_at')} completed_chunks="
                  f"{len(ck.get('completed_chunks', {}))}/{ck.get('chunks_total')}")
            print("  Use --resume to continue it, or --fresh to discard and restart.")
            return {"status": "failed", "error": "checkpoint_exists_use_resume_or_fresh"}
        if getattr(args, "resume", False):
            if not ck:
                print("ERROR: --resume requested but no checkpoint found.")
                return {"status": "failed", "error": "no_checkpoint_to_resume"}
            if not ck_matches:
                print("ERROR: checkpoint does not match current universe/date/chunk "
                      "parameters; refuse to mix tasks. Use --fresh to restart.")
                return {"status": "failed", "error": "checkpoint_mismatch"}
            sync_run_id = ck["sync_run_id"]
            lock.sync_run_id = sync_run_id
            print(f"  Resuming sync_run_id={sync_run_id}: "
                  f"{len(ck.get('completed_chunks', {}))}/{n_chunks} chunks already done")
        else:
            if ck and getattr(args, "fresh", False):
                print("  Discarding previous checkpoint (--fresh).")
            ck = {
                "version": 1,
                "source": "local_vendor", "adjustment": "none", "period": "1d",
                "universe_hash": universe_hash,
                "universe_source": universe_source,
                "start_date": args.start_date, "end_date": args.end_date,
                "chunk_size": chunk_size, "chunks_total": n_chunks,
                "sync_run_id": sync_run_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "completed_chunks": {},
            }
            _save_checkpoint(store, ck)

        # ---- chunked ZIP-first fetch/store ----
        t0 = time.time()
        completed: Dict[str, list] = ck.get("completed_chunks", {})
        skipped_chunks = 0
        for ci in range(n_chunks):
            key = str(ci)
            if key in completed:
                skipped_chunks += 1
                print(f"  chunk {ci + 1}/{n_chunks}: skipped (checkpoint)")
                continue
            chunk_syms = symbols[ci * chunk_size : (ci + 1) * chunk_size]
            tc = time.time()
            batch = provider.fetch_bars_zipfirst(
                chunk_syms, start_date=args.start_date, end_date=args.end_date
            )
            recs = []
            chunk_rows = 0
            for sym in chunk_syms:
                bars = batch.get(sym, [])
                if not bars:
                    recs.append({"symbol": sym, "blob_sha256": "", "first_date": None,
                                 "last_date": None, "row_count": 0,
                                 "quality": "no_data", "error": "empty"})
                    continue
                sha = store.store_bars(sym, bars)
                chunk_rows += len(bars)
                recs.append({"symbol": sym, "blob_sha256": sha,
                             "first_date": bars[0].trade_date,
                             "last_date": bars[-1].trade_date,
                             "row_count": len(bars), "quality": "ok", "error": ""})
            completed[key] = recs
            ck["completed_chunks"] = completed
            _save_checkpoint(store, ck)
            print(f"  chunk {ci + 1}/{n_chunks}: {sum(1 for r in recs if r['quality'] == 'ok')}"
                  f"/{len(chunk_syms)} ok, rows={chunk_rows}, {time.time() - tc:.1f}s")

        symbol_records: List[SymbolRecord] = []
        for ci in range(n_chunks):
            for r in completed.get(str(ci), []):
                symbol_records.append(SymbolRecord(**r))
        total_rows = sum(r.row_count for r in symbol_records)
        success = sum(1 for r in symbol_records if r.quality == "ok")
        no_data_syms = [r.symbol for r in symbol_records if r.quality == "no_data"]
        errors = [{"symbol": r.symbol, "error": r.error}
                  for r in symbol_records if r.quality in ("error", "no_data")]
        elapsed = time.time() - t0

        # ---- strict publish policy ----
        allowlist = _load_allowlist_file(getattr(args, "allow_no_data_file", None))
        policy = evaluate_strict_publish(
            symbol_records,
            expected_symbol_count=len(symbols),
            excluded_symbol_count=excluded_count,
            no_data_allowlist=allowlist,
            max_allow_count=int(getattr(args, "max_no_data_count", None) or 0),
            max_allow_ratio=float(getattr(args, "max_no_data_ratio", None) or 0.0),
        )

        cutoff_str = str(args.end_date or time.strftime("%Y%m%d"))
        canonical_pre = json.dumps(
            {"source": "local_vendor", "adjustment": "none", "period": "1d",
             "sync_run_id": sync_run_id, "symbols": symbols},
            sort_keys=True,
        )
        pre_sha = _hl.sha256(canonical_pre.encode()).hexdigest()
        dataset_id = make_dataset_id("localvendor", "none", "1d", cutoff_str, pre_sha)

        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source=DataSource.LOCAL_VENDOR.value,
            adjustment=AdjustmentMode.NONE.value,
            period=BarPeriod.DAY.value,
            anchor_date=args.anchor_date,
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=args.end_date or int(time.strftime("%Y%m%d")),
            provider_version=provider.provider_version(),
            sync_run_id=sync_run_id,
            status="building",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            # ---- survivorship / universe metadata (Gate A policy) ----
            universe_type="vendor_available_historical_union",
            universe_definition_version=UNIVERSE_DEFINITION_VERSION,
            survivorship_bias=True,
            historical_universe_complete=False,
            delisted_coverage_complete=False,
            coverage_start_year=years[0] if years else None,
            coverage_end_year=years[-1] if years else None,
            known_missing_delisted_count=len(KNOWN_MISSING_DELISTED_EVIDENCE),
            known_missing_delisted_symbols=list(KNOWN_MISSING_DELISTED_EVIDENCE),
            warning_text=SURVIVORSHIP_WARNING_TEXT,
            recommended_use=[
                "import pipeline validation", "indicator research on surviving symbols",
                "current-listing research", "engineering baseline",
                "comparison against future delisted-complete datasets",
            ],
            prohibited_or_discouraged_use=[
                "claiming complete 27-year whole-market returns",
                "claiming absence of survivorship bias",
                "claiming a historically true tradable universe",
            ],
        )
        manifest.symbols = symbol_records
        manifest.symbol_count = len(symbol_records)
        manifest.row_count = total_rows
        manifest.expected_symbol_count = policy["expected_symbol_count"]
        manifest.imported_symbol_count = policy["imported_symbol_count"]
        manifest.excluded_symbol_count = policy["excluded_symbol_count"]
        manifest.no_data_symbol_count = policy["no_data_symbol_count"]
        manifest.failed_symbol_count = policy["failed_symbol_count"]
        manifest.warning_symbol_count = policy["warning_symbol_count"]
        manifest.coverage_ratio = policy["coverage_ratio"]
        manifest.no_data_allowlist = policy["no_data_allowlist"]
        if policy["target_status"] != "ready":
            manifest.status = "partial"
            print(f"  STRICT POLICY -> partial: {'; '.join(policy['block_reasons'])}")
            if no_data_syms[:10]:
                print(f"  no_data sample: {no_data_syms[:10]}")
        store.publish(manifest)

        # checkpoint is consumed once the dataset reaches a terminal publish
        try:
            _checkpoint_path(store).unlink(missing_ok=True)
        except Exception:
            pass

        log_payload = {
            "sync_run_id": sync_run_id,
            "dataset_id": dataset_id,
            "source": "local_vendor",
            "universe_source": universe_source,
            "universe_hash": universe_hash,
            "chunks_total": n_chunks,
            "chunks_skipped_on_resume": skipped_chunks,
            "result": {
                "success": success, "no_data": len(no_data_syms),
                "total_rows": total_rows, "status": manifest.status,
                "coverage_ratio": policy["coverage_ratio"],
                "block_reasons": policy["block_reasons"],
            },
        }
        store.save_sync_log(sync_run_id, log_payload)
        for extra in (getattr(args, "log_path", None), getattr(args, "report_path", None)):
            if extra:
                try:
                    Path(extra).parent.mkdir(parents=True, exist_ok=True)
                    Path(extra).write_text(
                        json.dumps(log_payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                except Exception as e:
                    print(f"  WARNING: could not write log copy {extra}: {e}")

        print(f"  none/1d: {success}/{len(symbols)} ok, dataset={dataset_id}")
        print(f"  Status: {manifest.status} | rows={total_rows} | elapsed {elapsed:.1f}s"
              f" | chunks skipped on resume: {skipped_chunks}")
        return {
            "status": "success", "sync_run_id": sync_run_id,
            "dataset_id": dataset_id,
            "dataset_status": manifest.status,
            "datasets": {"none_1d": {
                "dataset_id": dataset_id, "total": len(symbols),
                "success": success, "failed": policy["failed_symbol_count"],
                "no_data": policy["no_data_symbol_count"],
                "total_rows": total_rows, "elapsed_sec": round(elapsed, 1),
                "status": manifest.status, "errors": errors[:50],
                "chunks_skipped_on_resume": skipped_chunks,
                "coverage_ratio": policy["coverage_ratio"],
            }},
        }
    finally:
        lock.release()


def audit_dataset(args, store: DatasetStore) -> dict:
    repo = MarketDataRepository(store)
    if args.dataset_id:
        result = repo.validate_dataset(args.dataset_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    datasets = repo.list_datasets()
    print(f"Found {len(datasets)} datasets:")
    for m in datasets:
        print(
            f"  {m.dataset_id} | {m.source}/{m.adjustment}/{m.period} | "
            f"status={m.status} | symbols={m.symbol_count} | rows={m.row_count}"
        )
    return {"status": "success", "count": len(datasets)}


def _sync_dataset(
    *,
    provider,
    store: DatasetStore,
    symbols: List[str],
    source: str,
    adjustment: AdjustmentMode,
    period: BarPeriod,
    sync_run_id: str,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    anchor_date: Optional[int] = None,
    parent_dataset_id: Optional[str] = None,
) -> dict:
    t0 = time.time()
    cutoff_str = str(end_date or time.strftime("%Y%m%d"))
    anchor_str = f"anchor{anchor_date}" if anchor_date else cutoff_str

    canonical_pre = json.dumps(
        {"source": source, "adjustment": adjustment.value, "period": period.value,
         "sync_run_id": sync_run_id, "symbols": sorted(symbols)},
        sort_keys=True,
    )
    import hashlib
    pre_sha = hashlib.sha256(canonical_pre.encode()).hexdigest()
    dataset_id = make_dataset_id(
        source.replace("_", ""), adjustment.value, period.value, anchor_str, pre_sha
    )

    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment.value,
        period=period.value,
        anchor_date=anchor_date,
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=end_date or int(time.strftime("%Y%m%d")),
        provider_version=provider.provider_version(),
        sync_run_id=sync_run_id,
        parent_dataset_id=parent_dataset_id,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    success = 0
    failed = 0
    no_data = 0
    total_rows = 0
    errors: List[dict] = []
    symbol_records: List[SymbolRecord] = []

    batch_size = getattr(provider, "_batch_size", 1)
    caps = provider.capabilities()
    if caps.supports_batch and batch_size > 1:
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                req = MarketDataRequest(
                    symbols=batch,
                    period=period,
                    adjustment=adjustment,
                    start_date=start_date,
                    end_date=end_date,
                    anchor_date=anchor_date,
                )
                bars = provider.fetch_bars(req)
                by_symbol: Dict[str, List[MarketBar]] = defaultdict(list)
                for b in bars:
                    by_symbol[_normalize_symbol(b.symbol)].append(b)
                for sym in batch:
                    norm_sym = _normalize_symbol(sym)
                    sym_bars = by_symbol.get(norm_sym, [])
                    if not sym_bars:
                        no_data += 1
                        symbol_records.append(
                            SymbolRecord(symbol=norm_sym, blob_sha256="", quality="no_data", error="empty")
                        )
                        continue
                    blob_sha = store.store_bars(norm_sym, sym_bars)
                    total_rows += len(sym_bars)
                    success += 1
                    symbol_records.append(
                        SymbolRecord(
                            symbol=norm_sym,
                            blob_sha256=blob_sha,
                            first_date=sym_bars[0].trade_date,
                            last_date=sym_bars[-1].trade_date,
                            row_count=len(sym_bars),
                            quality="ok",
                        )
                    )
            except ProviderError as e:
                for sym in batch:
                    norm_sym = _normalize_symbol(sym)
                    try:
                        single_req = MarketDataRequest(
                            symbols=[sym],
                            period=period,
                            adjustment=adjustment,
                            start_date=start_date,
                            end_date=end_date,
                            anchor_date=anchor_date,
                        )
                        single_bars = provider.fetch_bars(single_req)
                        if not single_bars:
                            no_data += 1
                            symbol_records.append(
                                SymbolRecord(symbol=norm_sym, blob_sha256="", quality="no_data", error="empty")
                            )
                            continue
                        blob_sha = store.store_bars(norm_sym, single_bars)
                        total_rows += len(single_bars)
                        success += 1
                        symbol_records.append(
                            SymbolRecord(
                                symbol=norm_sym,
                                blob_sha256=blob_sha,
                                first_date=single_bars[0].trade_date,
                                last_date=single_bars[-1].trade_date,
                                row_count=len(single_bars),
                                quality="ok",
                            )
                        )
                    except ProviderError as e2:
                        failed += 1
                        errors.append({"symbol": norm_sym, "error": str(e2)})
                        symbol_records.append(
                            SymbolRecord(symbol=norm_sym, blob_sha256="", quality="error", error=str(e2))
                        )
    else:
        for sym in symbols:
            norm_sym = _normalize_symbol(sym)
            try:
                req = MarketDataRequest(
                    symbols=[sym],
                    period=period,
                    adjustment=adjustment,
                    start_date=start_date,
                    end_date=end_date,
                    anchor_date=anchor_date,
                )
                bars = provider.fetch_bars(req)
                if not bars:
                    no_data += 1
                    symbol_records.append(
                        SymbolRecord(symbol=norm_sym, blob_sha256="", quality="no_data", error="empty")
                    )
                    continue
                blob_sha = store.store_bars(norm_sym, bars)
                total_rows += len(bars)
                success += 1
                symbol_records.append(
                    SymbolRecord(
                        symbol=norm_sym,
                        blob_sha256=blob_sha,
                        first_date=bars[0].trade_date,
                        last_date=bars[-1].trade_date,
                        row_count=len(bars),
                        quality="ok",
                    )
                )
            except ProviderError as e:
                failed += 1
                errors.append({"symbol": norm_sym, "error": str(e)})
                symbol_records.append(
                    SymbolRecord(symbol=norm_sym, blob_sha256="", quality="error", error=str(e))
                )

    manifest.symbols = symbol_records
    manifest.symbol_count = len(symbol_records)
    manifest.row_count = total_rows

    try:
        store.publish(manifest)
    except ValueError as e:
        print(f"  WARNING: publish integrity check failed: {e}")

    elapsed = time.time() - t0
    result = {
        "dataset_id": dataset_id,
        "total": len(symbols),
        "success": success,
        "failed": failed,
        "no_data": no_data,
        "total_rows": total_rows,
        "elapsed_sec": round(elapsed, 2),
        "status": manifest.status,
        "errors": errors[:50],
    }

    store.save_sync_log(sync_run_id, {
        "sync_run_id": sync_run_id,
        "dataset_id": dataset_id,
        "source": source,
        "adjustment": adjustment.value,
        "period": period.value,
        "result": result,
    })

    return result


def _resolve_symbols(args, provider) -> List[str]:
    if args.symbol:
        return [s.strip() for s in args.symbol.split(",") if s.strip()]
    return []


def _recent_trading_days_ago(n: int) -> Optional[int]:
    import datetime
    d = datetime.date.today() - datetime.timedelta(days=int(n * 1.5))
    return int(d.strftime("%Y%m%d"))


def _history_changed(
    local_bars: List[MarketBar], remote_bars: List[MarketBar]
) -> bool:
    if not local_bars or not remote_bars:
        return True
    local_map = {b.trade_date: b.close for b in local_bars}
    remote_map = {b.trade_date: b.close for b in remote_bars}
    overlap_dates = set(local_map.keys()) & set(remote_map.keys())
    if not overlap_dates:
        return True
    for d in overlap_dates:
        if abs(local_map[d] - remote_map[d]) > 1e-6:
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Sync market data to local datasets")
    parser.add_argument("--source", required=True,
                        choices=["tdxquant", "tushare", "tdx_local", "local_vendor", "all"])
    parser.add_argument("--mode", default="full",
                        choices=["full", "incremental", "rebuild", "audit"])
    parser.add_argument("--symbol", default=None, help="Comma-separated symbols")
    parser.add_argument("--period", default=None, help="1d, 1w, 1mon")
    parser.add_argument("--adjustment", default=None, help="none, front, qfq, asof_qfq")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--anchor-date", type=int, default=None)
    parser.add_argument("--start-date", type=int, default=None)
    parser.add_argument("--end-date", type=int, default=None)
    parser.add_argument("--include-bse", action="store_true")
    parser.add_argument("--include-delisted", action="store_true")
    parser.add_argument("--tdx-root", default=r"D:\通达信")
    parser.add_argument("--incoming-root", default=None,
                        help="Path to local vendor incoming ZIPs (for --source local_vendor)")
    parser.add_argument("--token", default=None, help="Tushare token (prefer ts.get_token())")
    parser.add_argument("--storage-root", default=None)
    parser.add_argument("--dataset-id", default=None, help="For audit mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be synced without writing data")
    parser.add_argument("--preflight", action="store_true",
                        help="Validate environment and data availability before sync")
    parser.add_argument("--universe-file", default=None,
                        help="Universe CSV (vendor_universe format); rows with "
                             "inclusion_status=included are synced")
    parser.add_argument("--chunk-size", type=int, default=500,
                        help="Symbols per ZIP-first chunk (checkpoint granularity)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume an interrupted local_vendor sync from its checkpoint")
    parser.add_argument("--fresh", action="store_true",
                        help="Discard an existing checkpoint and restart")
    parser.add_argument("--allow-no-data-file", default=None,
                        help="CSV (symbol,reason): explicit no_data allowlist for ready publish")
    parser.add_argument("--max-no-data-count", type=int, default=0,
                        help="Max allowlisted no_data symbols still publishing ready")
    parser.add_argument("--max-no-data-ratio", type=float, default=0.0,
                        help="Max allowlisted no_data ratio still publishing ready")
    parser.add_argument("--log-path", default=None, help="Extra JSON log copy path")
    parser.add_argument("--report-path", default=None, help="Extra JSON report copy path")

    args = parser.parse_args()

    # machine-local settings (.env at project root); real env vars win
    try:
        from wtpy.apps.astock.config import load_env_file
        load_env_file()
    except Exception:
        pass

    storage_root = Path(args.storage_root) if args.storage_root else get_storage_root()

    import os as _os
    _env_md = _os.environ.get("MARKET_DATA_ROOT", "").strip()
    if not _env_md and not args.storage_root:
        print("WARNING: MARKET_DATA_ROOT env not set and --storage-root not provided.")
        print(f"  Using default internal path: {storage_root}")
        print("  For production, set MARKET_DATA_ROOT in .env to your formal data root.")
        print()

    if args.preflight:
        print("PREFLIGHT CHECK")
        print(f"  ASTOCK_ENV: {_os.environ.get('ASTOCK_ENV', '(not set)')}")
        print(f"  Storage root: {storage_root}")
        print(f"  Exists: {storage_root.exists()}")
        print(f"  MARKET_DATA_ROOT env: {_env_md or '(not set)'}")
        if args.source in ("local_vendor", "all"):
            incoming = _resolve_incoming_root(args)
            print(f"  Incoming root: {incoming or '(NOT CONFIGURED — set LOCAL_VENDOR_RAW_ROOT)'}")
            if incoming:
                print(f"  Incoming exists: {Path(incoming).exists()}")
            lock_file = storage_root / ".locks" / "sync_local_vendor_none_1d.lock"
            if lock_file.exists():
                from wtpy.apps.astock.data.sync_lock import SyncTaskLock
                holder = SyncTaskLock.probe(lock_file)
                print(f"  Lock file: {lock_file}")
                print(f"  Lock holder metadata: {holder}")
            else:
                print("  Lock file: (none)")
            ck_file = storage_root / "sync_logs" / "checkpoint_local_vendor_none_1d.json"
            if ck_file.exists():
                try:
                    _ck = json.loads(ck_file.read_text(encoding="utf-8"))
                    print(f"  Checkpoint: {len(_ck.get('completed_chunks', {}))}"
                          f"/{_ck.get('chunks_total')} chunks done "
                          f"(sync_run_id={_ck.get('sync_run_id')}) — use --resume")
                except Exception:
                    print(f"  Checkpoint: unreadable ({ck_file})")
            else:
                print("  Checkpoint: (none)")
        import shutil
        probe_dir = storage_root if storage_root.exists() else storage_root.parent
        free = shutil.disk_usage(str(probe_dir)).free
        print(f"  Free disk space: {free/1024**3:.1f} GB")
        if free < 5 * 1024**3:
            print("  WARNING: Less than 5GB free space!")
        return

    if args.dry_run:
        print(f"DRY RUN: source={args.source}, mode={args.mode}")
        print(f"  Storage root: {storage_root}")
        if args.symbol:
            syms = [s.strip() for s in args.symbol.split(",") if s.strip()]
            print(f"  Symbols: {len(syms)} specified")
        elif args.universe_file:
            syms = _load_universe_file(Path(args.universe_file))
            print(f"  Symbols: {len(syms)} included from universe file {args.universe_file}")
        elif args.source == "local_vendor":
            incoming = _resolve_incoming_root(args)
            if incoming and Path(incoming).exists():
                from wtpy.apps.astock.data.providers.local_vendor import LocalVendorProvider
                from wtpy.apps.astock.data.vendor_universe import build_vendor_universe
                prov = LocalVendorProvider(incoming)
                if prov.health_check():
                    uni = build_vendor_universe(prov, with_metadata=False)
                    cs = max(1, int(args.chunk_size or 500))
                    n = len(uni.included_symbols)
                    print(f"  Symbols: {n} included (dynamic historical union, "
                          f"segment-only fast preview; the real import re-confirms "
                          f"with equity metadata, hash={uni.universe_hash[:12]})")
                    print(f"  Excluded non-A/unidentified: {uni.summary.get('excluded_count')}")
                    print(f"  Chunk plan: {(n + cs - 1) // cs} chunks x {cs}")
                else:
                    print("  Symbols: (incoming root has no year ZIPs)")
            else:
                print("  Symbols: (incoming root not configured/found)")
        else:
            print(f"  Symbols: (will use full universe)")
        print(f"  Date range: {args.start_date} - {args.end_date}")
        print("  No data will be written.")
        return

    store = DatasetStore(storage_root)

    if args.mode == "audit":
        result = audit_dataset(args, store)
        return

    sources = [args.source] if args.source != "all" else ["tdxquant", "tushare", "tdx_local", "local_vendor"]
    all_results = {}

    for src in sources:
        print(f"\n{'='*60}")
        print(f"Source: {src} | Mode: {args.mode}")
        print(f"{'='*60}")

        if src == "tdxquant":
            if args.mode in ("full", "rebuild"):
                r = sync_tdxquant_full(args, store)
            else:
                r = sync_tdxquant_incremental(args, store)
        elif src == "tushare":
            if args.mode in ("full", "rebuild"):
                r = sync_tushare_full(args, store)
            else:
                r = sync_tushare_incremental(args, store)
        elif src == "tdx_local":
            r = sync_tdx_local_full(args, store)
        elif src == "local_vendor":
            r = sync_local_vendor_full(args, store)
        else:
            r = {"status": "failed", "error": f"unknown source: {src}"}

        all_results[src] = r

    print(f"\n{'='*60}")
    print("Sync complete.")
    print(json.dumps(all_results, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
