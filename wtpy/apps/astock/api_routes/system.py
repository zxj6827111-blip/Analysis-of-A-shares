"""System routes: health, market-data, sync, pages."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..version import get_version_info, get_version_string
from .context import ApiContext, get_ctx

router = APIRouter()

SYNC_SCRIPT = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py")
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

_calendar_range_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
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
def _latest_factor_universe_file(ctx: ApiContext, ) -> Optional[str]:
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
        if m.source != "tushare" or m.adjustment != "adj_factor":
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
            cwd=str(Path(__file__).resolve().parents[3]),
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
            if _sync_state["status"] == "stopping":
                _sync_state["status"] = "stopped"
                _sync_state["error"] = "用户手动停止"
            else:
                _sync_state["status"] = "done" if proc.returncode == 0 else "error"
                _sync_state["error"] = f"exit code {proc.returncode}" if proc.returncode != 0 else None
    except Exception as e:
        with _sync_lock:
            _sync_state["status"] = "error"
            _sync_state["error"] = str(e)
    finally:
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
            "datasets": [], "latest_local_vendor": None,
        })
        return result
    store = DatasetStore(md_root)
    repo = MarketDataRepository(store)
    all_ds = repo.list_datasets()
    ready = [d for d in all_ds if d.status == "ready"]
    partial = [d for d in all_ds if d.status == "partial"]
    failed = [d for d in all_ds if d.status == "failed"]
    total_bars = sum(d.row_count for d in all_ds)
    total_symbols = sum(d.symbol_count for d in all_ds)
    blob_dir = md_root / "blobs"
    total_size = 0
    blob_count = 0
    if blob_dir.exists():
        for f in blob_dir.iterdir():
            if f.is_file():
                total_size += f.stat().st_size
                blob_count += 1

    def _date_range(d):
        firsts = [s.first_date for s in d.symbols if s.first_date]
        lasts = [s.last_date for s in d.symbols if s.last_date]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)

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
        })

    def _pick_source_freshness(key, label, match_fn):
        """Pick best dataset for a UI source tile.

        ready > partial; then freshest cutoff, fullest symbol_count,
        most rows (blocks empty/tiny shells), then newest created_at.
        """
        cands = [
            d for d in all_ds
            if match_fn(d) and (d.status or "") not in ("superseded", "failed", "building")
        ]
        if not cands:
            # fall back: still show something if only superseded exists
            cands = [d for d in all_ds if match_fn(d)]
        if not cands:
            return {
                "key": key, "label": label, "status": "missing",
                "dataset_id": None, "earliest_date": None, "latest_date": None,
                "data_cutoff_date": None, "updated_to": None,
                "symbol_count": 0, "row_count": 0, "created_at": None,
                "source": None, "adjustment": None,
            }
        rank = {"ready": 3, "partial": 2, "building": 1, "failed": 0, "superseded": -1}

        def _score(d):
            earliest, latest = _date_range(d)
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
        return {
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

    source_freshness = [
        _pick_source_freshness(
            "tdx", "通达信",
            lambda d: d.source == "tdxquant" and d.adjustment == "front"
            and getattr(d, "period", "1d") in ("1d", "", None),
        ),
        _pick_source_freshness(
            "tushare", "Tushare日线",
            lambda d: d.source == "tushare" and d.adjustment in ("none", "qfq")
            and getattr(d, "period", "1d") in ("1d", "", None),
        ),
        _pick_source_freshness(
            "factor", "Tushare前复权因子",
            lambda d: d.source == "tushare" and d.adjustment == "adj_factor",
        ),
        _pick_source_freshness(
            "derive", "派生QFQ",
            lambda d: d.source == "internal" and d.adjustment == "tushare_factor_qfq",
        ),
    ]

    latest_lv = None
    lv_ready = [
        d for d in ready
        if d.source == "local_vendor"
        and (d.adjustment or "none") == "none"
        and int(d.row_count or 0) > 0
    ]
    if not lv_ready:
        lv_ready = [d for d in ready if d.source == "local_vendor"]
    # Execution dataset fallback: when no local_vendor raw dataset exists
    # (e.g. a test server without vendor data), show the best ready raw
    # `none` dataset so the UI's L2 execution panel is truthful instead of
    # blocking with "无ready执行数据集". Prefers the same family order as
    # backtest.py / experiments.py: tdx_local > tdxquant > tushare.
    if not lv_ready:
        for _fs in ("tdx_local", "tdxquant", "tushare"):
            _cands = [
                d for d in ready
                if d.source == _fs and (d.adjustment or "none") == "none"
                and int(d.row_count or 0) > 0
            ]
            if _cands:
                lv_ready = _cands
                break
    if lv_ready:
        d = max(
            lv_ready,
            key=lambda x: (
                int(x.data_cutoff_date or 0),
                int(x.symbol_count or 0),
                int(x.row_count or 0),
                x.created_at or "",
            ),
        )
        earliest, latest = _date_range(d)
        latest_lv = {
            "dataset_id": d.dataset_id,
            "source": d.source,
            "adjustment": d.adjustment,
            "status": d.status,
            "symbol_count": d.symbol_count,
            "row_count": d.row_count,
            "earliest_date": earliest,
            "latest_date": latest,
            "coverage_start_year": d.coverage_start_year,
            "coverage_end_year": d.coverage_end_year,
            "survivorship_bias": d.survivorship_bias,
            "warning_text": d.warning_text,
        }

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
        "latest_local_vendor": latest_lv,
    })
    return result

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

    if payload.task not in ("tdx", "tushare", "factor", "derive", "ca"):
        raise HTTPException(400, f"未知任务类型: {payload.task}")

    if payload.task == "tdx":
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "tdxquant", "--mode", "incremental", "--end-date", str(end_date), "--skip-ca-detect"]
    elif payload.task == "tushare":
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "tushare", "--mode", "incremental", "--end-date", str(end_date)]
        if start_date:
            cmd += ["--start-date", str(start_date)]
    elif payload.task == "factor":
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "tushare", "--adjustment", "adj_factor", "--mode", "full", "--end-date", str(end_date)]
        universe_file = _latest_factor_universe_file(ctx, )
        if not universe_file:
            raise HTTPException(400, "Tushare adj_factor sync requires --universe-file")
        cmd += ["--universe-file", universe_file]
    elif payload.task == "ca":
        _CA_SCRIPT = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_ca_events.py")
        cmd = [sys.executable, "-u", _CA_SCRIPT, "--mode", "incremental", "--days", "90",
               "--storage-root", str(cfg.market_data_root)]
    else:
        cmd = [sys.executable, "-u", SYNC_SCRIPT, "--source", "internal", "--mode", "derive",
               "--adjustment", "tushare_factor_qfq", "--cutoff", str(end_date)]

    # Resume only when client asks; leftover checkpoint must not force resume.
    if payload.task in ("tdx", "tushare", "factor"):
        if payload.fresh:
            cmd += ["--fresh"]
        elif payload.resume:
            cmd += ["--resume"]

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

    t = threading.Thread(target=_run_sync_process, args=(cmd, payload.task), daemon=True)
    t.start()
    return {
        "ok": True,
        "task": payload.task,
        "message": f"已启动: {payload.task}",
        "resume": bool(payload.resume and not payload.fresh),
        "fresh": bool(payload.fresh),
        "checkpoint_present": _checkpoint_exists(ctx, payload.task) if payload.task in CHECKPOINT_FILES else False,
    }

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
        from ..service.db import experiment_results_table, list_experiments

        for exp in list_experiments(cfg, limit=50):
            try:
                table = experiment_results_table(cfg, exp["experiment_id"])
            except Exception:
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

    return {
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
            "latest_local_vendor": md.get("latest_local_vendor"),
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
