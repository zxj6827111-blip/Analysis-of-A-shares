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
from typing import Dict, List, Optional, Tuple

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


def _infer_incremental_resume(
    store: DatasetStore,
    *,
    source: str,
    adjustment: str,
    require_rows: int = 500,
) -> Tuple[Optional[int], Optional[str]]:
    """Resume window + parent dataset for an incremental sync.

    Returns (start_date, parent_dataset_id): resume from the latest ready
    manifest cutoff minus a safety margin (weekends / holidays / late vendor
    updates), and merge that parent's history into the new window so the
    dataset never orphans bars.

    Without a resume window, UI-launched incremental syncs used start_date=None
    — a full-history refetch per symbol that Tushare truncates at 6000 rows per
    call (about 25 years), leaving series stuck on early-2000s data. Parents
    whose per-symbol rows look degenerate (< require_rows average) are skipped
    so a window-only dataset never becomes the next parent.
    """
    import datetime as _dt

    best_id: Optional[str] = None
    best_cut = 0
    cands: List[DatasetManifest] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if not m:
            continue
        if m.source != source or m.adjustment != adjustment or m.status != "ready":
            continue
        c = int(m.data_cutoff_date or 0)
        if c <= 0:
            continue
        n = int(m.symbol_count or 0)
        avg = (int(m.row_count or 0) / n) if n else 0
        if avg < require_rows:
            continue
        cands.append(m)
    # Drop small pools (e.g. an index/ETF-only set with a newer cutoff) so a
    # full-market parent wins; otherwise most symbols find no history to merge.
    if cands:
        n_max = max(int(m.symbol_count or 0) for m in cands)
        cands = [
            m for m in cands
            if int(m.symbol_count or 0) >= max(50, int(n_max * 0.5))
        ]
    if cands:
        best_m = max(
            cands,
            key=lambda m: (
                int(m.data_cutoff_date or 0),
                int(m.symbol_count or 0),
                int(m.row_count or 0),
            ),
        )
        best_id = best_m.dataset_id
        best_cut = int(best_m.data_cutoff_date or 0)
    if best_id is None:
        return None, None
    d = _dt.datetime.strptime(str(best_cut), "%Y%m%d").date()
    d -= _dt.timedelta(days=20)
    return int(d.strftime("%Y%m%d")), best_id


def _normalize_symbol(symbol: str) -> str:
    """Normalize any symbol format to SSE.STK.600000 / SZSE.STK.000001 / BSE.STK.430047.

    Supported input formats:
      SSE.STK.600000 / SZSE.STK.000001 / BSE.STK.430047  (canonical, pass-through)
      600000.SH / 000001.SZ / 430047.BJ
      sh600000 / sz000001 / bj430047
      600000 / 000001 / 430047  (bare 6-digit, exchange inferred)
    Index/ETF forms map to SSE.IDX.* / SZSE.IDX.* / SSE.ETF.* / SZSE.ETF.*
      (sh000001, sz399001, sh510300, sz159915, 000001.SH-as-index ...).
    """
    from wtpy.apps.astock.service.index_etf import to_index_etf_std_code

    idx_etf = to_index_etf_std_code(symbol)
    if idx_etf:
        return idx_etf
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
        repo_tmp = MarketDataRepository(store)
        try:
            latest = repo_tmp.resolve_latest_ready(
                source=DataSource.TDXQUANT.value,
                adjustment=AdjustmentMode.FRONT.value,
                period=BarPeriod.DAY.value,
            )
            symbols = [s.symbol for s in latest.symbols if s.quality == "ok"]
            print(f"  Using {len(symbols)} symbols from latest dataset {latest.dataset_id}")
        except Exception:
            pass
    symbols = [s for s in symbols if not s.startswith("BSE.STK.920")]
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

    _status, _detail = _aggregate_dataset_status(results)
    _result = {"status": _status, "sync_run_id": sync_run_id, "datasets": results}
    if _status == "failed":
        _result["error"] = _detail
    elif _status == "partial":
        _result["warning"] = f"datasets not all ready: {_detail}"
    return _result


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
        repo_tmp = MarketDataRepository(store)
        try:
            latest = repo_tmp.resolve_latest_ready(
                source=DataSource.TDXQUANT.value,
                adjustment=AdjustmentMode.FRONT.value,
                period=BarPeriod.DAY.value,
            )
            symbols = [s.symbol for s in latest.symbols if s.quality == "ok"]
            print(f"  Using {len(symbols)} symbols from latest dataset {latest.dataset_id}")
        except Exception:
            pass
    symbols = [s for s in symbols if not s.startswith("BSE.STK.920")]
    if not symbols:
        return {"status": "failed", "error": "no_symbols"}

    print(f"Incremental sync for {len(symbols)} symbols from TdxQuant...")
    sync_run_id = make_sync_run_id("tdxquant")

    repo = MarketDataRepository(store)
    rebuild_symbols: set = set()
    latest_front = None
    parent_ds_id = None

    # Always resolve parent front dataset when present (needed for true incremental
    # merge and for CA detection). Previously skip_ca left parent unset (NameError).
    try:
        latest_front = repo.resolve_latest_ready(
            source=DataSource.TDXQUANT.value,
            adjustment=AdjustmentMode.FRONT.value,
            period=BarPeriod.DAY.value,
        )
        parent_ds_id = latest_front.dataset_id
        print(f"  Parent front dataset: {parent_ds_id}", flush=True)
    except Exception as e:
        print(
            f"  No parent front dataset (will full-fetch front): {type(e).__name__}",
            flush=True,
        )

    skip_ca = getattr(args, "skip_ca_detect", False)
    if skip_ca:
        print("  --skip-ca-detect: skipping per-symbol CA detection (fast mode)")
        print(
            "  front will use windowed fetch + parent merge (not full rebuild)",
            flush=True,
        )
    elif latest_front is not None:
        try:
            overlap_start = _recent_trading_days_ago(60)
            n_syms = len(symbols)
            for idx, sym in enumerate(symbols):
                if idx % 100 == 0 or idx == n_syms - 1:
                    print(
                        f"[SYNC_PROGRESS] done={idx} total={n_syms} phase=ca_detect",
                        flush=True,
                    )
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
                        rebuild_symbols.add(_normalize_symbol(sym))
                except Exception:
                    rebuild_symbols.add(_normalize_symbol(sym))
        except Exception:
            rebuild_symbols = {_normalize_symbol(s) for s in symbols}
    else:
        # No parent: every symbol needs a full front history pull.
        rebuild_symbols = {_normalize_symbol(s) for s in symbols}

    print(
        f"  {len(rebuild_symbols)} symbols need full rebuild (CA detected / no parent)"
    )
    results = {}

    incremental_start = _recent_trading_days_ago(60)
    # Prefer explicit CLI start_date for the incremental window when provided.
    front_window_start = args.start_date or incremental_start

    # none/1d: always short window (unadjusted bars do not rewrite history).
    # front/1d: windowed + parent merge for non-rebuild; full history for rebuild.
    # front/1w: same window (no week parent merge yet — week series is shorter).
    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY, incremental_start, None, set()),
        (
            AdjustmentMode.FRONT,
            BarPeriod.DAY,
            front_window_start,
            parent_ds_id,
            rebuild_symbols,
        ),
        # Week series has no parent merge path yet; keep full fetch for correctness.
        (AdjustmentMode.FRONT, BarPeriod.WEEK, None, None, set()),
    ]
    for adj, period, use_start, parent_id, rebuilds in configs:
        print(
            f"  Phase {adj.value}/{period.value}: symbols={len(symbols)} "
            f"start={use_start} rebuild_full={len(rebuilds)} parent={parent_id}",
            flush=True,
        )
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TDXQUANT.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=use_start,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
            parent_dataset_id=parent_id,
            rebuild_symbols=rebuilds or None,
        )
        results[f"{adj.value}_{period.value}"] = ds_result
        print(
            f"  {adj.value}/{period.value}: "
            f"{ds_result.get('success', 0)}/{ds_result.get('total', 0)} ok, "
            f"dataset={ds_result.get('dataset_id', 'N/A')} "
            f"elapsed={ds_result.get('elapsed_sec', '?')}s",
            flush=True,
        )

    _status, _detail = _aggregate_dataset_status(results)
    _result = {"status": _status, "sync_run_id": sync_run_id, "datasets": results}
    if _status == "failed":
        _result["error"] = _detail
    elif _status == "partial":
        _result["warning"] = f"datasets not all ready: {_detail}"
    return _result


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

    # A "full" sync without explicit start_date used to refetch full history
    # every run (and hit the 6000-row single-call cap). Resume from the latest
    # ready dataset instead unless the user explicitly asks for history.
    resume: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    for adj in (AdjustmentMode.NONE, AdjustmentMode.QFQ):
        if args.start_date is None:
            inferred, parent_id = _infer_incremental_resume(
                store, source=DataSource.TUSHARE.value, adjustment=adj.value
            )
            if inferred:
                print(f"  [auto] no --start-date given: resuming {adj.value}/1d "
                      f"from {inferred} (latest ready cutoff - 20d)")
                resume[adj.value] = (inferred, parent_id)
        else:
            _, parent_id = _infer_incremental_resume(
                store, source=DataSource.TUSHARE.value, adjustment=adj.value
            )
            resume[adj.value] = (args.start_date, parent_id)

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY),
        (AdjustmentMode.QFQ, BarPeriod.DAY),
    ]
    for adj, period in configs:
        start, parent = resume.get(adj.value, (None, None))
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TUSHARE.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=start,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
            parent_dataset_id=parent,
        )
        results[f"{adj.value}_{period.value}"] = ds_result
        print(
            f"  {adj.value}/{period.value}: "
            f"{ds_result['success']}/{ds_result['total']} ok, "
            f"dataset={ds_result.get('dataset_id', 'N/A')}"
        )

    result = {"status": "success", "sync_run_id": sync_run_id, "datasets": results}
    _status, _detail = _aggregate_dataset_status(results)
    if _status == "failed":
        result["status"] = "failed"
        result["error"] = _detail
    elif _status == "partial":
        result["status"] = "partial"
        result["warning"] = f"datasets not all ready: {_detail}"
    result["reconcile"] = _reconcile_after_sync(store)
    _apply_reconcile_status(result)
    return result


def sync_tushare_incremental(
    args, store: DatasetStore, *, skip_reconcile_status: bool = False
) -> dict:
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

    # Resume from the latest ready dataset (merging its history) unless the
    # user pins a start date; without this, incremental == full-history
    # refetch (6000-row cap truncates) or a window-only orphan dataset.
    resume: Dict[str, Tuple[Optional[int], Optional[str]]] = {}
    for adj in (AdjustmentMode.NONE, AdjustmentMode.QFQ):
        if args.start_date is None:
            inferred, parent_id = _infer_incremental_resume(
                store, source=DataSource.TUSHARE.value, adjustment=adj.value
            )
            if inferred:
                print(f"  [auto] no --start-date given: resuming {adj.value}/1d "
                      f"from {inferred} (latest ready cutoff - 20d)")
                resume[adj.value] = (inferred, parent_id)
        else:
            _, parent_id = _infer_incremental_resume(
                store, source=DataSource.TUSHARE.value, adjustment=adj.value
            )
            resume[adj.value] = (args.start_date, parent_id)

    ck_path = store.sync_logs_dir / "checkpoint_tushare_incremental_1d.json"
    ck = None
    if ck_path.exists():
        try:
            ck = json.loads(ck_path.read_text(encoding="utf-8"))
        except Exception:
            ck = None
    if ck and not getattr(args, "resume", False) and not getattr(args, "fresh", False):
        print("ERROR: tushare incremental checkpoint exists. Use --resume or --fresh.")
        return {"status": "failed", "error": "checkpoint_exists_use_resume_or_fresh"}
    if getattr(args, "fresh", False) and ck_path.exists():
        ck_path.unlink()
        ck = None
    if getattr(args, "resume", False) and ck:
        sync_run_id = ck.get("sync_run_id", sync_run_id)
        print(f"  Resuming tushare incremental sync_run_id={sync_run_id}")

    configs = [
        (AdjustmentMode.NONE, BarPeriod.DAY),
        (AdjustmentMode.QFQ, BarPeriod.DAY),
    ]
    results = {}
    for adj, period in configs:
        phase_key = f"{adj.value}/{period.value}"
        resume_records = None
        if ck and getattr(args, "resume", False):
            phase = ck.get("phases", {}).get(phase_key)
            if phase:
                resume_records = phase.get("done", {})
        start, parent = resume.get(adj.value, (None, None))
        ds_result = _sync_dataset(
            provider=provider,
            store=store,
            symbols=symbols,
            source=DataSource.TUSHARE.value,
            adjustment=adj,
            period=period,
            sync_run_id=sync_run_id,
            start_date=start,
            end_date=args.end_date,
            anchor_date=args.anchor_date,
            parent_dataset_id=parent,
            checkpoint_path=ck_path,
            resume_records=resume_records,
        )
        results[f"{adj.value}_{period.value}"] = ds_result

    result = {"status": "success", "sync_run_id": sync_run_id, "datasets": results}
    _status, _detail = _aggregate_dataset_status(results)
    if _status == "failed":
        result["status"] = "failed"
        result["error"] = _detail
    elif _status == "partial":
        result["status"] = "partial"
        result["warning"] = f"datasets not all ready: {_detail}"
    if skip_reconcile_status:
        # Chain mode: the raw step must not run a full product reconcile.
        # The chain runs one authoritative reconcile after the factor step
        # (raw runs before the day's factors exist, so a raw-side reconcile
        # would be a wasted full-market L2/L1 derivation that can only ever
        # report waiting_for_parent or stale results).
        result["reconcile"] = {
            "status": "deferred",
            "reason": "chain_reconcile_at_end",
        }
    else:
        result["reconcile"] = _reconcile_after_sync(store)
        _apply_reconcile_status(result)
    return result


def sync_tushare_chain(args, store: DatasetStore) -> dict:
    """Zero-config default chain (task=tushare): raw -> factor -> reconcile.

    Runs the Tushare raw incremental sync, then the adj_factor incremental
    sync (reusing the latest ready factor manifest's universe file, with a
    default raw cache under the external data root), then the product
    reconcile. The task only reports success when raw, factor and the formal
    L1/L2 product chain (lineage + freshness) all pass — a raw success with
    a failed factor step or blocked reconcile never claims "sync success".

    Fail-closed on the raw step: only a raw step that finished ``success``
    may run the factor step. Any other raw status (partial / warning /
    failed) skips factor and reconcile entirely, so a partial raw surface
    can never update the formal L1/L2 product surfaces.

    The raw step runs with ``skip_reconcile_status=True``, which now skips
    the product reconcile work entirely (not just the status demotion): the
    raw step runs before the factor step pulled the day's factors, so its
    reconcile would only report waiting_for_parent for a same-day run and
    would duplicate the full L2/L1 derivation. The raw step's ``reconcile``
    entry is a structured ``{"status": "deferred"}`` placeholder instead.
    The chain runs the single authoritative reconcile after the factor step;
    only that result is applied to the chain status.
    """
    import copy as _copy

    raw_result = sync_tushare_incremental(
        args, store, skip_reconcile_status=True
    )
    chain: dict = {
        "status": raw_result.get("status", "failed"),
        "sync_run_id": raw_result.get("sync_run_id"),
        "raw": raw_result,
        "datasets": dict(raw_result.get("datasets") or {}),
        "reconcile": raw_result.get("reconcile"),
    }
    raw_status = str(raw_result.get("status", "failed") or "failed")
    if raw_status != "success":
        # Fail-closed: only a fully successful raw surface may feed the
        # factor step. A partial/warning raw surface (some datasets not
        # ready) must never pull factors or touch the formal L1/L2 chain.
        chain["factor"] = {
            "status": "skipped",
            "reason": "raw_step_not_success",
            "raw_status": raw_status,
        }
        return chain

    # ---- factor step: env > latest ready factor manifest universe ----
    fa = _copy.copy(args)
    fa.adjustment = "adj_factor"
    fa.mode = "incremental"
    if not getattr(fa, "universe_file", None):
        fa.universe_file = _latest_factor_universe_file_path(store)
        if fa.universe_file:
            print(f"  [chain] factor universe file <- {fa.universe_file}")
        else:
            fa.universe_file = _auto_generate_factor_universe(store)
            if fa.universe_file:
                print(f"  [chain] auto factor universe <- {fa.universe_file}")
            else:
                print("  [chain] no factor universe file (env or ready factor manifest)")
    fa.factor_raw_root = (
        _resolve_factor_raw_root(args) or str(_factor_raw_cache_dir(store))
    )

    factor_result = sync_tushare_adj_factor_full(fa, store)
    chain["factor"] = factor_result
    chain["reconcile"] = factor_result.get("reconcile") or chain.get("reconcile")
    if factor_result.get("status") != "success":
        chain["status"] = "failed"
        chain["error"] = (
            f"factor step failed: {factor_result.get('error') or factor_result.get('status')}"
        )
        return chain
    if factor_result.get("dataset_status") != "ready":
        chain["status"] = "warning"
        chain["warning"] = (
            "factor step did not publish ready: "
            f"dataset_status={factor_result.get('dataset_status')}"
        )
        return chain

    # ---- final reconcile over the updated parents ----
    chain["reconcile"] = _reconcile_after_sync(store)
    chain["factor_dataset_id"] = factor_result.get("dataset_id")
    chain["status"] = "success"
    _apply_reconcile_status(chain)
    return chain


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
    _status, _detail = _aggregate_dataset_status({"none_1d": ds_result})
    _result = {"status": _status, "sync_run_id": sync_run_id,
               "datasets": {"none_1d": ds_result},
               "dataset_status": ds_result.get("status")}
    if _status == "failed":
        _result["error"] = _detail
    elif _status == "partial":
        _result["warning"] = f"datasets not all ready: {_detail}"
    return _result


def _resolve_index_etf_symbols(args, provider) -> List[str]:
    """Resolve index/ETF universe for tushare sync (--asset-class index|etf|all).

    --symbol wins; otherwise the Tushare index/ETF universe (filtered by
    --asset-class) is fetched. Universe entries arrive as SSE.IDX.* /
    SZSE.IDX.* / SSE.ETF.* / SZSE.ETF.* canonical symbols.
    """
    if args.symbol:
        return [s.strip() for s in args.symbol.split(",") if s.strip()]
    asset = (getattr(args, "asset_class", "index") or "index").lower()
    universe = provider.fetch_index_etf_universe()
    if asset == "index":
        return [e.symbol for e in universe if ".IDX." in e.symbol]
    if asset == "etf":
        return [e.symbol for e in universe if ".ETF." in e.symbol]
    return [e.symbol for e in universe]


def _index_etf_configs() -> List[tuple]:
    """Index/ETF sync configs: unadjusted only (indices/ETFs have no 复权)."""
    return [(AdjustmentMode.NONE, BarPeriod.DAY)]


def sync_tushare_index_etf_full(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    provider = TushareProvider(token=args.token)
    if not provider.health_check():
        print("ERROR: Tushare API not available (check token/network)")
        return {"status": "failed", "error": "api_unavailable"}

    symbols = _resolve_index_etf_symbols(args, provider)
    if not symbols:
        print("ERROR: No index/ETF symbols to sync")
        return {"status": "failed", "error": "no_symbols"}

    print(f"Syncing {len(symbols)} index/ETF symbols from Tushare (full mode)...")
    sync_run_id = make_sync_run_id("tushare_ie")

    results = {}
    for adj, period in _index_etf_configs():
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

    _status, _detail = _aggregate_dataset_status(results)
    _result = {"status": _status, "sync_run_id": sync_run_id, "datasets": results}
    if _status == "failed":
        _result["error"] = _detail
    elif _status == "partial":
        _result["warning"] = f"datasets not all ready: {_detail}"
    return _result


def sync_tushare_index_etf_incremental(args, store: DatasetStore) -> dict:
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    provider = TushareProvider(token=args.token)
    if not provider.health_check():
        print("ERROR: Tushare API not available")
        return {"status": "failed", "error": "api_unavailable"}

    symbols = _resolve_index_etf_symbols(args, provider)
    if not symbols:
        print("ERROR: No index/ETF symbols to sync")
        return {"status": "failed", "error": "no_symbols"}

    print(f"Incremental sync for {len(symbols)} index/ETF symbols from Tushare...")
    sync_run_id = make_sync_run_id("tushare_ie")

    asset = (getattr(args, "asset_class", "index") or "index").lower()
    ck_path = store.sync_logs_dir / f"checkpoint_tushare_index_etf_{asset}_1d.json"
    ck = None
    if ck_path.exists():
        try:
            ck = json.loads(ck_path.read_text(encoding="utf-8"))
        except Exception:
            ck = None
    if ck and not getattr(args, "resume", False) and not getattr(args, "fresh", False):
        print(f"ERROR: tushare index/ETF ({asset}) incremental checkpoint exists: "
              f"{ck_path.name}. Use --resume or --fresh.")
        return {"status": "failed", "error": "checkpoint_exists_use_resume_or_fresh"}
    if getattr(args, "fresh", False) and ck_path.exists():
        ck_path.unlink()
        ck = None
    if getattr(args, "resume", False) and ck:
        sync_run_id = ck.get("sync_run_id", sync_run_id)
        print(f"  Resuming tushare index/ETF incremental sync_run_id={sync_run_id}")

    results = {}
    for adj, period in _index_etf_configs():
        phase_key = f"{adj.value}/{period.value}"
        resume_records = None
        if ck and getattr(args, "resume", False):
            phase = ck.get("phases", {}).get(phase_key)
            if phase:
                resume_records = phase.get("done", {})
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
            checkpoint_path=ck_path,
            resume_records=resume_records,
        )
        results[f"{adj.value}_{period.value}"] = ds_result

    _status, _detail = _aggregate_dataset_status(results)
    _result = {"status": _status, "sync_run_id": sync_run_id, "datasets": results}
    if _status == "failed":
        _result["error"] = _detail
    elif _status == "partial":
        _result["warning"] = f"datasets not all ready: {_detail}"
    return _result


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


FACTOR_INCREMENTAL_POLICY_VERSION = "factor_inc_v2_parent_merge"
FACTOR_INCREMENTAL_LOOKBACK_DAYS = 20
FACTOR_PARENT_MIN_AVG_ROWS = 250
# Tushare adj_factor(trade_date=...) single responses can be row-capped
# (~6000 rows), silently dropping symbols on low-tier tokens. The batch path
# self-checks daily coverage against the universe; only large universes are
# checked (truncation cannot bite small ones).
FACTOR_BATCH_COVERAGE_MIN_UNIVERSE = 100
FACTOR_BATCH_COVERAGE_RATIO = 0.9
# Batch windows are meant for short correction windows; an explicit
# --start-date far in the past would otherwise pull (and hold in memory)
# hundreds of whole-market responses.
FACTOR_BATCH_MAX_WINDOW_DAYS = 60
# The probe walks back from the cutoff day across up to this many calendar
# days to find the most recent trading day (weekend/holiday runs).
FACTOR_BATCH_PROBE_MAX_DAYS = 7
# Factor freshness gate: the share of raw-active stocks whose factor series
# reaches the raw baseline's per-symbol last date. Below the ratio the factor
# manifest is demoted to partial (global max date alone masks local stalls).
FRESH_RATIO_MIN = 0.95
FACTOR_FRESHNESS_RAW_TOLERANCE_DAYS = 5
# Factor-side lag tolerance: Tushare publishes adj_factor EOD after daily
# bars, so a same-day sync can legitimately lag the raw last date by one
# trading day; 3 natural days covers a weekend + the ordering gap.
FACTOR_FRESHNESS_FACTOR_TOLERANCE_DAYS = 3
QFQ_FORMULA_VERSION = "tsqfq_v1"
QFQ_ANCHOR_POLICY = "last_factor_on_or_before_cutoff"
QFQ_PRICE_PRECISION_POLICY = "round_half_even_4dp_store; compare at 2dp"

# Gate B6: composite QFQ — same per-symbol math as tsqfq_v1, extended with a
# supplement factor parent (delisted stocks) and BSE pre-migration alias
# factor resolution. Bump this version if either rule changes.
COMPOSITE_QFQ_FORMULA_VERSION = "ctsfqfq_v1"
FACTOR_RESOLUTION_RULE_VERSION = (
    "factor_resolution_v1:exact_main>exact_supplement>bse_alias_main"
)


def _resolve_factor_raw_root(args) -> Optional[str]:
    import os
    val = getattr(args, "factor_raw_root", None) or os.environ.get(
        "TUSHARE_FACTOR_RAW_ROOT", "").strip()
    return val or None


def _factor_raw_cache_dir(store: DatasetStore) -> Path:
    """Zero-config default raw cache under the external data root.

    Used only when neither --factor-raw-root nor TUSHARE_FACTOR_RAW_ROOT is
    set — no new required configuration for the Tushare-only chain.
    """
    return store.root / "tushare_factor_raw_cache"


def _latest_factor_universe_file_path(store: DatasetStore) -> Optional[str]:
    """Reuse the latest ready adj_factor manifest's universe file.

    Environment variables (TUSHARE_FACTOR_UNIVERSE_FILE /
    ASTOCK_FACTOR_UNIVERSE_FILE) still win over manifest reuse. A manifest's
    universe is only reusable when it spans the full market: at least
    FULL_MARKET_MIN_SYMBOLS included rows, or >= FULL_MARKET_RELATIVE_RATIO
    of the current full-market raw baseline's ok symbols. With no full-market
    raw baseline in the store the manifest universe is never reused (the
    chain falls back to the auto universe generator / universe_file_required),
    so a tiny 1-symbol universe can never drive a default incremental run.
    """
    import os as _os

    for key in ("TUSHARE_FACTOR_UNIVERSE_FILE", "ASTOCK_FACTOR_UNIVERSE_FILE"):
        explicit = _os.environ.get(key, "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    baseline = _select_factor_raw_baseline(store)
    baseline_syms: set = set()
    if baseline is not None:
        baseline_syms = {
            r.symbol for r in baseline.symbols
            if r.quality == "ok" and r.blob_sha256
        }
    candidates: List[tuple] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if not m:
            continue
        if (
            m.source != "tushare"
            or m.adjustment != "adj_factor"
            or m.status != "ready"
            or (m.dataset_type or "") != "factor"
        ):
            continue
        uf = (m.universe_file or "").strip()
        if not uf or not Path(uf).exists():
            continue
        candidates.append(
            (int(m.data_cutoff_date or 0), int(m.symbol_count or 0),
             m.created_at or "", uf)
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    for _, _, _, uf in candidates:
        uni_syms = _load_universe_file(Path(uf))
        if len(uni_syms) >= FULL_MARKET_MIN_SYMBOLS:
            return uf
        if baseline_syms and (
            len(uni_syms) / len(baseline_syms) >= FULL_MARKET_RELATIVE_RATIO
        ):
            return uf
    return None


# Full-market gates for the factor raw baseline (P1-2): the freshness gate
# and the auto universe generator must never run against a 16-row orphan
# window or a tiny subset surface — only a real full-market history may
# drive factor coverage decisions / universe generation.
FACTOR_RAW_BASELINE_MIN_SYMBOLS = 500
FACTOR_RAW_BASELINE_MIN_MEDIAN_ROWS = 250
# Full-market anchor for A-share stock universes: the quality=ok symbol pool
# must reach this size AND stay within 10% of the largest same-family ready
# tushare/none surface in the store, so a 500-symbol subset pool next to a
# 4000+ full market is never treated as a baseline.
FULL_MARKET_MIN_SYMBOLS = 4000
FULL_MARKET_RELATIVE_RATIO = 0.9


def _is_full_market_manifest(store: DatasetStore, manifest: DatasetManifest) -> bool:
    """True when a tushare/none manifest is a full-market raw surface.

    n = quality=ok symbols with a blob in this manifest; N_max = the largest
    such pool across all ready tushare/none/1d surfaces in the store. The
    manifest is full-market only when n >= FULL_MARKET_MIN_SYMBOLS AND
    n >= FULL_MARKET_RELATIVE_RATIO * N_max (a manifest that IS the largest
    surface trivially passes; small subset pools fail the relative gate).
    Orphan windows fail on both count and rows.
    """
    n = sum(
        1 for r in manifest.symbols
        if r.quality == "ok" and r.blob_sha256
    )
    if n < FULL_MARKET_MIN_SYMBOLS:
        return False
    n_max = n
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if (
            m.source != "tushare"
            or (m.adjustment or "") != "none"
            or (m.period or "1d") != "1d"
            or m.status != "ready"
        ):
            continue
        n_max = max(
            n_max,
            sum(1 for r in m.symbols if r.quality == "ok" and r.blob_sha256),
        )
    return n >= FULL_MARKET_RELATIVE_RATIO * n_max


def _select_factor_raw_baseline(store: DatasetStore) -> Optional[DatasetManifest]:
    """Latest complete ready tushare/none full-market raw baseline.

    Reuses the formal base selector (blob-integrity gate + freshest/fullest
    ordering + orphan-window rejection) and adds a full-market gate on top:
    symbol count >= FACTOR_RAW_BASELINE_MIN_SYMBOLS, median rows >=
    FACTOR_RAW_BASELINE_MIN_MEDIAN_ROWS and _is_full_market_manifest
    (>= FULL_MARKET_MIN_SYMBOLS symbols with >= FULL_MARKET_RELATIVE_RATIO
    relative coverage). Orphan windows (16-row truncations, incremental
    windows without parent merge) are rejected outright. Used by the factor
    freshness gate (P1-4) and the auto universe generator (P1-5).
    """
    from wtpy.apps.astock.data.tushare_product import (
        manifest_history_signals,
        select_tushare_base,
    )

    base = select_tushare_base(store)
    if base is None:
        return None
    sig = manifest_history_signals(base)
    if int(sig.symbol_count or 0) < FACTOR_RAW_BASELINE_MIN_SYMBOLS:
        return None
    if float(sig.median_rows or 0) < FACTOR_RAW_BASELINE_MIN_MEDIAN_ROWS:
        return None
    if not _is_full_market_manifest(store, base):
        return None
    return base


def _factor_freshness_metrics(
    store: DatasetStore,
    factor_manifest: DatasetManifest,
    raw_manifest: Optional[DatasetManifest] = None,
) -> Optional[dict]:
    """Per-symbol factor freshness vs the raw baseline's active stocks.

    Active = raw quality ok AND raw last_date within
    FACTOR_FRESHNESS_RAW_TOLERANCE_DAYS of the raw cutoff (suspended/delisted
    stocks are auto-exempt). A factor symbol is fresh when its last_date is
    within FACTOR_FRESHNESS_FACTOR_TOLERANCE_DAYS of the raw symbol's last
    date (Tushare publishes adj_factor EOD after daily bars; 3 days covers a
    weekend + one trading-day ordering lag). Returns None when no raw
    baseline exists or there are no active symbols (the gate then passes).
    """
    import datetime as _dt

    if raw_manifest is None:
        raw_manifest = _select_factor_raw_baseline(store)
    if raw_manifest is None:
        return None
    raw_cutoff = int(raw_manifest.data_cutoff_date or 0)
    if not raw_cutoff:
        return None
    try:
        cutoff_d = _dt.datetime.strptime(str(raw_cutoff), "%Y%m%d").date()
    except ValueError:
        return None
    tol_d = cutoff_d - _dt.timedelta(days=FACTOR_FRESHNESS_RAW_TOLERANCE_DAYS)

    factor_by_sym = {r.symbol: r for r in factor_manifest.symbols}
    active: List[tuple] = []  # (raw_last_date_obj, raw_last, fac_last, symbol)
    for r in raw_manifest.symbols:
        if r.quality != "ok" or not r.blob_sha256:
            continue
        raw_last = int(r.last_date or 0)
        if not raw_last:
            continue
        try:
            rl_d = _dt.datetime.strptime(str(raw_last), "%Y%m%d").date()
        except ValueError:
            continue
        if rl_d < tol_d:
            continue  # suspended/delisted: auto-exempt
        fac = factor_by_sym.get(r.symbol)
        fac_last = int(fac.last_date or 0) if fac else 0
        active.append((rl_d, raw_last, fac_last, r.symbol))
    if not active:
        return None

    def _is_fresh(fac_last: int, raw_last_d) -> bool:
        if not fac_last:
            return False
        try:
            fl_d = _dt.datetime.strptime(str(fac_last), "%Y%m%d").date()
        except ValueError:
            return False
        return fl_d >= raw_last_d - _dt.timedelta(
            days=FACTOR_FRESHNESS_FACTOR_TOLERANCE_DAYS)

    fresh = sum(1 for rl_d, _, fac_last, _ in active if _is_fresh(fac_last, rl_d))
    stale = sorted(
        ({"symbol": sym, "factor_last_date": fac_last,
          "raw_last_date": raw_last}
         for rl_d, raw_last, fac_last, sym in active
         if not _is_fresh(fac_last, rl_d)),
        key=lambda s: int(s["raw_last_date"] or 0) - int(s["factor_last_date"] or 0),
        reverse=True,
    )
    factor_lasts = sorted(fac_last for _, _, fac_last, _ in active)
    n = len(factor_lasts)
    return {
        "fresh_symbol_ratio": round(fresh / len(active), 4),
        "fresh_count": fresh,
        "active_count": len(active),
        "stale_active_symbols": stale[:20],
        "p50_last_date": factor_lasts[n // 2] if n else 0,
        "p10_last_date": factor_lasts[max(0, int(n * 0.1))] if n else 0,
        "raw_dataset_id": raw_manifest.dataset_id,
        "fresh_tolerance_days": FACTOR_FRESHNESS_FACTOR_TOLERANCE_DAYS,
    }


def _auto_generate_factor_universe(store: DatasetStore) -> Optional[str]:
    """Generate a factor universe CSV from the raw baseline's ok symbols.

    First-migration path (P1-5): when no ready factor manifest exists yet,
    no universe file can be reused, so build one from the latest complete
    tushare/none raw manifest. Format is compatible with _load_universe_file
    (canonical_symbol + inclusion_status=included). Returns the generated
    path or None when no raw baseline is available.
    """
    import datetime as _dt
    import hashlib as _hl2

    raw = _select_factor_raw_baseline(store)
    if raw is None:
        return None
    symbols = sorted(
        r.symbol for r in raw.symbols
        if r.quality == "ok" and r.blob_sha256
    )
    if not symbols:
        return None
    cutoff = int(raw.data_cutoff_date or 0)
    uni_dir = store.root / "universes"
    uni_dir.mkdir(parents=True, exist_ok=True)
    payload = (
        "canonical_symbol,inclusion_status\n"
        + "\n".join(f"{s},included" for s in symbols) + "\n"
    )
    uni_sha = _hl2.sha256(payload.encode("utf-8-sig")).hexdigest()
    sha8 = uni_sha[:8]
    path = uni_dir / f"auto_factor_universe_{cutoff}_{sha8}.csv"
    if not path.exists():
        path.write_text(payload, encoding="utf-8-sig")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        meta_path.write_text(
            json.dumps(
                {
                    "source_dataset_id": raw.dataset_id,
                    "data_cutoff_date": cutoff,
                    "generated_at": _dt.datetime.now().isoformat(
                        timespec="seconds"),
                    "symbol_count": len(symbols),
                    "universe_sha256": uni_sha,
                    "file": str(path),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    print(f"  [auto] factor universe generated -> {path}")
    return str(path)


def _select_factor_incremental_parent(
    store: DatasetStore,
) -> Optional[DatasetManifest]:
    """Select a complete factor parent, rejecting short-window shells.

    Ready manifests win; a ``partial`` manifest demoted by the freshness gate
    (provenance freshness.gate == "blocked") is still acceptable as the
    window-continuation parent — its series are complete, only per-symbol
    freshness lags, and refusing it would degrade the next run to a
    full-history refetch. Other partial manifests (provider failures) are
    rejected: merging onto them would silently keep broken records.
    """
    candidates: List[DatasetManifest] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if not m:
            continue
        if (
            m.source != "tushare"
            or m.adjustment != "adj_factor"
            or (m.dataset_type or "") != "factor"
        ):
            continue
        if m.status == "ready":
            pass
        elif (
            m.status == "partial"
            and ((m.provenance or {}).get("freshness") or {}).get("gate")
            == "blocked"
        ):
            pass
        else:
            continue
        records = [r for r in m.symbols if r.blob_sha256]
        avg_rows = sum(int(r.row_count or 0) for r in records) / max(len(records), 1)
        if avg_rows < FACTOR_PARENT_MIN_AVG_ROWS:
            continue
        if any(not store.blob_exists(r.blob_sha256) for r in records):
            continue
        candidates.append(m)
    if not candidates:
        return None

    # A small, newer pool must not displace the full-market factor history.
    max_symbols = max(int(m.symbol_count or 0) for m in candidates)
    candidates = [
        m for m in candidates
        if int(m.symbol_count or 0) >= max(1, int(max_symbols * 0.5))
    ]
    return max(
        candidates,
        key=lambda m: (
            max((int(r.last_date or 0) for r in m.symbols), default=0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
    )


def _factor_resume_start(parent: DatasetManifest) -> Optional[int]:
    """Return parent real max date minus a calendar-day correction window."""
    import datetime as _dt

    last_date = max((int(r.last_date or 0) for r in parent.symbols), default=0)
    if not last_date:
        return None
    try:
        day = _dt.datetime.strptime(str(last_date), "%Y%m%d").date()
    except ValueError:
        return None
    day -= _dt.timedelta(days=FACTOR_INCREMENTAL_LOOKBACK_DAYS)
    return int(day.strftime("%Y%m%d"))


def _merge_factor_history(parent_arrays: dict, window_df):
    """Merge factor history with a correction window; window values win."""
    import pandas as pd

    old = pd.DataFrame({
        "trade_date": parent_arrays["trade_date"],
        "adj_factor": parent_arrays["adj_factor"],
    })
    merged = pd.concat(
        [old, window_df[["trade_date", "adj_factor"]]], ignore_index=True
    )
    merged["trade_date"] = merged["trade_date"].astype(str).astype(int)
    merged["adj_factor"] = merged["adj_factor"].astype(float)
    merged = merged.sort_values("trade_date", kind="stable")
    return merged.drop_duplicates(
        subset="trade_date", keep="last"
    ).reset_index(drop=True)


def _fetch_factor_window_by_trade_date(
    provider,
    window_start: int,
    cutoff: int,
    stats: dict,
    universe_size: int,
    rate_per_min: int = 400,
) -> Optional[Dict[str, pd.DataFrame]]:
    """Pull a correction window market-wide, one trade_date call per day.

    Tushare's adj_factor(trade_date=...) returns every symbol's factor for a
    given day, so a ~60-calendar-day window costs ~40 calls instead of one per
    symbol. Returns {symbol: DataFrame(trade_date, adj_factor)} or None when
    the endpoint is unusable (permission/limits/errors/bad rows/truncated
    coverage/oversized window), letting the caller fall back to the
    per-symbol window path.
    """
    import datetime as _dt
    import pandas as pd

    try:
        start = _dt.datetime.strptime(str(window_start), "%Y%m%d").date()
        end = _dt.datetime.strptime(str(cutoff), "%Y%m%d").date()
    except ValueError:
        return None
    if (end - start).days > FACTOR_BATCH_MAX_WINDOW_DAYS:
        print(f"  WARNING: trade_date batch window {window_start}..{cutoff} "
              f"is {(end - start).days} calendar days (> "
              f"{FACTOR_BATCH_MAX_WINDOW_DAYS}); skipping batch, using "
              f"per-symbol window fetch")
        return None

    interval = 60.0 / max(1, int(rate_per_min or 400))

    def _day_unusable(df, d: int) -> Optional[str]:
        """Return a fallback reason when a day's response is unusable.

        None/empty responses are non-trading days (usable); missing columns
        and truncated coverage abort the batch.
        """
        if df is None or df.empty:
            return None
        if not {"ts_code", "trade_date", "adj_factor"}.issubset(df.columns):
            return f"missing required columns on {d}"
        sub = df[["ts_code", "trade_date", "adj_factor"]].dropna()
        if not sub.empty and universe_size >= FACTOR_BATCH_COVERAGE_MIN_UNIVERSE:
            covered = int(sub["ts_code"].nunique())
            if covered <= int(FACTOR_BATCH_COVERAGE_RATIO * universe_size):
                return f"coverage on {d} too low ({covered}/{universe_size})"
        return None

    # Probe: walk back from the cutoff day to the most recent day that
    # returns data (weekend/holiday runs must not fail the probe). None AND
    # empty DataFrames count as "no data day" (an empty probe must keep
    # walking back — treating it as usable would batch-pull a full empty
    # window and publish stale parent blobs as ready). The probe only
    # verifies the trade_date endpoint works; the day loop below still
    # starts at window_start.
    probe_day = end
    probe = int(end.strftime("%Y%m%d"))
    probe_df = None
    for _ in range(FACTOR_BATCH_PROBE_MAX_DAYS):
        d = int(probe_day.strftime("%Y%m%d"))
        try:
            df = provider.fetch_adj_factor(trade_date=d)
        except Exception as e:
            print(f"  WARNING: trade_date batch probe failed ({type(e).__name__}); "
                  f"falling back to per-symbol window fetch")
            return None
        if df is not None and not df.empty:
            probe, probe_df = d, df
            break
        probe_day -= _dt.timedelta(days=1)
        time.sleep(interval)
    if probe_df is None:
        first = int(end.strftime("%Y%m%d"))
        last = int((end - _dt.timedelta(
            days=FACTOR_BATCH_PROBE_MAX_DAYS - 1)).strftime("%Y%m%d"))
        print(f"  WARNING: trade_date batch probe found no data on "
              f"{first}..{last} (rolled back {FACTOR_BATCH_PROBE_MAX_DAYS} "
              f"days); falling back to per-symbol window fetch")
        return None
    if probe != int(end.strftime("%Y%m%d")):
        print(f"  [batch] probe rolled back to {probe} "
              f"(cutoff {int(end.strftime('%Y%m%d'))} has no data)")
    probe_bad = _day_unusable(probe_df, probe)
    if probe_bad:
        print(f"  WARNING: trade_date batch probe on {probe} unusable "
              f"({probe_bad}); falling back to per-symbol window fetch")
        return None

    frames: Dict[str, List[dict]] = {}
    day = start
    while day <= end:
        d = int(day.strftime("%Y%m%d"))
        try:
            df = provider.fetch_adj_factor(trade_date=d)
        except Exception as e:
            print(f"  WARNING: trade_date batch fetch {d} failed "
                  f"({type(e).__name__}); falling back to per-symbol window fetch")
            return None
        if df is None or df.empty:
            day += _dt.timedelta(days=1)
            time.sleep(interval)
            continue
        bad = _day_unusable(df, d)
        if bad:
            print(f"  WARNING: trade_date batch response unusable ({bad}); "
                  f"falling back to per-symbol window fetch")
            return None
        sub = df[["ts_code", "trade_date", "adj_factor"]].dropna()
        try:
            for _, row in sub.iterrows():
                symbol = provider._from_ts_code(str(row["ts_code"]))
                frames.setdefault(symbol, []).append(
                    {"trade_date": int(row["trade_date"]),
                     "adj_factor": float(row["adj_factor"])})
        except (KeyError, ValueError, TypeError) as e:
            print(f"  WARNING: trade_date batch row parse failed on {d} "
                  f"({type(e).__name__}); falling back to per-symbol "
                  f"window fetch")
            return None
        day += _dt.timedelta(days=1)
        time.sleep(interval)
    stats["batch_by_trade_date"] = True
    by_symbol: Dict[str, pd.DataFrame] = {}
    for symbol, rows in frames.items():
        wdf = pd.DataFrame(rows).drop_duplicates(subset="trade_date", keep="last")
        wdf["trade_date"] = wdf["trade_date"].astype(int)
        wdf["adj_factor"] = wdf["adj_factor"].astype(float)
        by_symbol[symbol] = wdf.sort_values("trade_date").reset_index(drop=True)
    return by_symbol


def _prune_raw_batches(factor_raw_root: str, keep: int) -> None:
    """Delete the oldest tsfactor_* batch dirs, keeping the newest ``keep``.

    The fixed "latest" incremental cache is never touched.
    """
    import shutil

    root = Path(factor_raw_root)
    if not root.is_dir() or keep < 1:
        return
    batches = sorted(
        (p for p in root.iterdir()
         if p.is_dir() and p.name.startswith("tsfactor_")),
        key=lambda p: p.name,
    )
    for old in batches[:-keep]:
        print(f"  [raw] pruning old batch dir: {old}")
        try:
            shutil.rmtree(old)
        except Exception as e:
            print(f"  WARNING: could not prune {old}: {e}")


def sync_tushare_adj_factor_full(args, store: DatasetStore) -> dict:
    """Sync Tushare adj_factor into an immutable complete-history dataset.

    Existing ``full`` and ``incremental`` commands reuse a complete ready
    parent and fetch only a correction window. ``rebuild`` is the explicit
    full-history path. Existing deployment commands remain compatible.
    """
    import os
    import hashlib as _hl
    from wtpy.apps.astock.data.providers.tushare import TushareProvider
    from wtpy.apps.astock.data.providers.base import ProviderError, RateLimited
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError

    if (args.adjustment or "") != "adj_factor":
        print("ERROR: factor sync requires --adjustment adj_factor")
        return {"status": "failed", "error": "adjustment_must_be_adj_factor"}

    factor_raw_root = _resolve_factor_raw_root(args)
    if not factor_raw_root:
        # Zero-config chain: safe default cache under the external data root
        # (never a required env var).
        factor_raw_root = str(_factor_raw_cache_dir(store))
        print(f"  [auto] factor raw cache default: {factor_raw_root}")

    if not getattr(args, "universe_file", None):
        print("ERROR: --universe-file (frozen vendor universe CSV) is required")
        return {"status": "failed", "error": "universe_file_required"}
    universe_path = Path(args.universe_file)
    symbols = _load_universe_file(universe_path)
    if not symbols:
        print("ERROR: universe file has no included symbols")
        return {"status": "failed", "error": "empty_universe"}
    uni_sha = _hl.sha256(universe_path.read_bytes()).hexdigest()
    universe_hash = _hl.sha256(",".join(symbols).encode()).hexdigest()

    sync_run_id = make_sync_run_id("tsfactor")
    lock = SyncTaskLock(store.root, source="tushare", adjustment="adj_factor",
                        period="1d", sync_run_id=sync_run_id)
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return {"status": "failed", "error": "concurrent_lock", "holder": e.holder}
    if lock.recovered_stale:
        print(f"NOTE: recovered stale lock from pid={lock.recovered_stale.get('pid')}")

    try:
        provider = TushareProvider(token=args.token)
        try:
            provider._ensure_initialized()
        except Exception as e:
            print(f"ERROR: Tushare init failed ({type(e).__name__}). "
                  f"Configure token via ts.set_token() — token is never printed.")
            return {"status": "failed", "error": "token_or_init_failed"}

        # instrument raw API calls (count only; kwargs never logged)
        stats = {"api_calls": 0, "rate_limited": 0, "provider_failed": 0,
                 "retries_estimated": 0}
        _orig_af = provider._pro.adj_factor

        def _counting_af(**kw):
            stats["api_calls"] += 1
            return _orig_af(**kw)

        provider._pro.adj_factor = _counting_af

        # stock_basic metadata (L + D) for mapping table
        print("Fetching stock_basic (L + D) for mapping metadata...")
        meta: Dict[str, dict] = {}
        for status_flag in ("L", "D"):
            try:
                df = provider._call_with_retry(
                    provider._pro.stock_basic, list_status=status_flag)
                if df is not None:
                    for _, row in df.iterrows():
                        ts_code = str(row.get("ts_code", ""))
                        canon = provider._from_ts_code(ts_code)
                        meta[canon] = {
                            "ts_code": ts_code,
                            "name": str(row.get("name", "")),
                            "list_status": status_flag,
                            "list_date": str(row.get("list_date", "") or ""),
                            "delist_date": str(row.get("delist_date", "") or ""),
                        }
            except Exception as e:
                print(f"  WARNING: stock_basic({status_flag}) failed: {type(e).__name__}")
        print(f"  stock_basic entries: {len(meta)}")

        mode = str(getattr(args, "mode", "full") or "full").lower()
        parent = (
            None if mode == "rebuild"
            else _select_factor_incremental_parent(store)
        )
        parent_records = {
            r.symbol: r for r in (parent.symbols if parent else [])
            if r.blob_sha256
        }
        inferred_start = _factor_resume_start(parent) if parent else None
        explicit_start = getattr(args, "start_date", None)
        window_start = int(explicit_start or inferred_start or 0) or None
        sync_kind = (
            "incremental_parent_merge"
            if parent and window_start else "full_history"
        )
        if parent and window_start:
            print(
                f"  Factor parent: {parent.dataset_id}; fetch window "
                f"{window_start}.."
                f"{getattr(args, 'end_date', None) or 'today'}"
            )
        else:
            print("  Factor parent: none; fetching full history")

        # checkpoint / resume (same sync_run_id on resume)
        ck_path = store.sync_logs_dir / "checkpoint_tushare_adj_factor_1d.json"
        ck = None
        if ck_path.exists():
            try:
                ck = json.loads(ck_path.read_text(encoding="utf-8"))
            except Exception:
                ck = None
        if ck and not getattr(args, "resume", False) and not getattr(args, "fresh", False):
            print("ERROR: factor checkpoint exists. Use --resume or --fresh.")
            return {"status": "failed", "error": "checkpoint_exists_use_resume_or_fresh"}
        requested_cutoff = int(
            getattr(args, "end_date", None) or time.strftime("%Y%m%d")
        )
        _resume_ok = False
        if getattr(args, "resume", False):
            if (
                not ck
                or ck.get("universe_hash") != universe_hash
                or ck.get("parent_dataset_id")
                != (parent.dataset_id if parent else None)
                or ck.get("window_start") != window_start
                or int(ck.get("cutoff") or 0) != requested_cutoff
            ):
                # Stale-but-compatible checkpoint: the universe and parent
                # match, only the window/cutoff moved (e.g. yesterday's
                # checkpoint re-run today, when the inferred window start is
                # necessarily different). Discard it and restart with a fresh
                # window instead of forcing --fresh.
                if (
                    ck
                    and ck.get("universe_hash") == universe_hash
                    and ck.get("parent_dataset_id")
                    == (parent.dataset_id if parent else None)
                ):
                    print(
                        f"  stale checkpoint from "
                        f"{ck.get('saved_at') or 'an earlier run'}: "
                        "window/cutoff changed, restarting with a fresh window"
                    )
                else:
                    print("ERROR: no matching factor checkpoint to resume")
                    print(
                        "  checkpoint is from a previous window/date or a "
                        "different universe; use --fresh to restart"
                    )
                    return {"status": "failed", "error": "checkpoint_mismatch"}
            else:
                _resume_ok = True
        if _resume_ok:
            sync_run_id = ck["sync_run_id"]
            lock.sync_run_id = sync_run_id
            done: Dict[str, dict] = ck.get("done", {})
            stats["api_calls"] = int(ck.get("api_calls", 0))
            print(f"  Resuming sync_run_id={sync_run_id}: {len(done)}/{len(symbols)} done")
        else:
            done = {}
            ck = {
                "universe_hash": universe_hash,
                "sync_run_id": sync_run_id,
                "done": done,
                "api_calls": 0,
                "parent_dataset_id": parent.dataset_id if parent else None,
                "window_start": window_start,
                "cutoff": requested_cutoff,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

        # incremental merges overwrite a fixed cache dir each run; full/rebuild
        # keep immutable per-run snapshots
        if sync_kind == "incremental_parent_merge":
            raw_dir = Path(factor_raw_root) / "latest"
            print(f"  [raw] incremental cache -> {raw_dir}")
        else:
            raw_dir = Path(factor_raw_root) / sync_run_id
        raw_dir.mkdir(parents=True, exist_ok=True)

        rate_per_min = max(1, int(getattr(args, "rate_per_min", None) or 400))
        min_interval = 60.0 / rate_per_min
        cutoff = requested_cutoff

        from wtpy.apps.astock.data.io_util import atomic_write_json
        t0 = time.time()
        last_call = 0.0
        pending = [s for s in symbols if s not in done]

        # Bulk window fetch: one whole-market trade_date call per calendar day
        # (~40 calls per window) instead of one API call per symbol (~6000).
        # A probe decides availability; any failure falls back to the
        # per-symbol loop below (existing behaviour unchanged). Skipped when a
        # resume already covered every symbol (empty pending).
        window_map = None
        if sync_kind == "incremental_parent_merge" and pending:
            window_map = _fetch_factor_window_by_trade_date(
                provider, window_start, cutoff, stats, len(symbols),
                rate_per_min)
            if window_map is not None:
                if window_map:
                    print(f"  [batch] trade_date window fetch ok: "
                          f"{len(window_map)} symbols in window")
                else:
                    print("  WARNING: trade_date batch window returned no "
                          "rows; every symbol keeps its parent history")
        print(f"Syncing adj_factor for {len(pending)} symbols "
              f"(rate<={rate_per_min}/min, total universe {len(symbols)})...")
        for i, sym in enumerate(pending):
            ts_code = provider._to_ts_code(sym)
            parent_rec = parent_records.get(sym)
            # Symbols absent from the parent need full history; otherwise
            # newly listed names would become window-only orphan series.
            symbol_start = window_start if parent_rec else None
            rec = {"symbol": sym, "ts_code": ts_code}
            try:
                if window_map is not None and parent_rec:
                    # Batch path: the window was already pulled market-wide;
                    # a symbol missing from it keeps the parent blob below.
                    df = window_map.get(sym)
                else:
                    # Per-symbol path (also the fallback when the trade_date
                    # batch endpoint is unavailable).
                    wait = min_interval - (time.time() - last_call)
                    if wait > 0:
                        time.sleep(wait)
                    last_call = time.time()
                    df = provider.fetch_adj_factor(
                        ts_code, start_date=symbol_start, end_date=cutoff
                    )
                if df is None or df.empty:
                    if parent_rec:
                        rec.update({
                            "status": "factor_ready",
                            "blob_sha256": parent_rec.blob_sha256,
                            "rows": int(parent_rec.row_count or 0),
                            "first_date": parent_rec.first_date,
                            "last_date": parent_rec.last_date,
                            "window_status": "no_new_rows_parent_retained",
                        })
                    else:
                        rec.update({"status": "no_factor", "rows": 0})
                else:
                    df = df[["trade_date", "adj_factor"]].dropna()
                    df["trade_date"] = df["trade_date"].astype(str).astype(int)
                    df = df[df["trade_date"] <= cutoff]
                    df = df.sort_values("trade_date")
                    before = len(df)
                    df = df.drop_duplicates(subset="trade_date", keep="last")
                    dedup_dropped = before - len(df)
                    bad = df[df["adj_factor"] <= 0]
                    if len(bad) > 0:
                        rec.update({"status": "quality_failed",
                                    "error": f"nonpositive_factor_rows={len(bad)}"})
                    elif len(df) == 0:
                        if parent_rec:
                            rec.update({
                                "status": "factor_ready",
                                "blob_sha256": parent_rec.blob_sha256,
                                "rows": int(parent_rec.row_count or 0),
                                "first_date": parent_rec.first_date,
                                "last_date": parent_rec.last_date,
                                "window_status": "no_new_rows_parent_retained",
                            })
                        else:
                            rec.update({"status": "no_factor", "rows": 0})
                    else:
                        # Raw cache keeps the provider window; the blob
                        # below contains complete merged history.
                        df.to_csv(raw_dir / f"{ts_code}.csv", index=False)
                        if parent_rec:
                            parent_arrays = store.load_bars(
                                parent_rec.blob_sha256
                            )
                            df = _merge_factor_history(parent_arrays, df)
                        sha = store.store_factors(
                            sym, df["trade_date"].to_numpy(),
                            df["adj_factor"].to_numpy())
                        rec.update({
                            "status": "factor_ready", "blob_sha256": sha,
                            "rows": int(len(df)),
                            "first_date": int(df["trade_date"].iloc[0]),
                            "last_date": int(df["trade_date"].iloc[-1]),
                            "dedup_dropped": int(dedup_dropped),
                            "window_start": symbol_start,
                        })
            except RateLimited as e:
                stats["rate_limited"] += 1
                stats["provider_failed"] += 1
                rec.update({"status": "provider_failed", "error": "rate_limited"})
            except ProviderError as e:
                stats["provider_failed"] += 1
                rec.update({"status": "provider_failed", "error": type(e).__name__})
            except Exception as e:
                stats["provider_failed"] += 1
                rec.update({"status": "provider_failed", "error": type(e).__name__})
            if (
                rec.get("status") in ("provider_failed", "quality_failed")
                and parent_rec
            ):
                rec.update({
                    "blob_sha256": parent_rec.blob_sha256,
                    "rows": int(parent_rec.row_count or 0),
                    "first_date": parent_rec.first_date,
                    "last_date": parent_rec.last_date,
                    "parent_history_retained": True,
                })
            done[sym] = rec
            if (len(done)) % 25 == 0 or (i + 1) == len(pending):
                ck["done"] = done
                ck["api_calls"] = stats["api_calls"]
                atomic_write_json(ck_path, ck)
            if (len(done)) % 200 == 0:
                el = time.time() - t0
                print(f"  {len(done)}/{len(symbols)} done | api_calls={stats['api_calls']}"
                      f" | {el:.0f}s | {stats['api_calls']/max(el/60,0.01):.0f}/min", flush=True)

        elapsed = time.time() - t0
        stats["retries_estimated"] = max(0, stats["api_calls"] - len(symbols))

        ready_syms = [r for r in done.values() if r["status"] == "factor_ready"]
        no_factor = [r for r in done.values() if r["status"] == "no_factor"]
        failed = [r for r in done.values()
                  if r["status"] in ("provider_failed", "quality_failed")]

        symbol_records: List[SymbolRecord] = []
        for sym in symbols:
            r = done.get(sym, {})
            st = r.get("status", "provider_failed")
            if st == "factor_ready":
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256=r["blob_sha256"],
                    first_date=r.get("first_date"), last_date=r.get("last_date"),
                    row_count=r.get("rows", 0), quality="ok"))
            elif st == "no_factor":
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256="", quality="no_data", error="no_factor"))
            else:
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256=r.get("blob_sha256", ""),
                    first_date=r.get("first_date"),
                    last_date=r.get("last_date"),
                    row_count=r.get("rows", 0),
                    quality="error", error=r.get("error", st)))

        total_records = sum(r.row_count for r in symbol_records)
        content_hash = _hl.sha256(json.dumps(
            sorted((r.symbol, r.blob_sha256) for r in symbol_records if r.blob_sha256),
        ).encode()).hexdigest()

        canonical_pre = json.dumps(
            {"source": "tushare", "adjustment": "adj_factor", "period": "1d",
             "sync_run_id": sync_run_id, "symbols": symbols}, sort_keys=True)
        dataset_id = make_dataset_id(
            "tushare", "adjfactor", "1d", str(cutoff),
            _hl.sha256(canonical_pre.encode()).hexdigest())

        lasts = [r.last_date for r in symbol_records if r.last_date]
        actual_cutoff = max(lasts) if lasts else 0
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source=DataSource.TUSHARE.value,
            adjustment="adj_factor",
            period=BarPeriod.DAY.value,
            dataset_type="factor",
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=actual_cutoff,
            provider_version=provider.provider_version(),
            sync_run_id=sync_run_id,
            parent_dataset_id=parent.dataset_id if parent else None,
            status="building",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            universe_file=str(universe_path),
            universe_sha256=uni_sha,
            content_hash=content_hash,
            token_exposed=False,
            incremental_policy_version=FACTOR_INCREMENTAL_POLICY_VERSION,
            provenance={
                "api": "tushare pro.adj_factor (per ts_code)",
                "metadata_api": "pro.stock_basic list_status L+D",
                "raw_cache_dir": str(raw_dir),
                "rate_per_min_limit": rate_per_min,
                "api_calls": stats["api_calls"],
                "rate_limited": stats["rate_limited"],
                "retries_estimated": stats["retries_estimated"],
                "sync_kind": sync_kind,
                "requested_cutoff": cutoff,
                "actual_cutoff": actual_cutoff,
                "window_start": window_start,
                "parent_dataset_id": parent.dataset_id if parent else None,
                "parent_history_retained_failures": sum(
                    1 for r in done.values()
                    if r.get("parent_history_retained")
                ),
            },
            expected_symbol_count=len(symbols),
        )
        manifest.symbols = symbol_records
        manifest.symbol_count = len(symbol_records)
        manifest.row_count = total_records
        manifest.imported_symbol_count = len(ready_syms)
        manifest.no_data_symbol_count = len(no_factor)
        manifest.failed_symbol_count = len(failed)
        manifest.coverage_ratio = round(len(ready_syms) / max(len(symbols), 1), 6)
        if failed:
            manifest.status = "partial"
            print(f"  STRICT POLICY -> partial: provider/quality failures={len(failed)}")

        # Factor freshness gate (incremental/merge only): per-symbol factor
        # coverage must track the raw baseline's active stocks — the global
        # max date alone masks local stalls (122/123 stocks stale still
        # looked fresh). No raw baseline / no active symbols -> pass.
        # --skip-freshness-gate disables the demotion (explicit escape hatch
        # for subset-universe deployments) but records the decision in the
        # manifest provenance.
        freshness = None
        if sync_kind == "incremental_parent_merge":
            if getattr(args, "skip_freshness_gate", False):
                manifest.provenance["freshness_gate"] = "skipped_by_flag"
                print("  FACTOR FRESHNESS GATE: skipped by --skip-freshness-gate")
            else:
                freshness = _factor_freshness_metrics(store, manifest)
                if freshness is None:
                    print("  FACTOR FRESHNESS GATE: skipped (no raw baseline / "
                          "no active symbols)")
                else:
                    manifest.provenance["freshness"] = freshness
                    if freshness["fresh_symbol_ratio"] < FRESH_RATIO_MIN:
                        # "blocked" marks the partial as gate-demoted (series
                        # complete, freshness lagging) so the next run may
                        # still use it as the window-continuation parent.
                        freshness["gate"] = "blocked"
                        manifest.status = "partial"
                        print(f"  FACTOR FRESHNESS GATE: fresh="
                              f"{freshness['fresh_count']}/"
                              f"{freshness['active_count']} "
                              f"(ratio={freshness['fresh_symbol_ratio']} < "
                              f"{FRESH_RATIO_MIN}) -> partial")
                        for _s in freshness["stale_active_symbols"][:8]:
                            print(f"    stale {_s['symbol']}: factor="
                                  f"{_s['factor_last_date']} raw="
                                  f"{_s['raw_last_date']}")
                    else:
                        print(f"  FACTOR FRESHNESS GATE: fresh="
                              f"{freshness['fresh_count']}/"
                              f"{freshness['active_count']} "
                              f"(ratio={freshness['fresh_symbol_ratio']})")
        store.publish(manifest)
        # After a full ready factor publish, demote smaller same-family ready
        # shells so resolve_latest_ready / UI freshness always prefer this set.
        if (manifest.status or "") == "ready" and int(manifest.symbol_count or 0) >= 1000:
            try:
                from wtpy.apps.astock.data.repository import MarketDataRepository
                _repo = MarketDataRepository(store)
                _demoted = _repo.supersede_dominated_ready(manifest)
                if _demoted:
                    print(f"  superseded dominated factor sets: {len(_demoted)}")
                    for _did in _demoted[:8]:
                        print(f"    - {_did}")
            except Exception as _se:
                print(f"  WARN: supersede dominated factors failed: {_se}")
        try:
            ck_path.unlink(missing_ok=True)
        except Exception:
            pass

        # full/rebuild batches accumulate as tsfactor_{run_id} dirs; prune the
        # oldest once a run publishes cleanly (the "latest" cache is never
        # touched)
        keep_raw = int(getattr(args, "keep_raw_batches", None) or 0)
        if keep_raw > 0 and (manifest.status or "") in ("ready", "success"):
            _prune_raw_batches(factor_raw_root, keep_raw)

        # symbol coverage / mapping CSV
        cov_out = getattr(args, "coverage_out", None)
        if cov_out:
            import csv as _csv
            uni_rows = {r["canonical_symbol"]: r for r in _csv.DictReader(
                open(universe_path, encoding="utf-8-sig"))}
            with open(cov_out, "w", newline="", encoding="utf-8-sig") as f:
                w = _csv.writer(f)
                w.writerow(["canonical_symbol", "vendor_symbol", "ts_code",
                            "exchange", "board", "instrument_type", "list_status",
                            "list_date", "delist_date", "raw_available",
                            "factor_available", "factor_rows", "inclusion_status",
                            "exclusion_reason"])
                for sym in symbols:
                    u = uni_rows.get(sym, {})
                    mm = meta.get(sym, {})
                    r = done.get(sym, {})
                    st = r.get("status", "")
                    factor_ok = st == "factor_ready"
                    if factor_ok:
                        incl, reason = "included", ""
                    elif st == "no_factor":
                        expected_missing = mm.get("list_status") == "" or u.get("board") == "bse"
                        incl = "excluded"
                        reason = "no_factor_expected" if expected_missing else "no_factor_unexpected"
                    else:
                        incl, reason = "excluded", st
                    w.writerow([sym, u.get("symbol", ""), r.get("ts_code", ""),
                                u.get("exchange", ""), u.get("board", ""),
                                u.get("instrument_type", ""),
                                mm.get("list_status", "NOT_IN_STOCK_BASIC"),
                                mm.get("list_date", ""), mm.get("delist_date", ""),
                                True, factor_ok, r.get("rows", 0), incl, reason])
            print(f"  coverage CSV -> {cov_out}")

        log_payload = {
            "sync_run_id": sync_run_id, "dataset_id": dataset_id,
            "source": "tushare", "adjustment": "adj_factor",
            "parent_dataset_id": parent.dataset_id if parent else None,
            "sync_kind": sync_kind, "window_start": window_start,
            "requested_cutoff": cutoff, "actual_cutoff": actual_cutoff,
            "universe_sha256": uni_sha,
            "stats": {**stats, "elapsed_seconds": round(elapsed, 1),
                      "calls_per_min": round(stats["api_calls"] / max(elapsed / 60, 0.01), 1)},
            "result": {"factor_ready": len(ready_syms), "no_factor": len(no_factor),
                       "failed": len(failed), "records": total_records,
                       "status": manifest.status},
        }
        if freshness is not None:
            _fresh_log = dict(freshness)
            if "gate" not in _fresh_log:
                _fresh_log["gate"] = "passed"
                _fresh_log["reason"] = "freshness_ratio_ok"
            else:
                _fresh_log["reason"] = "freshness_below_threshold"
            log_payload["freshness"] = _fresh_log
            log_payload["freshness_gate"] = _fresh_log["gate"]
        elif (manifest.provenance or {}).get("freshness_gate"):
            log_payload["freshness_gate"] = (
                manifest.provenance or {}).get("freshness_gate")
        else:
            log_payload["freshness_gate"] = "skipped"
        store.save_sync_log(sync_run_id, log_payload)
        for extra in (getattr(args, "log_path", None), getattr(args, "report_path", None)):
            if extra:
                try:
                    Path(extra).parent.mkdir(parents=True, exist_ok=True)
                    Path(extra).write_text(json.dumps(log_payload, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
                except Exception:
                    pass

        print(f"  adj_factor: {len(ready_syms)}/{len(symbols)} factor_ready, "
              f"no_factor={len(no_factor)}, failed={len(failed)}")
        print(f"  dataset={dataset_id} status={manifest.status} records={total_records}"
              f" elapsed={elapsed:.0f}s api_calls={stats['api_calls']}")
        _result = {"status": "success", "sync_run_id": sync_run_id,
                   "dataset_id": dataset_id, "dataset_status": manifest.status,
                   "stats": log_payload["stats"], "result": log_payload["result"]}
        if freshness is not None:
            _result["freshness"] = freshness
        if (manifest.status or "") == "ready":
            _result["reconcile"] = _reconcile_after_sync(store)
            _apply_reconcile_status(_result)
        else:
            # Fail-closed: a factor manifest demoted by the freshness gate
            # (or partial via provider failures) must not touch the formal
            # L1/L2 product surfaces — the reconcile is skipped entirely.
            _result["reconcile"] = {
                "status": "skipped",
                "reason": "factor_not_ready",
                "dataset_status": manifest.status,
            }
        return _result
    finally:
        lock.release()


def derive_tushare_factor_qfq(args, store: DatasetStore) -> dict:
    """Derive an immutable internal/tushare_factor_qfq dataset from
    local_vendor raw bars × tushare adj_factor.

    anchor_factor = last factor on or before cutoff (per symbol)
    ratio(t) = adj_factor_asof(t) / anchor_factor      (never uses future factors)
    qfq price = raw price × ratio(t), round-half-even to 4dp
    volume/amount copied unchanged from raw.
    """
    import hashlib as _hl
    import numpy as np
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError

    raw_id = getattr(args, "raw_dataset_id", None)
    fac_id = getattr(args, "factor_dataset_id", None)
    if not raw_id or not fac_id:
        print("ERROR: derive requires --raw-dataset-id and --factor-dataset-id")
        return {"status": "failed", "error": "missing_parent_dataset_ids"}

    raw_m = store.load_manifest(raw_id)
    fac_m = store.load_manifest(fac_id)
    if raw_m is None or fac_m is None:
        print("ERROR: parent manifest not found")
        return {"status": "failed", "error": "parent_manifest_not_found"}
    if raw_m.status != "ready" or fac_m.status not in ("ready", "partial"):
        print(f"ERROR: parents must be ready (raw={raw_m.status}, factor={fac_m.status})")
        return {"status": "failed", "error": "parent_not_ready"}
    if getattr(args, "require_factor_ready", True) and fac_m.status != "ready":
        print(f"ERROR: factor dataset status={fac_m.status}; only ready factor "
              f"datasets may be used (pass --allow-partial-factor to override "
              f"for diagnostics only)")
        return {"status": "failed", "error": "factor_not_ready"}
    if fac_m.dataset_type != "factor":
        print("ERROR: factor_dataset_id does not point at a factor dataset")
        return {"status": "failed", "error": "not_a_factor_dataset"}
    if raw_m.source != "local_vendor" or raw_m.adjustment != "none":
        print("ERROR: raw parent must be local_vendor/none")
        return {"status": "failed", "error": "raw_parent_wrong_source"}

    raw_manifest_sha = _hl.sha256(
        (store.manifests_dir / f"{raw_id}.json").read_bytes()).hexdigest()
    fac_manifest_sha = _hl.sha256(
        (store.manifests_dir / f"{fac_id}.json").read_bytes()).hexdigest()

    raw_ok = {r.symbol: r for r in raw_m.symbols if r.quality == "ok"}
    fac_ok = {r.symbol: r for r in fac_m.symbols if r.quality == "ok"}
    eligible = sorted(set(raw_ok) & set(fac_ok))
    excluded = []
    for s in sorted(set(raw_ok) - set(fac_ok)):
        fr = next((x for x in fac_m.symbols if x.symbol == s), None)
        reason = "no_factor" if (fr and fr.quality == "no_data") else "factor_failed"
        excluded.append({"symbol": s, "reason": reason})
    for s in sorted(set(fac_ok) - set(raw_ok)):
        excluded.append({"symbol": s, "reason": "raw_missing"})

    lasts = [r.last_date for r in raw_ok.values() if r.last_date]
    cutoff = int(getattr(args, "cutoff", None) or (max(lasts) if lasts else 0))
    if not cutoff:
        print("ERROR: cannot determine cutoff")
        return {"status": "failed", "error": "no_cutoff"}

    sync_run_id = make_sync_run_id("tsqfq")
    lock = SyncTaskLock(store.root, source="internal",
                        adjustment="tushare_factor_qfq", period="1d",
                        sync_run_id=sync_run_id)
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return {"status": "failed", "error": "concurrent_lock"}

    try:
        t0 = time.time()
        print(f"Deriving tushare_factor_qfq: {len(eligible)} symbols "
              f"(raw={raw_id}, factor={fac_id}, cutoff={cutoff}, "
              f"formula={QFQ_FORMULA_VERSION})")
        records: List[SymbolRecord] = []
        issues: List[dict] = []
        total_rows = 0
        imported = 0
        for i, sym in enumerate(eligible):
            rr = raw_ok[sym]
            fr = fac_ok[sym]
            raw_arr = store.load_bars(rr.blob_sha256)
            fac_arr = store.load_bars(fr.blob_sha256)
            rd = raw_arr["trade_date"]
            fd = fac_arr["trade_date"]
            fv = fac_arr["adj_factor"]
            # anchor: last factor on or before cutoff
            aidx = int(np.searchsorted(fd, cutoff, side="right")) - 1
            if aidx < 0:
                records.append(SymbolRecord(symbol=sym, blob_sha256="",
                                            quality="error", error="no_anchor_factor"))
                issues.append({"symbol": sym, "issue": "no_anchor_factor"})
                continue
            anchor = float(fv[aidx])
            # asof alignment: factor effective at each raw date (past only)
            pos = np.searchsorted(fd, rd, side="right") - 1
            valid = pos >= 0
            leading_gap = int(np.sum(~valid))
            if leading_gap:
                issues.append({"symbol": sym, "issue": "leading_gap_rows_dropped",
                               "detail": leading_gap})
            rdv = rd[valid]
            ratio = fv[pos[valid]] / anchor
            def _r4(a):
                return np.round(a, 4)
            arrays = {
                "trade_date": rdv,
                "open": _r4(raw_arr["open"][valid] * ratio),
                "high": _r4(raw_arr["high"][valid] * ratio),
                "low": _r4(raw_arr["low"][valid] * ratio),
                "close": _r4(raw_arr["close"][valid] * ratio),
                "volume": raw_arr["volume"][valid],
                "amount": raw_arr["amount"][valid],
            }
            if len(rdv) == 0:
                records.append(SymbolRecord(symbol=sym, blob_sha256="",
                                            quality="no_data", error="all_rows_leading_gap"))
                continue
            # inherited raw OHLC bound anomalies (close outside range etc.)
            o, h, l, c = arrays["open"], arrays["high"], arrays["low"], arrays["close"]
            bad = int(np.sum((h < l) | (o > h) | (o < l) | (c > h) | (c < l)))
            if bad:
                issues.append({"symbol": sym, "issue": "ohlc_bounds_inherited_from_raw",
                               "detail": bad})
            sha = store.store_bar_arrays(sym, arrays)
            total_rows += len(rdv)
            imported += 1
            records.append(SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=int(rdv[0]),
                last_date=int(rdv[-1]), row_count=len(rdv), quality="ok"))
            if (i + 1) % 1000 == 0:
                print(f"  {i+1}/{len(eligible)} ({time.time()-t0:.0f}s)", flush=True)

        failed = [r for r in records if r.quality == "error"]
        content_hash = _hl.sha256(json.dumps(
            sorted((r.symbol, r.blob_sha256) for r in records if r.blob_sha256),
        ).encode()).hexdigest()
        canonical_pre = json.dumps(
            {"source": "internal", "adjustment": "tushare_factor_qfq", "period": "1d",
             "raw": raw_id, "factor": fac_id, "cutoff": cutoff,
             "formula": QFQ_FORMULA_VERSION, "sync_run_id": sync_run_id},
            sort_keys=True)
        dataset_id = make_dataset_id(
            "internal", "tsfqfq", "1d", str(cutoff),
            _hl.sha256(canonical_pre.encode()).hexdigest())

        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source="internal",
            adjustment="tushare_factor_qfq",
            period="1d",
            dataset_type="bars",
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=cutoff,
            provider_version=f"derive_{QFQ_FORMULA_VERSION}",
            sync_run_id=sync_run_id,
            parent_dataset_id=raw_id,
            status="building",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw_dataset_id=raw_id,
            raw_dataset_sha256=raw_manifest_sha,
            raw_source="local_vendor",
            factor_dataset_id=fac_id,
            factor_dataset_sha256=fac_manifest_sha,
            factor_source="tushare",
            anchor_policy=QFQ_ANCHOR_POLICY,
            formula_version=QFQ_FORMULA_VERSION,
            price_precision_policy=QFQ_PRICE_PRECISION_POLICY,
            volume_policy="copied_from_raw_shares_no_adjustment",
            amount_policy="copied_from_raw_cny_no_adjustment",
            universe_file=raw_m.universe_file or fac_m.universe_file,
            universe_sha256=raw_m.universe_sha256 or fac_m.universe_sha256,
            content_hash=content_hash,
            survivorship_bias=True,
            historical_universe_complete=False,
            delisted_coverage_complete=False,
            coverage_start_year=raw_m.coverage_start_year,
            coverage_end_year=raw_m.coverage_end_year,
            known_missing_delisted_count=raw_m.known_missing_delisted_count,
            known_missing_delisted_symbols=list(raw_m.known_missing_delisted_symbols),
            warning_text=raw_m.warning_text or SURVIVORSHIP_WARNING_TEXT,
            recommended_use=["L1 signal computation (front-adjusted)"],
            prohibited_or_discouraged_use=[
                "L2 execution prices", "limit up/down checks",
                "claiming Tushare-native qfq data",
            ],
            provenance={
                "derivation": "local_vendor raw OHLC × (adj_factor_asof/anchor)",
                "anchor_policy": QFQ_ANCHOR_POLICY,
                "leading_gap_policy": "rows before first factor date are dropped and recorded",
                "pre_close_policy": "bar schema has no pre_close column; derive downstream as prior qfq close",
            },
            expected_symbol_count=len(eligible),
        )
        manifest.symbols = records
        manifest.symbol_count = len(records)
        manifest.row_count = total_rows
        manifest.imported_symbol_count = imported
        manifest.excluded_symbol_count = len(excluded)
        manifest.no_data_symbol_count = sum(1 for r in records if r.quality == "no_data")
        manifest.failed_symbol_count = len(failed)
        manifest.coverage_ratio = round(imported / max(len(eligible), 1), 6)
        if failed or manifest.no_data_symbol_count:
            manifest.status = "partial"
            print(f"  STRICT POLICY -> partial (failed={len(failed)}, "
                  f"no_data={manifest.no_data_symbol_count})")
        store.publish(manifest)

        elapsed = time.time() - t0
        log_payload = {
            "sync_run_id": sync_run_id, "dataset_id": dataset_id,
            "raw_dataset_id": raw_id, "factor_dataset_id": fac_id,
            "cutoff": cutoff, "formula_version": QFQ_FORMULA_VERSION,
            "anchor_policy": QFQ_ANCHOR_POLICY,
            "result": {"eligible": len(eligible), "imported": imported,
                       "excluded": len(excluded), "failed": len(failed),
                       "rows": total_rows, "status": manifest.status,
                       "elapsed_seconds": round(elapsed, 1)},
            "excluded_sample": excluded[:50],
            "issues_sample": issues[:50],
        }
        store.save_sync_log(sync_run_id, log_payload)
        for extra in (getattr(args, "log_path", None), getattr(args, "report_path", None)):
            if extra:
                try:
                    Path(extra).parent.mkdir(parents=True, exist_ok=True)
                    Path(extra).write_text(json.dumps(log_payload, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
                except Exception:
                    pass
        print(f"  derived: {imported}/{len(eligible)} ok, excluded={len(excluded)}, "
              f"rows={total_rows}, dataset={dataset_id} status={manifest.status} "
              f"({elapsed:.0f}s)")
        return {"status": "success", "sync_run_id": sync_run_id,
                "dataset_id": dataset_id, "dataset_status": manifest.status,
                "result": log_payload["result"], "issues": issues}
    finally:
        lock.release()


def derive_composite_tushare_factor_qfq(args, store: DatasetStore) -> dict:
    """Gate B6: derive internal/composite_tushare_factor_qfq from
    internal/composite_none raw bars x tushare adj_factor parents.

    The derivation implementation lives in
    ``wtpy.apps.astock.data.tushare_product.derive_composite_tushare_factor_qfq``
    so the CLI, the Tushare product reconcile pipeline and the API share one
    code path (same math, same deterministic dataset id, same lineage
    metadata). This wrapper keeps the legacy CLI contract: parent auto-resolve,
    SyncTaskLock, sync log and the legacy return shape.
    """
    import hashlib as _hl
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError
    from wtpy.apps.astock.data.tushare_product import (
        derive_composite_tushare_factor_qfq as _derive_composite_tushare_factor_qfq,
    )

    raw_id = getattr(args, "raw_dataset_id", None)
    fac_id = getattr(args, "factor_dataset_id", None)
    sup_id = getattr(args, "supplement_factor_dataset_id", None)
    uni_id = getattr(args, "universe_dataset_id_arg", None)
    if not raw_id or not fac_id:
        print("ERROR: derive requires --raw-dataset-id and --factor-dataset-id")
        return {"status": "failed", "error": "missing_parent_dataset_ids"}

    sync_run_id = make_sync_run_id("ctsfqfq")
    lock = SyncTaskLock(store.root, source="internal",
                        adjustment="composite_tushare_factor_qfq", period="1d",
                        sync_run_id=sync_run_id)
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return {"status": "failed", "error": "concurrent_lock"}

    try:
        r = _derive_composite_tushare_factor_qfq(
            store,
            raw_dataset_id=raw_id,
            factor_dataset_id=fac_id,
            supplement_factor_dataset_id=sup_id,
            universe_dataset_id=uni_id,
            cutoff=getattr(args, "cutoff", None),
        )
        if r.get("status") != "success":
            print(f"ERROR: {r.get('error', 'derive failed')}")
            return r

        dataset_id = r["dataset_id"]
        result = r["result"]
        issues = r["issues"]
        log_payload = {
            "sync_run_id": sync_run_id, "dataset_id": dataset_id,
            "raw_dataset_id": raw_id, "factor_dataset_id": fac_id,
            "supplement_factor_dataset_id": sup_id or "",
            "universe_dataset_id": uni_id or "",
            "cutoff": result.get("cutoff"),
            "formula_version": COMPOSITE_QFQ_FORMULA_VERSION,
            "factor_resolution_rule": FACTOR_RESOLUTION_RULE_VERSION,
            "anchor_policy": QFQ_ANCHOR_POLICY,
            "result": result,
            "issues_sample": issues[:50],
        }
        store.save_sync_log(sync_run_id, log_payload)
        for extra in (getattr(args, "log_path", None), getattr(args, "report_path", None)):
            if extra:
                try:
                    Path(extra).parent.mkdir(parents=True, exist_ok=True)
                    Path(extra).write_text(
                        json.dumps(log_payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                except Exception:
                    pass
        print(f"  derived: {result.get('imported', 0)}/{result.get('eligible', 0)} ok, "
              f"missing_factor={result.get('missing_factor', 0)}, rows={result.get('rows', 0)}, "
              f"dataset={dataset_id} status={r.get('dataset_status')}")
        return {
            "status": "success", "sync_run_id": sync_run_id,
            "dataset_id": dataset_id, "dataset_status": r.get("dataset_status"),
            "result": result, "issues": issues,
        }
    finally:
        lock.release()
TDX_CHECKPOINT_VERSION = "tdx_ck_v1"
TDX_FRONT_INCREMENTAL_POLICY_VERSION = "tdx_front_inc_v1"
TDX_MAX_SINGLE_RETRIES = 2
TDX_FRONT_ANCHOR_POLICY = "front_anchored_at_latest_bar_on_or_before_request_end"
TDX_AMOUNT_POLICY = "tdx_amount_wan_yuan_scaled_x10000_to_yuan"
TDX_VOLUME_POLICY = "tdx_volume_shares_as_returned_unadjusted"
TDX_EST_SEC_PER_SYMBOL = 0.2  # measured 2026-07-26 (full-history single fetch)


def _to_tdx_code(canonical: str) -> str:
    """SSE.STK.600000 -> 600000.SH (tqcenter request format)."""
    parts = canonical.split(".")
    if len(parts) == 3:
        exch, _, code = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exch)
        if suffix:
            return f"{code}.{suffix}"
    return canonical


def _load_universe_rows(path: Path) -> List[dict]:
    import csv as _csv
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in _csv.DictReader(f)]


def _tdx_partition_universe(rows: List[dict]):
    """Partition included universe rows into TdxQuant (eligible, excluded).

    General metadata-driven rules only — no per-symbol hardcoding:
      - rows absent from the latest vendor year on a BSE legacy code segment
        were migrated to the 920 segment (their 920 row is a separate
        candidate) -> excluded, syncing them would double-count;
      - other rows absent from the latest vendor year are delisted/absent
        securities; TdxQuant serves no delisted data (live-verified) ->
        excluded with explicit reason.
    """
    eligible: List[tuple] = []
    excluded: List[tuple] = []
    for r in rows:
        status = (r.get("inclusion_status") or "included").strip().lower()
        if status != "included":
            continue
        sym = (r.get("canonical_symbol") or r.get("symbol") or "").strip()
        if not sym:
            continue
        present = (r.get("present_in_latest_year") or "").strip().lower() == "true"
        if present:
            eligible.append((sym, r))
            continue
        code = sym.split(".")[-1]
        if (r.get("exchange") or "").strip().upper() == "BSE" and not code.startswith("92"):
            reason = "bse_legacy_code_migrated_to_920_segment"
        else:
            reason = "absent_latest_vendor_year_delisted_no_provider_data"
        excluded.append((sym, r, reason))
    return eligible, excluded


def _tdx_checkpoint_path(store: DatasetStore) -> Path:
    return store.sync_logs_dir / "checkpoint_tdxquant_front_1d.json"


def _validate_symbol_bars(bars, *, require_positive: bool = True) -> Optional[str]:
    """Ingest-time quality gate. Returns an error string or None if clean.

    require_positive=False for TDX front-adjusted bars: the client's affine
    forward adjustment legitimately produces <=0 early prices on long-history
    high-dividend stocks (client displays the same values). Ordering
    invariants (high>=low, open/close within range) still hold because the
    affine transform is monotone.
    """
    import numpy as _np
    d = _np.array([b.trade_date for b in bars], dtype=_np.int64)
    if len(d) == 0:
        return "empty"
    diffs = _np.diff(d)
    if _np.any(diffs == 0):
        return "duplicate_trade_date"
    if _np.any(diffs < 0):
        return "dates_not_ascending"
    o = _np.array([b.open for b in bars])
    h = _np.array([b.high for b in bars])
    l = _np.array([b.low for b in bars])
    c = _np.array([b.close for b in bars])
    if _np.any(_np.isnan(o) | _np.isnan(h) | _np.isnan(l) | _np.isnan(c)):
        return "nan_price"
    if require_positive and not (
        _np.all(o > 0) and _np.all(h > 0) and _np.all(l > 0) and _np.all(c > 0)
    ):
        return "nonpositive_price"
    if _np.any(h < l):
        return "high_below_low"
    if _np.any((o < l) | (o > h)) or _np.any((c < l) | (c > h)):
        return "open_close_outside_range"
    return None


def sync_tdxquant_front_full(args, store: DatasetStore) -> dict:
    """Production full sync of TDX-native front-adjusted daily bars.

    Gate C phase 2. Requires the TDX client online. Uses:
      - SyncTaskLock scope (market_data_root, tdxquant, front, 1d);
      - per-batch checkpoint with --resume (same sync_run_id, universe/root
        validated);
      - batch fetch (10-20) with single-symbol fallback + bounded retries;
      - strict ready policy (failed==0, no unexpected no_data);
      - immutable manifest with full provenance.
    """
    import hashlib as _hl
    from wtpy.apps.astock.data.dataset_store import evaluate_strict_publish
    from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
    from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError
    from wtpy.apps.astock.data.io_util import atomic_write_json

    if (args.adjustment or "front") != "front":
        print("ERROR: this path syncs adjustment=front only")
        return {"status": "failed", "error": "adjustment_must_be_front"}
    if (args.period or "1d") != "1d":
        print("ERROR: this path syncs period=1d only")
        return {"status": "failed", "error": "period_must_be_1d"}
    if not getattr(args, "universe_file", None):
        print("ERROR: --universe-file (frozen vendor universe CSV) is required")
        return {"status": "failed", "error": "universe_file_required"}

    universe_path = Path(args.universe_file)
    rows = _load_universe_rows(universe_path)
    eligible, excluded = _tdx_partition_universe(rows)
    if not eligible:
        print("ERROR: universe has no eligible symbols")
        return {"status": "failed", "error": "empty_universe"}
    symbols = sorted(sym for sym, _ in eligible)
    uni_sha = _hl.sha256(universe_path.read_bytes()).hexdigest()
    universe_hash = _hl.sha256(",".join(symbols).encode()).hexdigest()
    allowlist = _load_allowlist_file(getattr(args, "allow_no_data_file", None))

    batch_size = max(1, min(int(args.batch_size or 10), 50))
    batch_pause = max(0.0, float(getattr(args, "batch_pause", 0.05) or 0.0))
    cutoff = int(getattr(args, "end_date", None) or time.strftime("%Y%m%d"))

    sync_run_id = make_sync_run_id("tdxfront")
    lock = SyncTaskLock(store.root, source="tdxquant", adjustment="front",
                        period="1d", sync_run_id=sync_run_id)
    try:
        lock.acquire()
    except SyncLockHeldError as e:
        print(f"ERROR: {e}")
        return {"status": "failed", "error": "concurrent_lock", "holder": e.holder}
    if lock.recovered_stale:
        print(f"NOTE: recovered stale lock from pid={lock.recovered_stale.get('pid')}")

    try:
        # checkpoint / resume (validated: version, universe, data root)
        ck_path = _tdx_checkpoint_path(store)
        ck = None
        if ck_path.exists():
            try:
                ck = json.loads(ck_path.read_text(encoding="utf-8"))
            except Exception:
                ck = None
        if ck and not getattr(args, "resume", False) and not getattr(args, "fresh", False):
            print("ERROR: tdxquant front checkpoint exists. Use --resume or --fresh.")
            return {"status": "failed", "error": "checkpoint_exists_use_resume_or_fresh"}
        if getattr(args, "resume", False):
            if not ck:
                print("ERROR: no tdxquant front checkpoint to resume")
                return {"status": "failed", "error": "checkpoint_missing"}
            if ck.get("checkpoint_version") != TDX_CHECKPOINT_VERSION:
                print("ERROR: checkpoint version mismatch")
                return {"status": "failed", "error": "checkpoint_version_mismatch"}
            if ck.get("universe_hash") != universe_hash:
                print("ERROR: universe changed since checkpoint — refusing resume")
                return {"status": "failed", "error": "checkpoint_universe_mismatch"}
            if ck.get("market_data_root") != str(store.root):
                print("ERROR: MARKET_DATA_ROOT changed since checkpoint — refusing resume")
                return {"status": "failed", "error": "checkpoint_root_mismatch"}
            sync_run_id = ck["sync_run_id"]
            lock.sync_run_id = sync_run_id
            done: Dict[str, dict] = ck.get("done", {})
            stats = dict(ck.get("stats") or {})
            print(f"  Resuming sync_run_id={sync_run_id}: "
                  f"{len(done)}/{len(symbols)} symbols already done")
        else:
            if getattr(args, "fresh", False) and ck_path.exists():
                ck_path.unlink()
            done = {}
            stats = {}
            ck = {"checkpoint_version": TDX_CHECKPOINT_VERSION,
                  "sync_run_id": sync_run_id,
                  "universe_hash": universe_hash,
                  "universe_sha256": uni_sha,
                  "market_data_root": str(store.root),
                  "batch_size": batch_size,
                  "eligible_count": len(symbols),
                  "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "done": done}
        stats.setdefault("provider_calls", 0)
        stats.setdefault("retries", 0)
        stats.setdefault("batch_fallbacks", 0)

        provider = TdxQuantProvider(tdx_root=args.tdx_root, batch_size=batch_size)
        if not provider.health_check():
            print("ERROR: TdxQuant client is not available (not running or not logged in)")
            return {"status": "failed", "error": "client_unavailable"}
        tqcenter_ver = provider.tqcenter_version()

        # count every tqcenter market-data call (provider must be idle outside sync)
        _real_gmd = provider._tq.get_market_data

        def _counting_gmd(*a, **kw):
            stats["provider_calls"] += 1
            return _real_gmd(*a, **kw)

        provider._tq.get_market_data = _counting_gmd

        pending = [s for s in symbols if s not in done]
        batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
        print(f"Syncing tdxquant/front/1d: {len(pending)} pending of "
              f"{len(symbols)} eligible ({len(excluded)} pre-excluded), "
              f"batch_size={batch_size}, cutoff={cutoff}, "
              f"tqcenter={tqcenter_ver}, sync_run_id={sync_run_id}")

        t0 = time.time()

        def _ingest(canon_sym: str, sym_bars) -> dict:
            if not sym_bars:
                return {"status": "no_data"}
            err = _validate_symbol_bars(sym_bars, require_positive=False)
            if err:
                return {"status": "failed", "error": f"quality_{err}"}
            sha = store.store_bars(canon_sym, sym_bars)
            return {"status": "ok", "blob_sha256": sha,
                    "rows": len(sym_bars),
                    "first_date": sym_bars[0].trade_date,
                    "last_date": sym_bars[-1].trade_date}

        def _fetch_single_with_retry(canon_sym: str) -> dict:
            last_err = None
            for attempt in range(1 + TDX_MAX_SINGLE_RETRIES):
                if attempt:
                    stats["retries"] += 1
                    time.sleep(0.5 * attempt)
                try:
                    req = MarketDataRequest(
                        symbols=[_to_tdx_code(canon_sym)],
                        period=BarPeriod.DAY, adjustment=AdjustmentMode.FRONT,
                        end_date=cutoff)
                    return _ingest(canon_sym, provider.fetch_bars(req))
                except ProviderError as e:
                    last_err = e
                except Exception as e:  # tqcenter internals can raise raw errors
                    last_err = e
            return {"status": "failed",
                    "error": f"{type(last_err).__name__}: {str(last_err)[:160]}"}

        for bi, batch in enumerate(batches):
            batch_done: Dict[str, dict] = {}
            try:
                req = MarketDataRequest(
                    symbols=[_to_tdx_code(s) for s in batch],
                    period=BarPeriod.DAY, adjustment=AdjustmentMode.FRONT,
                    end_date=cutoff)
                bars = provider.fetch_bars(req)
                by_symbol: Dict[str, list] = defaultdict(list)
                for b in bars:
                    by_symbol[_normalize_symbol(b.symbol)].append(b)
                for s in batch:
                    batch_done[s] = _ingest(s, by_symbol.get(s, []))
            except ProviderError:
                stats["batch_fallbacks"] += 1
                for s in batch:
                    batch_done[s] = _fetch_single_with_retry(s)
            except Exception:
                stats["batch_fallbacks"] += 1
                for s in batch:
                    batch_done[s] = _fetch_single_with_retry(s)

            done.update(batch_done)
            ck["done"] = done
            ck["stats"] = stats
            ck["current_batch_index"] = bi + 1
            ck["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            atomic_write_json(ck_path, ck)
            if (bi + 1) % 20 == 0 or (bi + 1) == len(batches):
                el = time.time() - t0
                n_ok = sum(1 for r in done.values() if r["status"] == "ok")
                print(f"  batch {bi + 1}/{len(batches)} | done={len(done)}"
                      f"/{len(symbols)} ok={n_ok} | calls={stats['provider_calls']}"
                      f" | {el:.0f}s", flush=True)
            if batch_pause:
                time.sleep(batch_pause)

        elapsed = time.time() - t0

        symbol_records: List[SymbolRecord] = []
        for sym in symbols:
            r = done.get(sym, {"status": "failed", "error": "not_attempted"})
            st = r.get("status")
            if st == "ok":
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256=r["blob_sha256"],
                    first_date=r.get("first_date"), last_date=r.get("last_date"),
                    row_count=r.get("rows", 0), quality="ok"))
            elif st == "no_data":
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256="", quality="no_data",
                    error="tdx_no_data"))
            else:
                symbol_records.append(SymbolRecord(
                    symbol=sym, blob_sha256="", quality="error",
                    error=r.get("error", "failed")))

        verdict = evaluate_strict_publish(
            symbol_records,
            expected_symbol_count=len(symbols),
            excluded_symbol_count=len(excluded),
            no_data_allowlist=allowlist,
            max_allow_count=int(getattr(args, "max_no_data_count", 0) or 0),
            max_allow_ratio=float(getattr(args, "max_no_data_ratio", 0.0) or 0.0),
        )

        total_rows = sum(r.row_count for r in symbol_records)
        firsts = [r.first_date for r in symbol_records if r.first_date]
        lasts = [r.last_date for r in symbol_records if r.last_date]
        content_hash = _hl.sha256(json.dumps(
            sorted((r.symbol, r.blob_sha256) for r in symbol_records if r.blob_sha256),
        ).encode()).hexdigest()
        canonical_pre = json.dumps(
            {"source": "tdxquant", "adjustment": "front", "period": "1d",
             "sync_run_id": sync_run_id, "symbols": symbols}, sort_keys=True)
        dataset_id = make_dataset_id(
            "tdxquant", "front", "1d", str(cutoff),
            _hl.sha256(canonical_pre.encode()).hexdigest())

        excluded_delisted = [s for s, _, rr in excluded
                             if rr.startswith("absent_latest_vendor_year")]
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source=DataSource.TDXQUANT.value,
            adjustment=AdjustmentMode.FRONT.value,
            period=BarPeriod.DAY.value,
            dataset_type="bars",
            weekly_bar_mode="local_aggregate",
            anchor_date=max(lasts) if lasts else None,
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=cutoff,
            provider_version=provider.provider_version(),
            sync_run_id=sync_run_id,
            status="building",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            universe_type="dynamic_vendor_union_filtered_by_provider_support",
            universe_definition_version=UNIVERSE_DEFINITION_VERSION,
            survivorship_bias=True,
            historical_universe_complete=False,
            delisted_coverage_complete=False,
            coverage_start_year=int(str(min(firsts))[:4]) if firsts else None,
            coverage_end_year=int(str(max(lasts))[:4]) if lasts else None,
            known_missing_delisted_count=len(KNOWN_MISSING_DELISTED_EVIDENCE)
            + len(excluded_delisted),
            known_missing_delisted_symbols=sorted(
                set(KNOWN_MISSING_DELISTED_EVIDENCE) | set(excluded_delisted))[:50],
            warning_text=SURVIVORSHIP_WARNING_TEXT
            + " TdxQuant serves listed securities only: delisted stocks and "
              "pre-migration BSE legacy codes return no data (live-verified).",
            recommended_use=["front-adjusted signal generation (L1)"],
            prohibited_or_discouraged_use=[
                "execution fills / limit checks (use raw L2 dataset)",
                "survivorship-sensitive studies",
                "ratio/return signals across <=0 front-adjusted price segments "
                "(native TDX affine semantics on long-history dividend stocks)"],
            universe_file=str(universe_path),
            universe_sha256=uni_sha,
            content_hash=content_hash,
            provider_versions={"tdxquant": provider.provider_version(),
                               "tqcenter": tqcenter_ver},
            incremental_policy_version=TDX_FRONT_INCREMENTAL_POLICY_VERSION,
            anchor_policy=TDX_FRONT_ANCHOR_POLICY,
            price_precision_policy="tdx_native_2dp_as_returned; affine front "
                                   "adjustment => early prices of long-history "
                                   "dividend stocks may be <= 0 (client-identical)",
            volume_policy=TDX_VOLUME_POLICY,
            amount_policy=TDX_AMOUNT_POLICY,
            provenance={
                "api": "tqcenter tq.get_market_data (GetHISDATsInStr, per-symbol DLL calls)",
                "dividend_type": "front",
                "fill_data": False,
                "front_anchor_semantics": "anchored at latest bar <= request end_time; "
                                          "request end_time set to cutoff",
                "adjustment_model": "TDX client affine forward adjustment (a*p+b), "
                                    "prices as returned — never recomputed locally",
                "amount_unit_scale": "x10000 (tqcenter returns 万元; stored as 元)",
                "batch_size": batch_size,
                "retry_policy": f"batch->singles fallback, "
                                f"{TDX_MAX_SINGLE_RETRIES} single retries, backoff 0.5s*n",
                "checkpoint_version": TDX_CHECKPOINT_VERSION,
                "provider_calls": stats["provider_calls"],
                "retries": stats["retries"],
                "batch_fallbacks": stats["batch_fallbacks"],
                "provider_called_only_during_sync": True,
                "silent_fallback": False,
                "tdx_client_root": str(args.tdx_root),
            },
        )
        manifest.symbols = symbol_records
        manifest.symbol_count = len(symbol_records)
        manifest.row_count = total_rows
        manifest.expected_symbol_count = verdict["expected_symbol_count"]
        manifest.imported_symbol_count = verdict["imported_symbol_count"]
        manifest.excluded_symbol_count = verdict["excluded_symbol_count"]
        manifest.no_data_symbol_count = verdict["no_data_symbol_count"]
        manifest.failed_symbol_count = verdict["failed_symbol_count"]
        manifest.warning_symbol_count = verdict["warning_symbol_count"]
        manifest.coverage_ratio = verdict["coverage_ratio"]
        manifest.no_data_allowlist = verdict["no_data_allowlist"]
        if verdict["target_status"] != "ready":
            manifest.status = "partial"
            print(f"  STRICT POLICY -> partial: {verdict['block_reasons']}")
        store.publish(manifest)
        if (manifest.status or "") == "ready" and int(manifest.symbol_count or 0) >= 1000:
            try:
                from wtpy.apps.astock.data.repository import MarketDataRepository
                _repo = MarketDataRepository(store)
                _demoted = _repo.supersede_dominated_ready(manifest)
                if _demoted:
                    print(f"  superseded dominated tdxquant/front sets: {len(_demoted)}")
                    for _did in _demoted[:8]:
                        print(f"    - {_did}")
            except Exception as _se:
                print(f"  WARN: supersede dominated tdx front failed: {_se}")
        try:
            ck_path.unlink(missing_ok=True)
        except Exception:
            pass

        cov_out = getattr(args, "coverage_out", None)
        if cov_out:
            import csv as _csv
            Path(cov_out).parent.mkdir(parents=True, exist_ok=True)
            with open(cov_out, "w", newline="", encoding="utf-8-sig") as f:
                w = _csv.writer(f)
                w.writerow(["canonical_symbol", "provider_symbol", "exchange",
                            "board", "raw_available", "tdxquant_supported",
                            "front_data_available", "earliest_date",
                            "latest_date", "rows", "inclusion_status",
                            "exclusion_reason", "no_data_reason",
                            "provider_error"])
                for sym, u in eligible:
                    r = done.get(sym, {})
                    st = r.get("status", "")
                    w.writerow([
                        sym, _to_tdx_code(sym), u.get("exchange", ""),
                        u.get("board", ""), True, st == "ok", st == "ok",
                        r.get("first_date", ""), r.get("last_date", ""),
                        r.get("rows", 0), "included" if st == "ok" else st,
                        "", "tdx_no_data" if st == "no_data" else "",
                        r.get("error", "") if st == "failed" else ""])
                for sym, u, reason in excluded:
                    w.writerow([sym, _to_tdx_code(sym), u.get("exchange", ""),
                                u.get("board", ""), True, False, False,
                                "", "", 0, "excluded", reason, "", ""])
            print(f"  coverage CSV -> {cov_out}")

        log_payload = {
            "sync_run_id": sync_run_id, "dataset_id": dataset_id,
            "source": "tdxquant", "adjustment": "front", "period": "1d",
            "cutoff": cutoff, "universe_sha256": uni_sha,
            "universe_hash": universe_hash,
            "tqcenter_version": tqcenter_ver,
            "batch_size": batch_size,
            "stats": {**stats, "elapsed_seconds": round(elapsed, 1)},
            "result": {"eligible": len(symbols),
                       "excluded_pre_sync": len(excluded),
                       "imported": manifest.imported_symbol_count,
                       "no_data": manifest.no_data_symbol_count,
                       "failed": manifest.failed_symbol_count,
                       "rows": total_rows,
                       "earliest_date": min(firsts) if firsts else None,
                       "latest_date": max(lasts) if lasts else None,
                       "status": manifest.status},
        }
        store.save_sync_log(sync_run_id, log_payload)
        for extra in (getattr(args, "log_path", None), getattr(args, "report_path", None)):
            if extra:
                try:
                    Path(extra).parent.mkdir(parents=True, exist_ok=True)
                    Path(extra).write_text(
                        json.dumps(log_payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                except Exception:
                    pass

        print(f"  tdxquant/front: {manifest.imported_symbol_count}/{len(symbols)} ok, "
              f"no_data={manifest.no_data_symbol_count}, "
              f"failed={manifest.failed_symbol_count}")
        print(f"  dataset={dataset_id} status={manifest.status} rows={total_rows} "
              f"elapsed={elapsed:.0f}s provider_calls={stats['provider_calls']}")
        return {"status": "success", "sync_run_id": sync_run_id,
                "dataset_id": dataset_id, "dataset_status": manifest.status,
                "stats": log_payload["stats"], "result": log_payload["result"]}
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


# Raw sync no_data policy: symbols with no data AND no parent history to
# retain must not silently vanish from a new dataset. Exceeding the stricter
# of the two limits forces the manifest to partial.
RAW_NO_DATA_MAX_RATIO = 0.05
RAW_NO_DATA_MAX_COUNT = 20


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
    rebuild_symbols: Optional[set] = None,
    checkpoint_path: Optional[Path] = None,
    resume_records: Optional[Dict[str, dict]] = None,
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
    no_data_no_parent = 0
    total_rows = 0
    errors: List[dict] = []
    symbol_records: List[SymbolRecord] = []

    # Parent records for window retention: a suspended symbol with parent
    # history keeps its parent blob instead of vanishing from the new surface.
    parent_manifest = (
        store.load_manifest(parent_dataset_id) if parent_dataset_id else None
    )
    parent_records = {
        r.symbol: r for r in (parent_manifest.symbols if parent_manifest else [])
        if r.blob_sha256
    }

    # ---- restore from checkpoint (resume) ----
    done_map: Dict[str, dict] = dict(resume_records or {})
    for sym, rec in done_map.items():
        q = rec.get("quality")
        parent_rec = parent_records.get(sym)
        if q == "no_data" and parent_rec is not None:
            # The window produced no rows before the interrupt and a parent
            # blob exists: keep the parent history instead of dropping it.
            rec = {
                "symbol": sym,
                "blob_sha256": parent_rec.blob_sha256,
                "first_date": parent_rec.first_date,
                "last_date": parent_rec.last_date,
                "row_count": int(parent_rec.row_count or 0),
                "quality": "ok",
                "window_status": "no_new_rows_parent_retained",
            }
            q = "ok"
        symbol_records.append(SymbolRecord(**rec))
        if q == "ok":
            success += 1
            total_rows += int(rec.get("row_count", 0))
        elif q == "no_data":
            no_data += 1
            no_data_no_parent += 1
        else:
            failed += 1
    if done_map:
        print(f"  [resume] restored {len(done_map)} symbols from checkpoint "
              f"for {adjustment.value}/{period.value}", flush=True)

    def _persist_checkpoint() -> None:
        if checkpoint_path is None:
            return
        from wtpy.apps.astock.data.io_util import atomic_write_json
        ck = {
            "checkpoint_version": 1,
            "sync_run_id": sync_run_id,
            "source": source,
            "market_data_root": str(store.root),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phases": {},
        }
        try:
            if checkpoint_path.exists():
                ck = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        ck.setdefault("phases", {})[f"{adjustment.value}/{period.value}"] = {
            "done": {r.symbol: r.to_dict() for r in symbol_records},
            "dataset_id": dataset_id,
        }
        ck["sync_run_id"] = sync_run_id
        ck["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        atomic_write_json(checkpoint_path, ck)

    phase = f"{adjustment.value}/{period.value}"
    total_symbols = len(symbols)
    print(
        f"  Syncing {total_symbols} symbols phase={phase} "
        f"start={start_date} end={end_date} batch={getattr(provider, '_batch_size', 1)}",
        flush=True,
    )
    print(
        f"[SYNC_PROGRESS] done={len(done_map)} total={total_symbols} phase={phase}",
        flush=True,
    )

    def _emit_progress(force: bool = False) -> None:
        # Emit every 10 symbols (or force) so the data-center UI updates during
        # batch fetches; previously batch path never printed progress at all.
        _done = success + failed + no_data
        if force or _done == total_symbols or _done % 10 == 0:
            print(
                f"[SYNC_PROGRESS] done={_done} total={total_symbols} phase={phase}",
                flush=True,
            )

    def _start_for_symbol(norm_sym: str) -> Optional[int]:
        """Per-symbol fetch window: rebuild => full history; else start_date."""
        if rebuild_symbols and norm_sym in rebuild_symbols:
            return None
        return start_date

    def _finalize_bars(
        norm_sym: str, bars: List[MarketBar], use_start: Optional[int]
    ) -> List[MarketBar]:
        """Optionally prepend parent history when doing a true incremental window."""
        if not bars:
            return bars
        if not parent_dataset_id or use_start is None:
            return bars
        try:
            parent_bars = MarketDataRepository(store).load_bars(
                dataset_id=parent_dataset_id,
                symbol=norm_sym,
            )
        except Exception:
            parent_bars = []
        if not parent_bars:
            return bars
        return _merge_bar_lists(parent_bars, bars)

    def _empty_record(norm_sym: str) -> SymbolRecord:
        """Empty window: retain the parent blob when one exists (a suspended
        stock must not disappear from the new surface); otherwise no_data."""
        nonlocal no_data, no_data_no_parent, success, total_rows
        parent_rec = parent_records.get(norm_sym)
        if parent_rec is None:
            no_data += 1
            no_data_no_parent += 1
            return SymbolRecord(
                symbol=norm_sym, blob_sha256="",
                quality="no_data", error="empty",
            )
        success += 1
        total_rows += int(parent_rec.row_count or 0)
        return SymbolRecord(
            symbol=norm_sym, blob_sha256=parent_rec.blob_sha256,
            first_date=parent_rec.first_date, last_date=parent_rec.last_date,
            row_count=int(parent_rec.row_count or 0), quality="ok",
            window_status="no_new_rows_parent_retained",
        )

    batch_size = getattr(provider, "_batch_size", 1)
    caps = provider.capabilities()
    # When any symbol needs a different start_date (rebuild full vs incremental
    # window), fall back to per-symbol path so progress + merge stay correct.
    mixed_starts = bool(rebuild_symbols) and start_date is not None
    use_batch = bool(caps.supports_batch and batch_size > 1 and not mixed_starts)

    if use_batch:
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch = [s for s in batch if _normalize_symbol(s) not in done_map]
            if not batch:
                continue
            # Homogeneous window for the whole batch
            use_start = _start_for_symbol(_normalize_symbol(batch[0]))
            try:
                req = MarketDataRequest(
                    symbols=batch,
                    period=period,
                    adjustment=adjustment,
                    start_date=use_start,
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
                        symbol_records.append(_empty_record(norm_sym))
                        continue
                    sym_bars = _finalize_bars(norm_sym, sym_bars, use_start)
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
            except ProviderError:
                for sym in batch:
                    norm_sym = _normalize_symbol(sym)
                    try:
                        single_start = _start_for_symbol(norm_sym)
                        single_req = MarketDataRequest(
                            symbols=[sym],
                            period=period,
                            adjustment=adjustment,
                            start_date=single_start,
                            end_date=end_date,
                            anchor_date=anchor_date,
                        )
                        single_bars = provider.fetch_bars(single_req)
                        if not single_bars:
                            symbol_records.append(_empty_record(norm_sym))
                            continue
                        single_bars = _finalize_bars(
                            norm_sym, single_bars, single_start
                        )
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
                            SymbolRecord(
                                symbol=norm_sym,
                                blob_sha256="",
                                quality="error",
                                error=str(e2),
                            )
                        )
            _emit_progress()
            _persist_checkpoint()
        _emit_progress(force=True)
    else:
        for sym in symbols:
            norm_sym = _normalize_symbol(sym)
            if norm_sym in done_map:
                continue
            use_start = _start_for_symbol(norm_sym)
            try:
                req = MarketDataRequest(
                    symbols=[sym],
                    period=period,
                    adjustment=adjustment,
                    start_date=use_start,
                    end_date=end_date,
                    anchor_date=anchor_date,
                )
                bars = provider.fetch_bars(req)
                if not bars:
                    symbol_records.append(_empty_record(norm_sym))
                else:
                    bars = _finalize_bars(norm_sym, bars, use_start)
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
                    SymbolRecord(
                        symbol=norm_sym,
                        blob_sha256="",
                        quality="error",
                        error=str(e),
                    )
                )
            _emit_progress()
            if (success + failed + no_data) % 50 == 0:
                _persist_checkpoint()
        _emit_progress(force=True)
        _persist_checkpoint()

    manifest.symbols = symbol_records
    manifest.symbol_count = len(symbol_records)
    manifest.row_count = total_rows

    # Too many symbols with no data AND no parent history must not publish as
    # ready (silent data loss on the new surface). The threshold has a floor
    # of 2 so small universes (e.g. ~30-symbol index/ETF surfaces with 2-3
    # legitimately missing members) are not demoted to partial by rounding
    # 5% down to 1; large universes still cap at the 5% ratio / 20 count.
    max_no_data = min(
        RAW_NO_DATA_MAX_COUNT,
        max(2, int(RAW_NO_DATA_MAX_RATIO * len(symbols))),
    )
    if no_data_no_parent > max_no_data:
        manifest.status = "partial"
        print(f"  STRICT POLICY -> partial: no_data_without_parent="
              f"{no_data_no_parent} > {max_no_data} allowed")

    try:
        store.publish(manifest)
        if checkpoint_path is not None and checkpoint_path.exists():
            try:
                ck = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                ck.get("phases", {}).pop(f"{adjustment.value}/{period.value}", None)
                if not ck.get("phases"):
                    checkpoint_path.unlink(missing_ok=True)
                else:
                    from wtpy.apps.astock.data.io_util import atomic_write_json
                    atomic_write_json(checkpoint_path, ck)
            except Exception:
                pass
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



def _merge_bar_lists(
    parent_bars: List[MarketBar], new_bars: List[MarketBar]
) -> List[MarketBar]:
    """Merge parent history with newly fetched bars; new bars win on date conflict."""
    by_date: Dict[int, MarketBar] = {b.trade_date: b for b in parent_bars}
    for b in new_bars:
        by_date[b.trade_date] = b
    return [by_date[d] for d in sorted(by_date)]


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


def _reconcile_after_sync(store: DatasetStore, *, dry_run: bool = False) -> dict:
    """Auto-reconcile the Tushare product pair after a Tushare sync/derive.

    Local-only: composite manifests + QFQ derivation reuse existing blobs,
    never a network full-history pull. Missing parents return a structured
    ``waiting_for_parent`` state instead of publishing half-baked surfaces.
    """
    from wtpy.apps.astock.data.tushare_product import (
        reconcile_tushare_product_datasets,
    )

    try:
        r = reconcile_tushare_product_datasets(store, dry_run=dry_run)
        print(
            f"  [reconcile] status={r.status} l1={r.l1_dataset_id or '-'} "
            f"l2={r.l2_dataset_id or '-'} missing={r.missing or '-'} "
            f"issues={r.issues or '-'}"
        )
        return r.to_dict()
    except Exception as e:
        print(f"  [reconcile] ERROR: {type(e).__name__}: {e}")
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}


def _apply_reconcile_status(result: dict) -> None:
    """Overall task success requires the full product chain to pass.

    Sync tasks only report success when raw, factor, L2, L1, lineage and
    freshness all passed — otherwise the task is marked ``warning`` with the
    reconcile reason, never a blanket success.
    """
    rc = (result.get("reconcile") or {}).get("status")
    if result.get("status") == "success" and rc not in ("published", "up_to_date"):
        result["status"] = "warning"
        result["warning"] = (
            f"product_reconcile={rc} missing="
            f"{result['reconcile'].get('missing') or '-'} issues="
            f"{result['reconcile'].get('issues') or '-'}"
        )


def _aggregate_dataset_status(results: Dict[str, dict]) -> Tuple[str, str]:
    """Aggregate per-dataset results into a top-level (status, detail).

    Any dataset failed -> ('failed', phase); any dataset partial or with a
    non-ready status -> ('partial', phases); otherwise ('success', ''). A raw
    surface that only published partial must never be reported as success.
    """
    partial_phases: List[str] = []
    for phase, ds in (results or {}).items():
        if not isinstance(ds, dict):
            continue
        st = ds.get("status") or ds.get("dataset_status") or ""
        if st == "failed":
            return "failed", f"dataset {phase} failed"
        if st not in ("ready", "success", ""):
            partial_phases.append(f"{phase}={st}")
    if partial_phases:
        return "partial", "; ".join(partial_phases)
    return "success", ""


def _auto_resolve_parents(
    args,
    store: DatasetStore,
    *,
    raw_source: str,
    raw_adjustment: str,
) -> Optional[str]:
    """Fill missing --raw-dataset-id / --factor-dataset-id with the latest
    ready parents. UI-launched derive tasks never select dataset ids, so
    without this they always failed with missing_parent_dataset_ids."""
    if not getattr(args, "raw_dataset_id", None):
        best_id, best_cut = None, 0
        for mid in store.list_manifests():
            m = store.load_manifest(mid)
            if not m or m.source != raw_source or m.adjustment != raw_adjustment:
                continue
            if m.status != "ready":
                continue
            c = int(m.data_cutoff_date or 0)
            if c > best_cut:
                best_cut, best_id = c, m.dataset_id
        if not best_id:
            return f"no ready {raw_source}/{raw_adjustment} parent dataset"
        print(f"  [auto] raw parent -> {best_id} (cutoff={best_cut})")
        args.raw_dataset_id = best_id
    if not getattr(args, "factor_dataset_id", None):
        # Latest-candidate semantics (P1 derive-path review): when the NEWEST
        # factor manifest is a freshness-gate-blocked partial, the derive must
        # refuse instead of silently falling back to an older ready factor —
        # otherwise a残缺 L1 derived from stale factors could enter the formal
        # product plane (the derive-time freshness gate is the second line of
        # defence, but failing fast here avoids running the derivation at all).
        from wtpy.apps.astock.data.tushare_product import (
            _select_latest_tushare_factor_candidate,
        )
        latest = _select_latest_tushare_factor_candidate(store)
        if latest is not None and (latest.status or "") == "partial" and (
            ((latest.provenance or {}).get("freshness") or {}).get("gate")
            == "blocked"
        ):
            return (
                "latest factor is a freshness-gate-blocked partial "
                f"({latest.dataset_id}); refusing to derive over an older "
                "ready factor"
            )
        best_id, best_cut = None, 0
        for mid in store.list_manifests():
            m = store.load_manifest(mid)
            if not m or m.dataset_type != "factor" or m.status != "ready":
                continue
            c = int(m.data_cutoff_date or 0)
            if c > best_cut:
                best_cut, best_id = c, m.dataset_id
        if not best_id:
            return "no ready factor dataset"
        print(f"  [auto] factor parent -> {best_id} (cutoff={best_cut})")
        args.factor_dataset_id = best_id
    return None


def _exit_code_for_results(all_results: dict) -> int:
    """Map per-source sync results to a process exit code.

    0 = every source succeeded; 1 = any source failed; 2 = only
    warning/partial results. Non-zero codes let schedulers/UI stop treating
    business failures (expired token, missing parents, blocked reconcile) as
    success. A result whose top-level status is success but whose
    dataset_status is partial/building/failed is NOT a success (e.g. the
    factor sync demoted by the freshness gate returns
    {"status": "success", "dataset_status": "partial"}).
    """
    statuses = []
    for r in all_results.values():
        if not isinstance(r, dict):
            continue
        st = r.get("status", "failed")
        if st == "success":
            ds = (r.get("dataset_status") or "").strip()
            if ds == "failed":
                st = "failed"
            elif ds in ("partial", "building"):
                st = "partial"
        statuses.append(st)
    if any(st == "failed" for st in statuses):
        return 1
    if any(st in ("warning", "partial") for st in statuses):
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description="Sync market data to local datasets")
    parser.add_argument("--source", required=True,
                        choices=["tdxquant", "tushare", "tdx_local", "local_vendor",
                                 "internal", "all"])
    parser.add_argument("--mode", default="full",
                        choices=["full", "incremental", "rebuild", "audit", "derive"],
                        help="full (default) / incremental: 存在完整父数据集时"
                             "只拉修正窗口并合并（无父则全量）; rebuild: 忽略"
                             "父数据集强制全量重建历史; audit/derive: diagnostics")
    parser.add_argument("--symbol", default=None, help="Comma-separated symbols")
    parser.add_argument("--asset-class", default="stocks",
                        choices=["stocks", "index", "etf", "all"],
                        help="For --source tushare: stocks (default) | index | etf | all "
                             "(index/ETF sync uses index_daily/fund_daily, none/1d only)")
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
    parser.add_argument("--factor-raw-root", default=None,
                        help="Raw cache dir for tushare adj_factor CSVs "
                             "(or env TUSHARE_FACTOR_RAW_ROOT)")
    parser.add_argument("--keep-raw-batches", type=int, default=3,
                        help="Keep the newest N tsfactor_* raw batch dirs after "
                             "a successful adj_factor sync, deleting older ones "
                             "(the fixed 'latest' incremental cache is untouched)")
    parser.add_argument("--rate-per-min", type=int, default=400,
                        help="Max Tushare API calls per minute for factor sync")
    parser.add_argument("--skip-freshness-gate", action="store_true",
                        help="Factor sync: skip the per-symbol freshness gate "
                             "(explicit escape hatch, e.g. subset-universe "
                             "deployments; the decision is recorded in the "
                             "manifest provenance as freshness_gate="
                             "skipped_by_flag)")
    parser.add_argument("--coverage-out", default=None,
                        help="Write factor symbol coverage/mapping CSV here")
    parser.add_argument("--raw-dataset-id", default=None,
                        help="Parent raw dataset id (mode=derive)")
    parser.add_argument("--supplement-factor-dataset-id", default=None,
                        help="Gate B6: supplemental factor dataset (delisted "
                             "stocks) for composite QFQ derivation")
    parser.add_argument("--universe-dataset-id", dest="universe_dataset_id_arg",
                        default=None,
                        help="Gate B6: point-in-time universe id providing the "
                             "BSE pre-migration alias map")
    parser.add_argument("--factor-dataset-id", default=None,
                        help="Parent factor dataset id (mode=derive)")
    parser.add_argument("--cutoff", type=int, default=None,
                        help="Derivation cutoff date YYYYMMDD (default: raw latest)")
    parser.add_argument("--batch-pause", type=float, default=0.05,
                        help="Seconds to pause between tdxquant batches (throttle)")
    parser.add_argument("--skip-ca-detect", action="store_true",
                        help="Skip per-symbol CA detection in incremental mode (much faster)")

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
        if not storage_root.exists():
            print("ERROR: preflight failed: storage root does not exist")
            return 1
        if args.source == "tdxquant" and (args.adjustment or "") == "front":
            from wtpy.apps.astock.data.providers.tdxquant import (
                TdxQuantProvider, read_tqcenter_version)
            tq_file = Path(args.tdx_root) / "PYPlugins" / "user" / "tqcenter.py"
            print(f"  tqcenter file: {tq_file} exists={tq_file.exists()}")
            print(f"  tqcenter version: {read_tqcenter_version(args.tdx_root)}")
            _prov = TdxQuantProvider(tdx_root=args.tdx_root,
                                     batch_size=args.batch_size)
            print(f"  TdxQuant client health: {_prov.health_check()}")
            if args.universe_file:
                _up = Path(args.universe_file)
                if _up.exists():
                    import hashlib as _hl2
                    _rows = _load_universe_rows(_up)
                    _eli, _exc = _tdx_partition_universe(_rows)
                    _syms = sorted(s for s, _ in _eli)
                    print(f"  Universe file: {_up} rows={len(_rows)} "
                          f"eligible={len(_eli)} pre_excluded={len(_exc)}")
                    print(f"  Universe sha256: "
                          f"{_hl2.sha256(_up.read_bytes()).hexdigest()[:16]}...")
                    print(f"  Universe hash (eligible): "
                          f"{_hl2.sha256(','.join(_syms).encode()).hexdigest()[:16]}...")
                else:
                    print(f"  Universe file: {_up} MISSING")
            else:
                print("  Universe file: (REQUIRED — pass --universe-file)")
            _lockf = storage_root / ".locks" / "sync_tdxquant_front_1d.lock"
            if _lockf.exists():
                from wtpy.apps.astock.data.sync_lock import SyncTaskLock as _STL
                print(f"  Lock file: {_lockf}")
                print(f"  Lock holder metadata: {_STL.probe(_lockf)}")
            else:
                print("  Lock file: (none)")
            _ckf = storage_root / "sync_logs" / "checkpoint_tdxquant_front_1d.json"
            if _ckf.exists():
                try:
                    _ckj = json.loads(_ckf.read_text(encoding="utf-8"))
                    print(f"  Checkpoint: {len(_ckj.get('done', {}))}"
                          f"/{_ckj.get('eligible_count')} symbols done "
                          f"(sync_run_id={_ckj.get('sync_run_id')}) — use --resume")
                except Exception:
                    print(f"  Checkpoint: unreadable ({_ckf})")
            else:
                print("  Checkpoint: (none)")
            print(f"  Batch size: {args.batch_size} | retry: "
                  f"batch->singles, {TDX_MAX_SINGLE_RETRIES} retries/symbol")
            print(f"  no_data policy: allowlist_file="
                  f"{args.allow_no_data_file or '(none)'} "
                  f"max_count={args.max_no_data_count} "
                  f"max_ratio={args.max_no_data_ratio}")
            _bld, _rdy = [], []
            _mdir = storage_root / "manifests"
            if _mdir.exists():
                for _mp in _mdir.glob("tdxquant_front_1d_*.json"):
                    try:
                        _mj = json.loads(_mp.read_text(encoding="utf-8"))
                        (_bld if _mj.get("status") == "building" else _rdy).append(
                            (_mj.get("dataset_id"), _mj.get("status")))
                    except Exception:
                        pass
            print(f"  Building tdxquant/front datasets: {_bld or '(none)'}")
            print(f"  Existing tdxquant/front datasets: {_rdy or '(none)'}")
            try:
                _probe_f = storage_root / ".preflight_write_probe"
                _probe_f.write_text("ok", encoding="utf-8")
                _probe_f.unlink()
                print("  Write permission: True")
            except Exception as _we:
                print(f"  Write permission: False ({_we})")
            try:
                import psutil as _ps
                _vm = _ps.virtual_memory()
                print(f"  Memory: {_vm.available / 1024**3:.1f} GB available "
                      f"of {_vm.total / 1024**3:.1f} GB")
            except ImportError:
                try:
                    import ctypes as _ct

                    class _MS(_ct.Structure):
                        _fields_ = [("dwLength", _ct.c_uint32),
                                    ("dwMemoryLoad", _ct.c_uint32),
                                    ("ullTotalPhys", _ct.c_uint64),
                                    ("ullAvailPhys", _ct.c_uint64),
                                    ("ullTotalPageFile", _ct.c_uint64),
                                    ("ullAvailPageFile", _ct.c_uint64),
                                    ("ullTotalVirtual", _ct.c_uint64),
                                    ("ullAvailVirtual", _ct.c_uint64),
                                    ("ullAvailExtendedVirtual", _ct.c_uint64)]

                    _ms = _MS(dwLength=_ct.sizeof(_MS))
                    _ct.windll.kernel32.GlobalMemoryStatusEx(_ct.byref(_ms))
                    print(f"  Memory: {_ms.ullAvailPhys / 1024**3:.1f} GB available "
                          f"of {_ms.ullTotalPhys / 1024**3:.1f} GB")
                except Exception:
                    print("  Memory: (unavailable)")
        if args.source == "tushare" and args.adjustment == "adj_factor":
            frr = _resolve_factor_raw_root(args)
            print(f"  Factor raw root: {frr or '(default: <data root>/tushare_factor_raw_cache)'}")
            try:
                import warnings as _w
                _w.filterwarnings("ignore")
                import tushare as _ts
                _tok = None
                try:
                    _tok = _ts.get_token()
                except Exception:
                    pass
                print(f"  token_configured: {bool(_tok)}")
            except ImportError:
                print("  token_configured: False (tushare not installed)")
            lockf = storage_root / ".locks" / "sync_tushare_adj_factor_1d.lock"
            print(f"  Lock file: {lockf if lockf.exists() else '(none)'}")
            ckf = storage_root / "sync_logs" / "checkpoint_tushare_adj_factor_1d.json"
            print(f"  Checkpoint: {ckf if ckf.exists() else '(none)'}")
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
        return 0

    if args.dry_run:
        print(f"DRY RUN: source={args.source}, mode={args.mode}"
              + (f", adjustment={args.adjustment}" if args.adjustment else ""))
        print(f"  Storage root: {storage_root}")
        if args.source == "tdxquant" and (args.adjustment or "") == "front":
            if not args.universe_file:
                print("  Universe: (REQUIRED — pass --universe-file)")
                return 0
            _up = Path(args.universe_file)
            _rows = _load_universe_rows(_up)
            _eli, _exc = _tdx_partition_universe(_rows)
            _bs = max(1, min(int(args.batch_size or 10), 50))
            _nb = (len(_eli) + _bs - 1) // _bs
            _est = len(_eli) * TDX_EST_SEC_PER_SYMBOL + _nb * float(args.batch_pause or 0)
            import hashlib as _hl2
            _syms = sorted(s for s, _ in _eli)
            print(f"  source=tdxquant adjustment=front period=1d")
            print(f"  Candidates: {len(_rows)} universe rows")
            print(f"  Eligible: {len(_eli)} | Pre-excluded: {len(_exc)}")
            _reasons = {}
            for _, _, _r in _exc:
                _reasons[_r] = _reasons.get(_r, 0) + 1
            for _r, _c in sorted(_reasons.items()):
                print(f"    excluded[{_r}] = {_c}")
            print(f"  Batch plan: {_nb} batches x {_bs}")
            print(f"  Estimated provider calls: ~{_nb} batch calls "
                  f"(tqcenter issues 1 DLL call per symbol internally: "
                  f"~{len(_eli)} symbol fetches)")
            print(f"  Estimated duration: ~{_est/60:.0f} min "
                  f"(@{TDX_EST_SEC_PER_SYMBOL}s/symbol measured)")
            print(f"  Cutoff: {args.end_date or '(today)'}")
            print(f"  Universe sha256: {_hl2.sha256(_up.read_bytes()).hexdigest()[:16]}...")
            print(f"  Universe hash (eligible): "
                  f"{_hl2.sha256(','.join(_syms).encode()).hexdigest()[:16]}...")
            print(f"  Checkpoint path: "
                  f"{storage_root / 'sync_logs' / 'checkpoint_tdxquant_front_1d.json'}")
            _mdir = storage_root / "manifests"
            _existing = []
            if _mdir.exists():
                for _mp in _mdir.glob("tdxquant_front_1d_*.json"):
                    try:
                        _mj = json.loads(_mp.read_text(encoding="utf-8"))
                        _existing.append((_mj.get("dataset_id"), _mj.get("status")))
                    except Exception:
                        pass
            print(f"  Existing tdxquant/front datasets (preserved): "
                  f"{_existing or '(none)'}")
            print("  No client calls made. No data will be written.")
            return 0
        if args.source == "tushare" and args.adjustment == "adj_factor":
            if args.universe_file:
                syms = _load_universe_file(Path(args.universe_file))
                rpm = max(1, int(args.rate_per_min or 400))
                print(f"  Factor universe: {len(syms)} symbols")
                print(f"  Rate limit: {rpm}/min -> est {len(syms)/rpm:.0f}+ min "
                      f"(1 adj_factor call per symbol + 2 stock_basic calls)")
                print(f"  Raw cache: {_resolve_factor_raw_root(args) or '(not configured)'}")
            else:
                print("  Factor universe: (requires --universe-file)")
            print("  No Tushare daily bars will be downloaded. No data will be written.")
            return 0
        if args.mode == "derive":
            print(f"  raw_dataset_id: {args.raw_dataset_id}")
            print(f"  factor_dataset_id: {args.factor_dataset_id}")
            print(f"  cutoff: {args.cutoff or '(raw latest)'} | formula {QFQ_FORMULA_VERSION}")
            print("  No data will be written.")
            return 0
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
        return 0

    store = DatasetStore(storage_root)

    if args.mode == "audit":
        result = audit_dataset(args, store)
        return 0 if (result or {}).get("status") == "success" else 1

    if args.mode == "derive":
        if args.source != "internal":
            print("ERROR: --mode derive requires --source internal")
            return 1
        adj = args.adjustment or "tushare_factor_qfq"
        if adj == "tushare_factor_qfq":
            err = _auto_resolve_parents(
                args, store, raw_source="local_vendor", raw_adjustment="none"
            )
            if err:
                print(f"ERROR: {err}")
                return 1
            r = derive_tushare_factor_qfq(args, store)
        elif adj == "composite_tushare_factor_qfq":
            err = _auto_resolve_parents(
                args, store, raw_source="internal", raw_adjustment="composite_none"
            )
            if err:
                print(f"ERROR: {err}")
                return 1
            r = derive_composite_tushare_factor_qfq(args, store)
            if r.get("status") == "success":
                r["reconcile"] = _reconcile_after_sync(store)
                _apply_reconcile_status(r)
        else:
            print("ERROR: derive supports adjustment=tushare_factor_qfq or "
                  "composite_tushare_factor_qfq only")
            return 1
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
        r_status = str(r.get("status") or "failed")
        r_ds = (r.get("dataset_status") or "").strip()
        if r_status == "failed":
            return 1
        if r_status == "success" and r_ds not in ("partial", "building", "failed"):
            return 0
        return 2

    # Tushare-only policy: `all` now means "all Tushare stock tasks" — TDX,
    # tdx_local and local_vendor providers are never initialized by the
    # default chain (legacy sources stay available via explicit --source).
    if args.source == "all":
        print("NOTE: --source all now means Tushare-only (policy); "
              "TDX/local_vendor require explicit --source")
    sources = [args.source] if args.source != "all" else ["tushare"]
    all_results = {}

    for src in sources:
        print(f"\n{'='*60}")
        print(f"Source: {src} | Mode: {args.mode}"
              + (f" | Adjustment: {args.adjustment}" if args.adjustment else ""))
        print(f"{'='*60}")

        if src == "tdxquant":
            if (args.adjustment or "") == "front" and args.mode in ("full", "rebuild"):
                # Gate C phase 2 production path: front/1d with lock+checkpoint
                r = sync_tdxquant_front_full(args, store)
            elif args.mode in ("full", "rebuild"):
                r = sync_tdxquant_full(args, store)
            else:
                r = sync_tdxquant_incremental(args, store)
        elif src == "tushare":
            asset = (args.asset_class or "stocks").lower()
            if asset in ("index", "etf", "all"):
                if args.adjustment == "adj_factor":
                    r = {"status": "failed",
                         "error": "adj_factor sync does not apply to index/ETF "
                                  "(no 复权)"}
                elif args.mode in ("full", "rebuild"):
                    r = sync_tushare_index_etf_full(args, store)
                else:
                    r = sync_tushare_index_etf_incremental(args, store)
            elif args.adjustment == "adj_factor":
                # Gate C factor mode: fetches adj_factor ONLY, never daily bars
                r = sync_tushare_adj_factor_full(args, store)
            elif args.mode in ("full", "rebuild"):
                r = sync_tushare_full(args, store)
            elif args.mode == "incremental" and not args.adjustment:
                # Zero-config default chain (task=tushare): raw incremental ->
                # adj_factor incremental -> product reconcile. Explicit
                # --adjustment none remains a raw-only incremental sync.
                r = sync_tushare_chain(args, store)
            else:
                r = sync_tushare_incremental(args, store)
        elif src == "tdx_local":
            r = sync_tdx_local_full(args, store)
        elif src == "local_vendor":
            r = sync_local_vendor_full(args, store)
        elif src == "internal":
            r = {"status": "failed",
                 "error": "source=internal only supports --mode derive"}
        else:
            r = {"status": "failed", "error": f"unknown source: {src}"}

        all_results[src] = r

    print(f"\n{'='*60}")
    print("Sync complete.")
    print(json.dumps(all_results, indent=2, ensure_ascii=False, default=str))

    code = _exit_code_for_results(all_results)
    if code == 1:
        _reasons = [
            f"{src}={r.get('status')}({r.get('error') or 'no detail'})"
            for src, r in all_results.items()
            if isinstance(r, dict) and r.get("status") == "failed"
        ]
        print(f"SYNC STATUS: failed ({'; '.join(_reasons)})")
    elif code == 2:
        _reasons = [
            f"{src}={r.get('status')}"
            for src, r in all_results.items()
            if isinstance(r, dict) and r.get("status") in ("warning", "partial")
        ]
        print(f"SYNC STATUS: warning ({'; '.join(_reasons)})")
    else:
        print("SYNC STATUS: success")
    return code


if __name__ == "__main__":
    sys.exit(main())
