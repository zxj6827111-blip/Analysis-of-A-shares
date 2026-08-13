"""System routes: health, market-data, sync, pages."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..version import get_version_info, get_version_string
from .context import ApiContext, get_ctx

router = APIRouter()

# Project root: api_routes/ is one level deeper than api.py, so the anchor is
# parents[4] here (parents[3] would resolve to wtpy/ and break UI sync).
PROJECT_ROOT = Path(__file__).resolve().parents[4]

SYNC_SCRIPT = str(PROJECT_ROOT / "scripts" / "sync_market_data.py")
PROGRESS_RE = re.compile(r"\[SYNC_PROGRESS\] done=(\d+) total=(\d+) phase=(\S+)")
CHECKPOINT_FILES = {
    "tdx": "checkpoint_tdxquant_front_1d.json",
    "factor": "checkpoint_tushare_adj_factor_1d.json",
    "tushare": "checkpoint_tushare_incremental_1d.json",
    "local_vendor": "checkpoint_local_vendor_none_1d.json",
}
HTML_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
class SyncStartBody(BaseModel):
    task: str
    end_date: Optional[int] = None
    start_date: Optional[int] = None
    resume: bool = False
    fresh: bool = False

def _checkpoint_exists(ctx: ApiContext, task: str) -> bool:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    fname = CHECKPOINT_FILES.get(task)
    if not fname:
        return False
    return (cfg.market_data_root / "sync_logs" / fname).exists()
def _latest_factor_universe_file(ctx: ApiContext) -> Optional[str]:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """Reuse the latest ready adj_factor universe for UI-launched refreshes."""
    import os as _os
    from ..data.dataset_store import DatasetStore

    for key in ("TUSHARE_FACTOR_UNIVERSE_FILE", "ASTOCK_FACTOR_UNIVERSE_FILE"):
        explicit = _os.environ.get(key, "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    store = DatasetStore(cfg.market_data_root)
    candidates = []
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
        uf = (m.universe_file or "").strip()
        if not uf or not Path(uf).exists():
            continue
        candidates.append((int(m.data_cutoff_date or 0), int(m.symbol_count or 0), m.created_at or "", uf))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]
def _run_sync_process(ctx: ApiContext, cmd: List[str], task_name: str) -> None:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    proc = None
    try:
        import os as _os
        env = dict(_os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # Always pin formal data root so UI sync never writes to internal test tree.
        try:
            env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
            if cfg.astock_env:
                env["ASTOCK_ENV"] = str(cfg.astock_env)
        except Exception:
            pass
        # Explicit --storage-root wins even if script ignores env on some paths.
        if "--storage-root" not in cmd:
            cmd = list(cmd) + ["--storage-root", str(cfg.market_data_root)]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        with _sync_lock:
            _sync_proc["proc"] = proc
        for line in proc.stdout:
            with _sync_lock:
                _sync_state["output"].append(line.rstrip())
                if len(_sync_state["output"]) > 500:
                    _sync_state["output"] = _sync_state["output"][-500:]
                m = PROGRESS_RE.search(line)
                if m:
                    _sync_state["progress_done"] = int(m.group(1))
                    _sync_state["progress_total"] = int(m.group(2))
                    _sync_state["progress_phase"] = m.group(3)
        proc.wait()
        with _sync_lock:
            # Classify by real exit code first: rc=0 means the sync genuinely
            # finished even if a stop request arrived while the reader thread
            # was draining (stopped/done race — completed runs must never be
            # reported as stopped, which would trigger a user restart).
            if proc.returncode == 0:
                _sync_state["status"] = "done"
                _sync_state["error"] = None
            elif _sync_state.get("stop_requested"):
                _sync_state["status"] = "stopped"
                _sync_state["error"] = "用户手动停止"
            elif proc.returncode == 2:
                # 2 = warning/partial (e.g. reconcile waiting_for_parent, or a
                # non-ready factor surface). Business-incomplete but not a
                # hard failure — the UI shows the concrete reason instead of a
                # blanket "出错" (EOD retry still treats rc!=0 as failed).
                _sync_state["status"] = "warning"
                _sync_state["error"] = (
                    "exit code 2 (warning): 数据不完整，请查看日志了解原因"
                )
            else:
                # Any non-zero exit code means the sync did not fully succeed
                # (1 = business failure such as expired token / missing parent,
                # 2 = warning/partial). UI shows error instead of done so
                # business failures never look successful; the numeric code
                # itself is available to schedulers/alerting.
                _sync_state["status"] = "error"
                _sync_state["error"] = f"exit code {proc.returncode}"
    except Exception as e:
        with _sync_lock:
            # Windows TerminateProcess can make the stdout read loop raise
            # OSError while the user is stopping the sync — keep that an
            # intentional stop, not a spurious error.
            if _sync_state["status"] == "stopping":
                _sync_state["status"] = "stopped"
                _sync_state["error"] = "用户手动停止"
            else:
                _sync_state["status"] = "error"
                _sync_state["error"] = str(e)
    finally:
        # Never leave an orphan holding the SyncTaskLock: a reader-loop
        # exception or a timed-out stop must not strand the child process.
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except OSError:
                # ProcessLookupError etc. after natural exit are expected.
                pass
            except Exception:
                pass
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["finished_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            _sync_proc["proc"] = None
def _html_page(filename: str, missing_title: str) -> HTMLResponse:
    index_path = STATIC_DIR / filename
    if index_path.exists():
        return HTMLResponse(
            index_path.read_text(encoding="utf-8"),
            headers=dict(HTML_NO_CACHE_HEADERS),
        )
    return HTMLResponse(
        f"<h1>{missing_title}</h1><p>static/{filename} missing</p>",
        headers=dict(HTML_NO_CACHE_HEADERS),
    )

@router.get("/api/v1/health")
def health(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    return {
        "ok": True,
        "storage_root": str(cfg.storage_root),
        "output_root": str(cfg.output_root),
        "registry_path": str(cfg.registry_path),
        "registry_exists": Path(cfg.registry_path).exists(),
        "market_data_root": str(cfg.market_data_root),
        "market_data_root_is_external": cfg.market_data_root_is_external,
    }

@router.get("/api/v1/version")
def version(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    import platform

    info = get_version_info()
    info["python_version"] = platform.python_version()
    info["platform"] = platform.platform()
    return info

@router.get("/api/v1/market-data/status")
def market_data_status(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock

    # Full scan of every manifest + blob dir can take seconds on a large
    # warehouse; warehouse state does not change between page loads, so cache
    # the result for a short TTL (refreshed by the next sync/derive run via
    # the natural expiry).
    mdc = ctx.md_status_cache
    now = _time.time()
    if (
        mdc.get("payload") is not None
        and now - mdc.get("ts", 0.0) < 30.0
    ):
        return mdc["payload"]

    from ..data.dataset_store import DatasetStore
    from ..data.repository import MarketDataRepository
    md_root = cfg.market_data_root
    is_test = not cfg.market_data_root_is_external
    result = {
        "data_root": str(md_root),
        "is_test_root": is_test,
        "astock_env": cfg.astock_env,
        "exists": md_root.exists(),
    }
    if not md_root.exists():
        result.update({
            "manifest_count": 0, "ready_dataset_count": 0,
            "datasets": [], "product": {"l1": None, "l2": None, "active": False},
        })
        return result
    store = DatasetStore(md_root)
    repo = MarketDataRepository(store)
    # Read-only path: skip manifest deepcopy (~100k symbol records each)
    # since this handler never mutates the loaded manifests.
    all_ds = repo.list_datasets(deep_copy=False)
    ready = [d for d in all_ds if d.status == "ready"]
    partial = [d for d in all_ds if d.status == "partial"]
    failed = [d for d in all_ds if d.status == "failed"]
    total_bars = sum(d.row_count for d in all_ds)
    total_symbols = sum(d.symbol_count for d in all_ds)
    blob_dir = md_root / "blobs"
    total_size = 0
    blob_count = 0
    # The blob set only changes during sync/derive; walking ~100k files with
    # stat() takes many seconds, so cache it for 5 minutes independently of
    # the main status cache.
    bsc = ctx.md_blob_stats_cache
    if bsc.get("ts") and now - bsc.get("ts", 0.0) < 300.0:
        blob_count = bsc.get("count") or 0
        total_size = bsc.get("size") or 0
    elif blob_dir.exists():
        import os as _os

        _count = 0
        _size = 0
        with _os.scandir(blob_dir) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                _count += 1
                try:
                    _size += entry.stat().st_size
                except OSError:
                    pass
        blob_count = _count
        total_size = _size
        bsc["ts"] = now
        bsc["count"] = _count
        bsc["size"] = _size

    _date_range_cache: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    _has_stocks_cache: Dict[str, bool] = {}

    def _dataset_has_stocks(d) -> bool:
        """True when the dataset contains at least one A-share stock symbol.

        ETF/指数数据集与全市场股票共用 tushare/none/1d scope（manifest 无
        universe_type 标记），但「Tushare日线」卡是股票地基，纯 ETF/指数
        增量数据集（如手动 ETF 同步产出的 2500 只）不能覆盖它——否则 raw
        卡会显示 2500 只 ETF 冒充全市场。按符号构成区分，memoize 避免每个
        候选数据集反复遍历全部 symbol 记录。
        """
        key = getattr(d, "dataset_id", None)
        if key is not None and key in _has_stocks_cache:
            return _has_stocks_cache[key]
        res = any(".STK." in (s.symbol or "") for s in d.symbols)
        if key is not None:
            _has_stocks_cache[key] = res
        return res

    def _date_range(d):
        """min/max symbol dates; memoized per dataset_id because this walks
        every symbol record and is called repeatedly by the tile pickers."""
        key = getattr(d, "dataset_id", None)
        if key is not None and key in _date_range_cache:
            return _date_range_cache[key]
        firsts = [s.first_date for s in d.symbols if s.first_date]
        lasts = [s.last_date for s in d.symbols if s.last_date]
        res = (min(firsts) if firsts else None, max(lasts) if lasts else None)
        if key is not None:
            _date_range_cache[key] = res
        return res

    datasets_info = []
    for d in all_ds:
        earliest, latest = _date_range(d)
        datasets_info.append({
            "dataset_id": d.dataset_id,
            "source": d.source,
            "adjustment": d.adjustment,
            "status": d.status,
            "symbol_count": d.symbol_count,
            "row_count": d.row_count,
            "created_at": d.created_at,
            "earliest_date": earliest,
            "latest_date": latest,
            "data_cutoff_date": d.data_cutoff_date,
            "survivorship_bias": d.survivorship_bias,
            "universe_type": d.universe_type,
            "warning_text": d.warning_text,
            "data_policy": (d.provenance or {}).get("data_policy"),
        })

    def _factor_freshness_summary(d):
        """Freshness-gate summary for the factor tile.

        Reads manifest.provenance["freshness"] (the sync script records
        gate="blocked", the derive path records status="blocked"/"passed";
        both shapes are handled). Old-format manifests without freshness
        yield None. stale_active_symbols is capped at the top 5.
        """
        prov = d.provenance or {}
        f = prov.get("freshness") or {}
        if not f:
            if prov.get("freshness_gate") == "skipped_by_flag":
                return {
                    "fresh_symbol_ratio": None, "fresh_count": None,
                    "active_count": None, "stale_active_symbols": [],
                    "p50": None, "p10": None,
                    "gate": "skipped", "reason": "skipped_by_flag",
                }
            return None
        return {
            "fresh_symbol_ratio": f.get("fresh_symbol_ratio"),
            "fresh_count": f.get("fresh_count"),
            "active_count": f.get("active_count"),
            "stale_active_symbols": (f.get("stale_active_symbols") or [])[:5],
            "p50": f.get("p50_last_date"),
            "p10": f.get("p10_last_date"),
            "gate": f.get("gate") or f.get("status"),
            "reason": f.get("reason"),
        }

    def _pick_source_freshness(key, label, match_fn, *,
                               latest_first=False, carry_freshness=False):
        """Pick best dataset for a UI source tile.

        Default: ready > partial; then freshest cutoff, fullest symbol_count,
        most rows (blocks empty/tiny shells), then newest created_at.
        latest_first (factor tile): newest candidate wins regardless of
        status — a freshly synced partial demoted by the freshness gate must
        not be shadowed by an older ready factor surface (same ordering as
        tushare_product._select_latest_tushare_factor_candidate).
        """
        cands = [
            d for d in all_ds
            if match_fn(d) and (d.status or "") not in ("superseded", "failed", "building")
        ]
        if not cands:
            # fall back: still show something if only superseded exists
            cands = [d for d in all_ds if match_fn(d)]
        if not cands:
            tile = {
                "key": key, "label": label, "status": "missing",
                "dataset_id": None, "earliest_date": None, "latest_date": None,
                "data_cutoff_date": None, "updated_to": None,
                "symbol_count": 0, "row_count": 0, "created_at": None,
                "source": None, "adjustment": None,
            }
            if carry_freshness:
                tile["freshness"] = None
            return tile
        if latest_first:
            def _score(d):
                _earliest, latest = _date_range(d)
                updated = d.data_cutoff_date or latest or 0
                return (
                    int(updated or 0),
                    int(d.symbol_count or 0),
                    int(d.row_count or 0),
                    int(latest or 0),
                    d.created_at or "",
                )
        else:
            rank = {"ready": 3, "partial": 2, "building": 1, "failed": 0, "superseded": -1}

            def _score(d):
                _earliest, latest = _date_range(d)
                updated = d.data_cutoff_date or latest or 0
                return (
                    rank.get(d.status or "", -1),
                    int(updated or 0),
                    int(d.symbol_count or 0),
                    int(d.row_count or 0),
                    int(latest or 0),
                    d.created_at or "",
                )

        best = max(cands, key=_score)
        earliest, latest = _date_range(best)
        updated = best.data_cutoff_date or latest
        tile = {
            "key": key,
            "label": label,
            "status": best.status,
            "dataset_id": best.dataset_id,
            "source": best.source,
            "adjustment": best.adjustment,
            "earliest_date": earliest,
            "latest_date": latest,
            "data_cutoff_date": best.data_cutoff_date,
            "updated_to": updated,
            "symbol_count": best.symbol_count,
            "row_count": best.row_count,
            "created_at": best.created_at,
        }
        if carry_freshness:
            tile["freshness"] = _factor_freshness_summary(best)
        return tile

    # Tushare-only policy: resolve the formal L1/L2 product pair ONCE and
    # reuse it for both the source tiles and the product block, so the
    # dashboard can never show a tile from a different (independent-latest)
    # surface than the pair the backtests actually use.
    try:
        from ..data.tushare_product import resolve_active_tushare_product_pair

        pair = resolve_active_tushare_product_pair(store, deep_copy=False)
    except Exception:
        pair = None

    def _pair_tile(key, label, manifest, max_date, data_policy):
        """Tile for a formal product surface (same shape as
        _pick_source_freshness so index_v3.html renderSourceFreshness keeps
        working); None manifest -> inactive/None tile."""
        if manifest is None:
            return {
                "key": key, "label": label, "status": "inactive",
                "dataset_id": None, "source": None, "adjustment": None,
                "earliest_date": None, "latest_date": None, "max_date": None,
                "data_cutoff_date": None, "updated_to": None,
                "symbol_count": 0, "row_count": 0, "created_at": None,
                "data_policy": None,
            }
        earliest, latest = _date_range(manifest)
        return {
            "key": key, "label": label, "status": manifest.status,
            "dataset_id": manifest.dataset_id,
            "source": manifest.source, "adjustment": manifest.adjustment,
            "earliest_date": earliest, "latest_date": latest,
            "max_date": max_date,
            "data_cutoff_date": manifest.data_cutoff_date,
            "updated_to": manifest.data_cutoff_date or latest,
            "symbol_count": manifest.symbol_count,
            "row_count": manifest.row_count,
            "created_at": manifest.created_at,
            "data_policy": data_policy,
        }

    if pair is not None:
        l2_tile = _pair_tile("l2_product", "正式L2(未复权)",
                             pair.l2_manifest, pair.l2_max_date, pair.data_policy)
        l1_tile = _pair_tile("l1_product", "正式L1(前复权)",
                             pair.l1_manifest, pair.l1_max_date, pair.data_policy)
        product = {
            "l1": {
                "dataset_id": pair.l1_dataset_id,
                "data_cutoff_date": pair.l1_manifest.data_cutoff_date,
                "max_date": pair.l1_max_date,
                "data_policy": pair.data_policy,
            },
            "l2": {
                "dataset_id": pair.l2_dataset_id,
                "data_cutoff_date": pair.l2_manifest.data_cutoff_date,
                "max_date": pair.l2_max_date,
                "data_policy": pair.data_policy,
            },
            "active": True,
        }
    else:
        l2_tile = _pair_tile("l2_product", "正式L2(未复权)", None, None, None)
        l1_tile = _pair_tile("l1_product", "正式L1(前复权)", None, None, None)
        product = {"l1": None, "l2": None, "active": False}

    source_freshness = [
        _pick_source_freshness(
            "tushare", "Tushare日线",
            lambda d: d.source == "tushare" and d.adjustment == "none"
            and getattr(d, "period", "1d") in ("1d", "", None)
            # 退市池补充面也是 tushare/none/1d/ready，但不能冒充原始日线
            # （否则最新退市池会覆盖 raw 日线卡片，如 333 只显示为全市场）
            and not (d.universe_type or "").startswith("b1_delisted")
            # 纯 ETF/指数数据集与股票共用同一 scope，同样不能冒充股票地基
            # （手动 ETF 增量同步若比股票新一天，会覆盖 raw 卡为 2500 只）
            and _dataset_has_stocks(d),
        ),
        _pick_source_freshness(
            "factor", "Tushare前复权因子",
            lambda d: d.source == "tushare" and d.adjustment == "adj_factor"
            and (getattr(d, "dataset_type", "") or "") == "factor",
            latest_first=True,
            carry_freshness=True,
        ),
        l2_tile,
        l1_tile,
    ]

    result.update({
        "manifest_count": len(all_ds),
        "blob_count": blob_count,
        "total_size_bytes": total_size,
        "ready_dataset_count": len(ready),
        "partial_dataset_count": len(partial),
        "failed_dataset_count": len(failed),
        "total_bar_count": total_bars,
        "total_symbol_count": total_symbols,
        "datasets": datasets_info,
        "source_freshness": source_freshness,
        "product": product,
    })
    mdc["ts"] = now
    mdc["payload"] = result
    return result

def _latest_factor_manifest(store) -> Optional["DatasetManifest"]:
    """Latest tushare/adj_factor factor manifest regardless of status.

    Mirrors tushare_product._select_latest_tushare_factor_candidate: newest
    by data_cutoff_date -> symbol_count -> row_count -> created_at, no
    ready-only filter, blob integrity enforced. A freshly synced partial
    demoted by the freshness gate is the newest surface and must not be
    shadowed by an older ready factor.
    """
    from ..data.dataset_store import DatasetManifest

    candidates = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if m.source != "tushare" or (m.adjustment or "") != "adj_factor":
            continue
        if (m.dataset_type or "") != "factor":
            continue
        if any(r.blob_sha256 and not store.blob_exists(r.blob_sha256)
               for r in m.symbols):
            continue
        candidates.append(m)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
    )

@router.get("/api/v1/system/data-health")
def data_health(ctx: ApiContext = Depends(get_ctx)) -> dict:
    """Tushare-only product chain health (formal L1/L2 dates, real lineage).

    Reports the actual product dataset dates — never the universe-file max
    date masquerading as the backtest surface. Distinguishes current
    freshness from historical completeness (pre-2001 backfill is a separate
    channel).
    """
    cfg = ctx.cfg
    from ..data.dataset_store import DatasetStore
    from ..data.tushare_product import tushare_product_data_health

    md_root = cfg.market_data_root
    if not md_root.exists():
        return {
            "ok": False,
            "status": "stale",
            "error": "market data root missing",
            "market_data_root": str(md_root),
        }
    store = DatasetStore(md_root)

    # expected latest trading day: calendar-aware when the app calendar file
    # exists, otherwise a weekday fallback (plan 10.2 — never natural-day lag).
    # When the calendar itself is stale (its max < real dataset dates) the
    # weekday heuristic takes over so the report never claims an expected day
    # older than the actual data.
    calendar_dates: Optional[List[int]] = None
    expected: Optional[int] = None
    calendar_stale = False
    try:
        from ..data.calendar import TradeCalendar
        from ..data.tushare_product import (
            manifest_history_signals,
            select_tushare_base,
            select_tushare_factor,
        )

        try:
            cal = TradeCalendar.load(cfg.calendar_path)
        except Exception:
            try:
                cal = TradeCalendar.from_tdx(cfg.tdx_root)
            except Exception:
                # 无 TDX 部署(服务器没有通达信):从最新 ready 数据集推导
                # 交易日历(与回测 repo 模式同一来源,缓存于 storage/calendars)。
                cal = None
                try:
                    from ..data.calendar import build_calendar_from_dataset

                    _base_m = select_tushare_base(store)
                    if _base_m is not None:
                        cal, _ = build_calendar_from_dataset(
                            store,
                            _base_m.dataset_id,
                            cache_dir=Path(cfg.storage_root) / "calendars",
                        )
                except Exception:
                    cal = None
        if cal is not None and cal.dates:
            calendar_dates = [int(d) for d in cal.dates]
            import datetime as _dt

            now = _dt.datetime.now()
            today = now.date()
            completed_through = (
                today if now.hour >= 18 else today - _dt.timedelta(days=1)
            )
            completed_int = int(completed_through.strftime("%Y%m%d"))
            cand = [d for d in calendar_dates if d <= completed_int]
            if cand:
                expected = max(cand)
            # real data max (base/factor) — the calendar must never lag behind
            data_maxes: List[int] = []
            for _m in (select_tushare_base(store), select_tushare_factor(store)):
                if _m is not None:
                    _sig = manifest_history_signals(_m)
                    if _sig.max_last_date:
                        data_maxes.append(int(_sig.max_last_date))
            # only a calendar missing a date the data already has is stale;
            # data not yet caught up to today is the normal state and must
            # never discard the calendar for trading-day lag
            if not expected or (
                data_maxes and int(cal.dates[-1]) < max(data_maxes)
            ):
                from ..data.tushare_product import _last_weekday_on_or_before
                expected = _last_weekday_on_or_before(completed_int)
                calendar_stale = True
            if calendar_stale:
                # stale calendar cannot compute trading-day lag — fall back to
                # workday counting against the weekday expected date
                calendar_dates = None
    except Exception:
        calendar_dates = None
        expected = None

    # recent sync failures from sync_logs (last 20 runs, newest first)
    recent_errors: List[dict] = []
    try:
        logs = sorted(store.sync_logs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for lp in logs[:20]:
            try:
                log = json.loads(lp.read_text(encoding="utf-8"))
            except Exception:
                continue
            result = log.get("result") or log
            status = result.get("status") if isinstance(result, dict) else None
            if status in ("failed", "error", "partial"):
                entry = {
                    "sync_run_id": log.get("sync_run_id"),
                    "dataset_id": log.get("dataset_id"),
                    "status": status,
                    "error": result.get("error") if isinstance(result, dict) else None,
                    "log_file": lp.name,
                }
                if isinstance(result, dict):
                    # Carry the concrete failure detail the UI needs (partial
                    # derives report missing_factor + a missing list, raw syncs
                    # report counts); every key stays None when absent.
                    missing = result.get("missing")
                    entry.update({
                        "missing_factor": result.get("missing_factor"),
                        "missing_count": (
                            len(missing) if isinstance(missing, list)
                            else result.get("missing_count")
                        ),
                        "imported": result.get("imported"),
                        "eligible": result.get("eligible"),
                        "row_count": result.get("row_count"),
                        "failed": result.get("failed"),
                        "no_data": result.get("no_data"),
                        "warning": result.get("warning"),
                        "reason": result.get("reason"),
                    })
                    issues_sample = (
                        log.get("issues_sample")
                        or result.get("issues_sample")
                        or result.get("issues")
                    )
                else:
                    issues_sample = log.get("issues_sample")
                if issues_sample:
                    entry["issues_sample"] = [
                        str(x)[:200] for x in list(issues_sample)[:3]
                    ]
                recent_errors.append(entry)
    except Exception:
        pass

    health = tushare_product_data_health(
        store,
        expected_trading_day=expected,
        calendar_dates=calendar_dates,
        recent_sync_errors=recent_errors,
    )
    # P2-1b: report the LATEST factor surface regardless of status — the
    # health check inside tushare_product_data_health only looks at ready
    # factors, so a freshness-gate-blocked partial (the newest state) would
    # otherwise be hidden behind an older ready factor. The factor item
    # carries the gate state (blocked/passed/skipped) from provenance.
    try:
        latest_factor = _latest_factor_manifest(store)
        if latest_factor is not None:
            from ..data.tushare_product import manifest_history_signals

            sig = manifest_history_signals(latest_factor)
            prov = latest_factor.provenance or {}
            f = prov.get("freshness") or {}
            if not f and prov.get("freshness_gate") == "skipped_by_flag":
                gate = "skipped"
            else:
                gate = f.get("gate") or f.get("status")
            health.setdefault("current_freshness", {})["tushare_factor"] = {
                "key": "tushare_factor", "label": "Tushare adj_factor",
                "status": latest_factor.status,
                "dataset_id": latest_factor.dataset_id,
                "data_cutoff_date": latest_factor.data_cutoff_date,
                "max_date": sig.max_last_date,
                "symbol_count": sig.symbol_count,
                "row_count": sig.total_rows,
                "data_policy": prov.get("data_policy"),
                "fresh_symbol_ratio": f.get("fresh_symbol_ratio"),
                "stale_active_symbols": f.get("stale_active_symbols"),
                "freshness_gate": gate,
            }
    except Exception:
        pass
    health["ok"] = health["status"] in ("healthy", "warning")
    health["market_data_root"] = str(md_root)
    return health

@router.get("/api/v1/universe/summary")
def universe_summary(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    from ..data.universe import AShareUniverse

    n = 0
    sample: List[str] = []
    if cfg.universe_path.exists():
        try:
            u = AShareUniverse.load(cfg.universe_path)
            n = len(u)
            sample = u.codes()[:8]
        except Exception:
            n = 0
    cal_min = cal_max = None
    cal_count = 0
    if Path(cfg.calendar_path).exists():
        try:
            from ..data.calendar import TradeCalendar

            cal = TradeCalendar.load(cfg.calendar_path)
            cal_count = len(cal.dates)
            if cal.dates:
                cal_min = int(cal.dates[0])
                cal_max = int(cal.dates[-1])
        except Exception:
            pass
    # day bar storage range (from calendar / manifest if present)
    data_min = cal_min
    data_max = cal_max
    try:
        import json as _json
        man = Path(cfg.storage_root) / "manifest.json"
        if man.exists():
            md = _json.loads(man.read_text(encoding="utf-8"))
            data_min = md.get("min_date") or md.get("first") or data_min
            data_max = md.get("max_date") or md.get("last") or data_max
        calp = Path(cfg.calendar_path)
        if calp.exists():
            cd = _json.loads(calp.read_text(encoding="utf-8"))
            data_min = cd.get("first") or data_min
            data_max = cd.get("last") or data_max
    except Exception:
        pass
    min60 = {"available": False, "min_date": None, "max_date": None}
    try:
        from ..data.minline_reader import min60_coverage_summary
        min60 = min60_coverage_summary(
            cfg.tdx_root, sample or ["SSE.STK.600000", "SZSE.STK.000001"]
        )
    except Exception:
        pass
    return {
        "global_universe_count": n,
        "default_demo_codes": ["SSE.STK.600000", "SZSE.STK.000001"],
        "full_market_token": "ALL",
        "sample_codes": sample,
        "calendar_path": str(cfg.calendar_path),
        "calendar_exists": Path(cfg.calendar_path).exists(),
        "calendar_count": cal_count,
        "calendar_min": cal_min,
        "calendar_max": cal_max,
        "data_min_date": data_min,
        "data_max_date": data_max,
        "data_range_label": (
            f"{data_min} ~ {data_max}" if data_min and data_max else "鏈煡"
        ),
        "min60": min60,
    }

@router.get("/api/v1/calendar/range")
def calendar_range(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    _calendar_range_cache = ctx.calendar_range_cache
    """Year/month bounds for date dropdowns (trading calendar).

    max_date reflects the freshest ready dataset cutoff (so the backtest
    end-date dropdown tracks real data), not just the static calendar file.
    """
    import time as _time

    now = _time.time()
    cached = _calendar_range_cache.get("data")
    if cached and (now - float(_calendar_range_cache.get("ts") or 0)) < 60:
        return cached

    from ..data.calendar import TradeCalendar

    years: List[int] = []
    min_d = max_d = None
    if Path(cfg.calendar_path).exists():
        try:
            cal = TradeCalendar.load(cfg.calendar_path)
            if cal.dates:
                min_d = int(cal.dates[0])
                max_d = int(cal.dates[-1])
                years = sorted({d // 10000 for d in cal.dates})
        except Exception:
            pass
    if not years:
        # fallback range for UI
        years = list(range(2010, 2027))
        min_d = 20100104
        max_d = 20261231

    # Extend max_date to the freshest ready dataset cutoff so the UI
    # end-date dropdown matches the data the user actually synced.
    try:
        from ..data.dataset_store import DatasetStore
        from ..data.repository import MarketDataRepository

        if cfg.market_data_root.exists():
            store = DatasetStore(cfg.market_data_root)
            repo = MarketDataRepository(store)
            best_cut = 0
            for d in repo.list_datasets():
                if (d.status or "") not in ("ready", "partial"):
                    continue
                cut = int(d.data_cutoff_date or 0)
                if not cut:
                    lasts = [s.last_date for s in d.symbols if s.last_date]
                    cut = max(lasts) if lasts else 0
                if cut > best_cut:
                    best_cut = cut
            if best_cut and best_cut > int(max_d or 0):
                max_d = best_cut
                top_year = best_cut // 10000
                if top_year not in years:
                    years = sorted(set(years) | {top_year})
    except Exception:
        pass

    data = {
        "years": years,
        "min_date": min_d,
        "max_date": max_d,
        "months": list(range(1, 13)),
        "days": list(range(1, 32)),
    }
    _calendar_range_cache["data"] = data
    _calendar_range_cache["ts"] = now
    return data

@router.post("/api/v1/db/migrate")
def api_db_migrate(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    from ..service.db import migrate_runs_index_to_sqlite, db_path, init_db

    init_db(cfg)
    report = migrate_runs_index_to_sqlite(cfg)
    report["db_path"] = str(db_path(cfg))
    return report

@router.get("/api/v1/db/stats")
def api_db_stats(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    from ..service.db import count_runs_db, db_path, init_db, list_experiments

    init_db(cfg)
    return {
        "db_path": str(db_path(cfg)),
        "n_runs": count_runs_db(cfg),
        "n_experiments": len(list_experiments(cfg, limit=200)),
    }

@router.post("/api/v1/data-sync/start")
def data_sync_start(payload: SyncStartBody, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    import datetime
    today = int(datetime.date.today().strftime("%Y%m%d"))
    end_date = payload.end_date or today
    start_date = payload.start_date

    if payload.task not in ("tdx", "tushare", "factor", "derive", "ca", "reconcile"):
        raise HTTPException(400, f"未知任务类型: {payload.task}")

    # The EOD auto-sync child writes the same (tushare, none/adj_factor, 1d)
    # checkpoints and manifests the manual task would touch; refuse to start
    # while it is alive (the child itself also takes the SyncTaskLock).
    if payload.task in ("tushare", "factor"):
        from ..data.sync_lock import _pid_alive
        eod_state_path = PROJECT_ROOT / "storage" / "astock" / "eod_sync_state.json"
        try:
            eod_st = json.loads(eod_state_path.read_text(encoding="utf-8"))
        except Exception:
            eod_st = {}
        eod_pid = int(eod_st.get("sync_pid") or 0)
        if eod_pid > 0 and _pid_alive(eod_pid):
            raise HTTPException(
                409,
                "EOD 自动同步正在运行"
                f"（PID={eod_pid}，启动于 {eod_st.get('last_sync_started_at') or '?'}）。"
                "请等待其结束后再手动同步。",
            )

    if payload.task == "tdx":
        # Tushare-only policy: TDX is disabled in the default sync chain.
        # Return a structured skip WITHOUT touching the TDX client or
        # spawning any process (no silent remapping to Tushare).
        return {
            "ok": True,
            "task": "tdx",
            "skipped": "disabled_by_policy",
            "message": "通达信(TDX)已退出正式同步：系统为 Tushare-only 数据策略。"
                       "请使用 tushare/factor/derive 任务。",
        }
    if payload.task == "tushare":
        # Zero-config default chain (handled inside the script for the same
        # CLI args cron jobs already use): raw incremental -> adj_factor
        # incremental -> product reconcile. Only the full formal L1/L2 chain
        # reports success.
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "tushare", "--mode", "incremental", "--end-date", str(end_date)]
        if start_date:
            cmd += ["--start-date", str(start_date)]
    elif payload.task == "factor":
        # Tushare-only chain: adj_factor is always incremental (window fetch
        # + parent merge); a user-pinned start_date overrides the auto resume.
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "tushare",
               "--adjustment", "adj_factor", "--mode", "incremental",
               "--end-date", str(end_date)]
        if start_date:
            cmd += ["--start-date", str(start_date)]
        universe_file = _latest_factor_universe_file(ctx, )
        if not universe_file:
            raise HTTPException(400, "Tushare adj_factor sync requires --universe-file")
        cmd += ["--universe-file", universe_file]
    elif payload.task == "ca":
        _CA_SCRIPT = str(PROJECT_ROOT / "scripts" / "sync_ca_events.py")
        cmd = [sys.executable, "-u", _CA_SCRIPT, "--mode", "incremental", "--days", "90",
               "--storage-root", str(cfg.market_data_root)]
    elif payload.task == "reconcile":
        # Manual product merge (UI「手动合并计算」): backfill the delisted
        # pool (zero-config Gate B2) then reconcile the formal L1/L2 pair.
        # Runs offline against local data except the delisted roster fetch.
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "internal",
               "--mode", "reconcile", "--cutoff", str(end_date)]
    else:
        # Rebuild the composite signal plane (L1) from the latest ready
        # composite_none x adj_factor parents (script auto-resolves parents).
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "internal", "--mode", "derive",
               "--adjustment", "composite_tushare_factor_qfq", "--cutoff", str(end_date)]

    # Resume only when client asks; leftover checkpoint must not force resume.
    if payload.task in ("tdx", "tushare", "factor"):
        if payload.fresh:
            cmd += ["--fresh"]
        elif payload.resume:
            cmd += ["--resume"]
        elif _checkpoint_exists(ctx, payload.task):
            # A checkpoint left by an interrupted run (crash / killed EOD
            # child) makes the script fail closed with exit 1 unless
            # --resume/--fresh is passed; auto-resume instead of surprising
            # the user with a plain "sync failed" from a bare start.
            cmd += ["--resume"]
            print(f"[SYNC] 检测到残留 checkpoint（{CHECKPOINT_FILES[payload.task]}），"
                  "自动附加 --resume")

    # Claim under lock so concurrent POSTs cannot both start a sync.
    with _sync_lock:
        if _sync_state["running"]:
            raise HTTPException(409, f"同步任务正在进行中: {_sync_state['task']}")
        ctx.sync_state = {
            "running": True,
            "task": payload.task,
            "started_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "status": "running",
            "output": [f"[CMD] {' '.join(cmd)}"],
            "error": None,
            "progress_done": 0,
            "progress_total": 0,
            "progress_phase": "",
        }

    t = threading.Thread(
        target=_run_sync_process, args=(ctx, cmd, payload.task), daemon=True
    )
    t.start()
    return {
        "ok": True,
        "task": payload.task,
        "message": f"已启动: {payload.task}",
        "resume": bool(payload.resume and not payload.fresh),
        "fresh": bool(payload.fresh),
        "checkpoint_present": _checkpoint_exists(ctx, payload.task) if payload.task in CHECKPOINT_FILES else False,
    }

@router.get("/api/v1/eod-sync/status")
def eod_sync_status(ctx: ApiContext = Depends(get_ctx)) -> dict:
    """Auto EOD sync config + last trigger record (datastore page status card)."""
    # The health scan below (tushare_product_data_health) walks every
    # manifest + blob (seconds on large warehouses) and the config only
    # changes at process start, so cache the payload for a short TTL.
    # Note: dynamic fields (last_trigger_date/last_sync_started_at/
    # manual_running/auto_running) are frozen for the TTL window too —
    # acceptable for the status card, they self-heal on next expiry.
    esc = ctx.eod_sync_cache
    now = _time.time()
    if (
        esc.get("payload") is not None
        and now - esc.get("ts", 0.0) < 30.0
    ):
        return esc["payload"]
    import datetime as _dt
    import json as _json
    import os as _os

    cfg = ctx.cfg
    state_path = PROJECT_ROOT / "storage" / "astock" / "eod_sync_state.json"
    state: Dict[str, Any] = {}
    try:
        if state_path.exists():
            state = _json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    def _sane_state_value(v):
        # Future-dated or unparseable records are dirty (a crash can persist
        # a timestamp for a sync that never ran) and are dropped from the
        # payload; the caller flags them via state_suspect.
        if not v:
            return None
        s = str(v).strip()
        try:
            if len(s) >= 8 and s[:8].isdigit():
                d = _dt.datetime.strptime(s[:8], "%Y%m%d").date()
                if d > _dt.date.today():
                    return None
            else:
                dt = _dt.datetime.fromisoformat(s)
                if dt > _dt.datetime.now() + _dt.timedelta(minutes=10):
                    return None
        except ValueError:
            return None
        return v

    last_trigger_date = _sane_state_value(state.get("last_trigger_date"))
    last_sync_started_at = _sane_state_value(state.get("last_sync_started_at"))
    state_suspect = bool(
        (state.get("last_trigger_date") and last_trigger_date is None)
        or (state.get("last_sync_started_at") and last_sync_started_at is None)
    )

    def _flag(name: str, default: str = "1") -> bool:
        return _os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")

    health: Dict[str, Any] = {}
    try:
        from ..data.dataset_store import DatasetStore
        from ..data.tushare_product import tushare_product_data_health

        if cfg.market_data_root.exists():
            health = tushare_product_data_health(DatasetStore(cfg.market_data_root))
    except Exception:
        health = {}

    with ctx.sync_lock:
        manual_running = bool(ctx.sync_state.get("running"))

    pid = state.get("sync_pid")
    auto_running = None
    if pid:
        try:
            import psutil  # type: ignore

            auto_running = bool(psutil.pid_exists(int(pid)))
        except Exception:
            auto_running = None

    payload = {
        "ok": True,
        "enabled": _flag("ASTOCK_EOD_SYNC_ENABLED", "1"),
        "sync_time": _os.environ.get("ASTOCK_EOD_SYNC_TIME", "18:30"),
        "min_lag_days": int(_os.environ.get("ASTOCK_EOD_SYNC_MIN_LAG_DAYS", "1") or 1),
        "poll_seconds": int(_os.environ.get("ASTOCK_EOD_SYNC_POLL_SECONDS", "1800") or 1800),
        "startup_check": _flag("ASTOCK_EOD_SYNC_STARTUP", "1"),
        "last_trigger_date": last_trigger_date,
        "last_sync_started_at": last_sync_started_at,
        "last_reason": state.get("last_reason"),
        "last_sync_exit_code": state.get("last_sync_exit_code"),
        "last_sync_finished_at": state.get("last_sync_finished_at"),
        "retry_count": state.get("retry_count"),
        "pending_retry_at": state.get("pending_retry_at"),
        "sync_pid": pid,
        "state_suspect": state_suspect,
        "auto_running": auto_running,
        "manual_running": manual_running,
        "data_status": health.get("status"),
        "raw_lag": (health.get("trading_day_lag") or {}).get("raw"),
        "expected_latest_trading_day": health.get("expected_latest_trading_day"),
        "state_path": str(state_path),
    }
    if health:
        # Only cache a real health snapshot; a transient failure must not
        # stick for the TTL window.
        esc["ts"] = now
        esc["payload"] = payload
    return payload

@router.get("/api/v1/data-sync/status")
def data_sync_status(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    with _sync_lock:
        return {
            "running": _sync_state["running"],
            "task": _sync_state["task"],
            "status": _sync_state["status"],
            "started_at": _sync_state["started_at"],
            "finished_at": _sync_state["finished_at"],
            "error": _sync_state["error"],
            "progress_done": _sync_state.get("progress_done", 0),
            "progress_total": _sync_state.get("progress_total", 0),
            "progress_phase": _sync_state.get("progress_phase", ""),
            "output_tail": _sync_state["output"][-50:],
            "output_lines": len(_sync_state["output"]),
            "checkpoints": {t: _checkpoint_exists(ctx, t) for t in CHECKPOINT_FILES},
        }

@router.post("/api/v1/data-sync/stop")
def data_sync_stop(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    with _sync_lock:
        if not _sync_state["running"]:
            return {"ok": False, "message": "当前没有运行中的任务"}
        proc = _sync_proc.get("proc")
        if proc is None:
            return {"ok": False, "message": "进程句柄丢失"}
        _sync_state["status"] = "stopping"
        # Internal marker for the worker classifier (never part of the
        # status payload): rc!=0 + stop_requested -> stopped, rc==0 -> done.
        _sync_state["stop_requested"] = True
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        return {"ok": True, "message": "已停止同步任务"}
    except Exception as e:
        return {"ok": False, "message": f"停止失败: {e}"}

@router.get("/api/v1/ca-events/status")
def ca_events_status(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    ca_dir = cfg.market_data_root / "ca_events"
    meta_file = ca_dir / "_meta.json"
    if not meta_file.exists():
        return {"exists": False, "last_sync_at": None, "total_files": 0}
    try:
        meta_mtime = meta_file.stat().st_mtime
    except OSError:
        meta_mtime = None
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    n_files = meta.get("success", 0)
    if not n_files:
        if _ca_file_count_cache["mtime"] != meta_mtime:
            _ca_file_count_cache["count"] = sum(
                1 for p in ca_dir.glob("*.json") if p.name != "_meta.json"
            ) if ca_dir.exists() else 0
            _ca_file_count_cache["mtime"] = meta_mtime
        n_files = _ca_file_count_cache["count"]
    return {
        "exists": True,
        "last_sync_at": meta.get("last_sync_at"),
        "last_sync_mode": meta.get("last_sync_mode"),
        "sync_range": meta.get("sync_range"),
        "total_stocks": meta.get("total_stocks", 0),
        "success": meta.get("success", 0),
        "failed": meta.get("failed", 0),
        "total_files": n_files,
    }

@router.get("/api/v1/dashboard/overview")
def dashboard_overview(
    top: int = Query(8, ge=1, le=50),
    min_win_rate: float = Query(0.5, ge=0.0, le=1.0),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """Read-only key-findings dashboard: data health + sync + top findings.

    Composes existing route handlers (single source of truth, no duplicated
    logic). Every sub-block degrades gracefully so the dashboard renders even
    on a bare server (missing data root, no experiments, no watchlist data).
    """
    dc = ctx.dashboard_cache
    cache_key = (top, min_win_rate)
    if (
        dc.get("payload") is not None
        and dc.get("key") == cache_key
        and _time.time() - dc.get("ts", 0.0) < 30.0
    ):
        return dc["payload"]
    cfg = ctx.cfg
    md = market_data_status(ctx)
    try:
        sync = data_sync_status(ctx)
    except Exception:
        sync = {"running": False, "status": "unavailable"}
    try:
        ca = ca_events_status(ctx)
    except Exception:
        ca = {"exists": False}
    try:
        uni = universe_summary(ctx)
    except Exception:
        uni = {}

    findings: List[Dict[str, Any]] = []
    try:
        from ..service.db import experiment_findings_batch, list_experiments

        exps = list_experiments(cfg, limit=50)
        # Batch the per-experiment / per-variant SQLite lookups (N+1) into a
        # handful of IN() queries; same row shape as experiment_results_table.
        tables = experiment_findings_batch(cfg, [e["experiment_id"] for e in exps])
        for exp in exps:
            table = tables.get(exp["experiment_id"])
            if table is None:
                continue
            for row in table.get("rows") or []:
                m = row.get("metrics") or {}
                win_rate = m.get("win_rate")
                if win_rate is None:
                    continue
                try:
                    win_rate = float(win_rate)
                except (TypeError, ValueError):
                    continue
                if win_rate < min_win_rate:
                    continue
                tr = m.get("total_return")
                dd = m.get("max_drawdown")
                pr = m.get("payoff_ratio") or m.get("profit_loss_ratio")
                findings.append({
                    "experiment_id": table.get("experiment_id"),
                    "experiment_name": table.get("name"),
                    "variant_id": row.get("variant_id"),
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "signal_data_source": row.get("signal_data_source"),
                    "signal_adjustment": row.get("signal_adjustment"),
                    "total_return": tr,
                    "win_rate": win_rate,
                    "max_drawdown": dd,
                    "payoff_ratio": pr,
                    "n_round_trips": m.get("n_round_trips"),
                })
    except Exception:
        pass

    def _find_score(f: Dict[str, Any]) -> float:
        try:
            return (
                float(f.get("total_return") or 0)
                + 2.0 * float(f.get("win_rate") or 0)
                - float(f.get("max_drawdown") or 0)
            )
        except (TypeError, ValueError):
            return -1e9

    findings.sort(key=_find_score, reverse=True)
    findings = findings[:top]

    wl_count = None
    wl_symbols: List[str] = []
    try:
        from ..service.index_etf import watchlist

        items = watchlist(cfg, kind="all")
        wl_count = len(items)
        wl_symbols = [str(i.get("name") or i.get("code") or "") for i in items[:12]]
    except Exception:
        pass

    payload = {
        "ok": True,
        "generated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": {
            "data_root": md.get("data_root"),
            "exists": md.get("exists"),
            "ready_dataset_count": md.get("ready_dataset_count"),
            "partial_dataset_count": md.get("partial_dataset_count"),
            "failed_dataset_count": md.get("failed_dataset_count"),
            "total_bar_count": md.get("total_bar_count"),
            "total_symbol_count": md.get("total_symbol_count"),
            "source_freshness": md.get("source_freshness"),
            "product": md.get("product"),
        },
        "sync": {
            "running": bool(sync.get("running")),
            "status": sync.get("status"),
            "task": sync.get("task"),
            "last_finished_at": sync.get("finished_at"),
            "error": sync.get("error"),
        },
        "ca": {
            "exists": bool(ca.get("exists")),
            "last_sync_at": ca.get("last_sync_at"),
            "total_files": ca.get("total_files"),
        },
        "universe": {
            "count": uni.get("global_universe_count"),
            "calendar_count": uni.get("calendar_count"),
            "data_min_date": uni.get("data_min_date"),
            "data_max_date": uni.get("data_max_date"),
        },
        "findings": findings,
        "watchlist": {"count": wl_count, "symbols": wl_symbols},
    }
    dc["key"] = cache_key
    dc["ts"] = _time.time()
    dc["payload"] = payload
    return payload

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    return _html_page("dashboard.html", "AStock dashboard")

@router.get("/quick.html", response_class=HTMLResponse)
def quick_page(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    return _html_page("quick.html", "AStock quick query")

@router.get("/", response_class=HTMLResponse)
def index(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """Official main UI: V3 (index_v3.html). Rollback: serve index.html instead."""
    return _html_page("index_v3.html", "AStock")

@router.get("/legacy", response_class=HTMLResponse)
def index_legacy(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """Original V1 UI (index.html); kept for quick rollback and bookmarks."""
    return _html_page("index.html", "AStock legacy")

@router.get("/v2", response_class=HTMLResponse)
def index_v2(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """V2 transition UI (index_v2.html)."""
    return _html_page("index_v2.html", "AStock v2")

@router.get("/v3", response_class=HTMLResponse)
def index_v3(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """V3 formal UI (same shell as /)."""
    return _html_page("index_v3.html", "AStock v3")

@router.get("/v3/task-detail", response_class=HTMLResponse)
def index_v3_task_detail(ctx: ApiContext = Depends(get_ctx)) -> HTMLResponse:
    cfg = ctx.cfg
    _sync_state = ctx.sync_state
    _sync_proc = ctx.sync_proc
    _sync_lock = ctx.sync_lock
    """Independent task detail page; same SPA shell as V3 main."""
    return _html_page("index_v3.html", "AStock v3")
