"""Bagua + gua routes."""
from __future__ import annotations

import threading as _bq_threading
import time as _bq_time
import uuid as _bq_uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .context import ApiContext, get_ctx

router = APIRouter()

class BaguaBatchBody(BaseModel):
    codes: Optional[List[str]] = None
    all_stocks: bool = False
    date: str
    period: str = "DAY"
    adjust: str = "tdx_front"
    limit: Optional[int] = None

class BaguaExportBody(BaseModel):
    codes: Optional[List[str]] = None
    all_stocks: bool = True
    date: str
    period: Optional[str] = None  # single period shortcut
    periods: Optional[List[str]] = None  # DAY/WEEK/MONTH multi-sheet
    adjust: str = "tdx_front"
    limit: Optional[int] = None

def _bq_normalize_periods(
    period: Optional[str] = None,
    periods: Optional[List[str]] = None,
) -> List[str]:
    out: List[str] = []
    for p in list(periods or []):
        t = str(p).strip()
        if t:
            out.append(t)
    if period:
        for p in str(period).replace(";", ",").split(","):
            t = p.strip()
            if t and t not in out:
                out.append(t)
    return out or ["DAY", "WEEK", "MONTH"]
def _bq_run_export_job(ctx: ApiContext, job_id: str, params: Dict[str, Any]) -> None:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    from ..service.bagua_query import export_bagua_multi_period_xlsx

    with _bq_export_lock:
        job = _bq_export_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _bq_time.time()
        job["message"] = "正在计算全市场卦象…"

    def _prog(info: Dict[str, Any]) -> None:
        with _bq_export_lock:
            j = _bq_export_jobs.get(job_id)
            if not j:
                return
            per = info.get("period") or ""
            done = info.get("done") or 0
            total = info.get("total") or 0
            pi = info.get("period_index") or 0
            pc = info.get("period_count") or 1
            j["progress"] = info
            j["message"] = (
                f"周期 {pi}/{pc} {per} · {done}/{total}"
                f"（成功 {info.get('ok_count', 0)} / 失败 {info.get('error_count', 0)}）"
            )

    try:
        path = export_bagua_multi_period_xlsx(
            cfg,
            date=params["date"],
            periods=params["periods"],
            adjust=params["adjust"],
            codes=params.get("codes"),
            all_stocks=bool(params.get("all_stocks")),
            limit=params.get("limit"),
            on_progress=_prog,
        )
        with _bq_export_lock:
            job = _bq_export_jobs.get(job_id)
            if job:
                job["status"] = "done"
                job["finished_at"] = _bq_time.time()
                job["path"] = str(path)
                job["filename"] = path.name
                job["message"] = "导出完成"
    except Exception as e:
        with _bq_export_lock:
            job = _bq_export_jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["finished_at"] = _bq_time.time()
                job["error"] = str(e)
                job["message"] = f"导出失败: {e}"
def _bq_start_export_job(ctx: ApiContext, 
    *,
    date: str,
    periods: List[str],
    adjust: str,
    codes: Optional[List[str]],
    all_stocks: bool,
    limit: Optional[int],
) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    job_id = "bqexp_" + _bq_uuid.uuid4().hex[:12]
    rec = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _bq_time.time(),
        "started_at": None,
        "finished_at": None,
        "message": "已排队",
        "date": date,
        "periods": periods,
        "adjust": adjust,
        "all_stocks": all_stocks,
        "codes_count": len(codes or []) if codes else None,
        "limit": limit,
        "path": None,
        "filename": None,
        "error": None,
    }
    with _bq_export_lock:
        # keep last 20 jobs
        if len(_bq_export_jobs) > 20:
            old = sorted(
                _bq_export_jobs.items(),
                key=lambda kv: float(kv[1].get("created_at") or 0),
            )[: max(0, len(_bq_export_jobs) - 15)]
            for k, _ in old:
                _bq_export_jobs.pop(k, None)
        _bq_export_jobs[job_id] = rec
    params = {
        "date": date,
        "periods": periods,
        "adjust": adjust,
        "codes": codes,
        "all_stocks": all_stocks,
        "limit": limit,
    }
    t = _bq_threading.Thread(
        target=_bq_run_export_job,
        args=(job_id, params),
        name=f"bagua-export-{job_id}",
        daemon=True,
    )
    t.start()
    return {"ok": True, "mode": "async", **{k: v for k, v in rec.items() if k != "path"}}
def _bq_should_async(all_stocks: bool, codes: Optional[List[str]], limit: Optional[int]) -> bool:
    if limit is not None and int(limit) <= 50:
        return False
    if all_stocks:
        return True
    n = len(codes or [])
    return n > 50

@router.get("/api/v1/gua/states")
def api_gua_states(
    search: Optional[str] = None,
    main_hexagram_id: Optional[int] = None,
    action_signal: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=384),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    from ..service.gua import list_states

    return list_states(
        cfg,
        search=search,
        main_hexagram_id=main_hexagram_id,
        action_signal=action_signal,
        page=page,
        page_size=page_size,
    )

@router.get("/api/v1/gua/hexagrams")
def api_gua_hexagrams(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    from ..service.gua import list_hexagrams, rule_version, load_kb

    kb = load_kb(cfg)
    return {
        "items": list_hexagrams(cfg),
        "rule_version": rule_version(cfg),
        "count_gua": kb.get("count_gua"),
        "count_yao": kb.get("count_yao"),
        "action_signal_counts": kb.get("action_signal_counts") or {},
        "empty_biangua_count": kb.get("empty_biangua_count"),
    }

@router.post("/api/v1/gua/preview")
def api_gua_preview(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    from ..service.gua import preview_filter

    if not isinstance(payload, dict):
        payload = {}
    gf = payload.get("gua_filter")
    if gf is None:
        gf = payload
    signal_preview = bool(
        payload.get("signal_preview")
        or payload.get("include_signal_preview")
        or payload.get("real_signals")
    )
    rule_ids = payload.get("rule_ids")
    codes = payload.get("codes")
    period = payload.get("period") or "DAY"
    start = payload.get("start")
    end = payload.get("end")
    max_codes = int(payload.get("max_codes") or 20)
    min_sample = int(payload.get("min_sample") or 30)
    try:
        return preview_filter(
            gf or {},
            cfg=cfg,
            signal_preview=signal_preview,
            rule_ids=rule_ids,
            codes=codes,
            period=period,
            start=start,
            end=end,
            max_codes=max_codes,
            min_sample=min_sample,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.get("/api/v1/bagua/watchlist")
def api_bagua_watchlist(
    kind: str = Query("all", description="all | index | etf"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """Index (沪深指数) and ETF presets with TDX data availability."""
    from ..service.index_etf import watchlist

    import time as _time

    key = (kind, str(cfg.market_data_root))
    if (
        _wl_cache["payload"] is not None
        and _wl_cache["key"] == key
        and _time.time() - _wl_cache["ts"] < 60.0
    ):
        return _wl_cache["payload"]
    try:
        items = watchlist(cfg, kind=kind)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua watchlist failed: {e}") from e
    payload = {
        "ok": True,
        "kind": kind,
        "count": len(items),
        "symbols": items,
        "note": "指数/ETF 无复权口径，卦象按未复权(raw)价格计算（通达信本地 day 文件）。",
    }
    _wl_cache["key"] = key
    _wl_cache["ts"] = _time.time()
    _wl_cache["payload"] = payload
    return payload

@router.get("/api/v1/bagua/constituents")
def api_bagua_constituents(
    code: str = Query(..., min_length=1, description="index/ETF code e.g. sh510300 / sz399006"),
    limit: Optional[int] = Query(None, ge=1, le=2000, description="max constituent count"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """Constituent stocks (成分股) of an index or ETF (via tracked index)."""
    from ..service.bagua_query import normalize_query_code
    from ..service.index_etf import resolve_constituents

    try:
        std = normalize_query_code(code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        return resolve_constituents(cfg, std, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua constituents failed: {e}") from e

@router.get("/api/v1/bagua/query")
def api_bagua_query(
    code: str = Query(..., min_length=1, description="stock code e.g. 600000 / sh600000"),
    date: str = Query(..., min_length=4, description="YYYY-MM-DD or YYYYMMDD"),
    period: str = Query("DAY", description="DAY | WEEK | MONTH"),
    adjust: str = Query(
        "tdx_front",
        description="tdx_front (通达信前复权) | tushare_qfq (Tushare前复权) | raw (未复权)",
    ),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """Query hexagram for one stock on a date (OHLC digit-sum algorithm)."""
    from ..service.bagua_query import query_bagua

    try:
        return query_bagua(cfg, code=code, date=date, period=period, adjust=adjust)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua query failed: {e}") from e

@router.post("/api/v1/bagua/batch/query")
def api_bagua_batch(payload: BaguaBatchBody = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """Multi-stock or full-market hexagram query (same OHLC algorithm)."""
    from ..service.bagua_query import batch_query_bagua

    if not payload.all_stocks and not payload.codes:
        raise HTTPException(400, "codes or all_stocks required")
    try:
        return batch_query_bagua(
            cfg,
            codes=payload.codes,
            all_stocks=payload.all_stocks,
            date=payload.date,
            period=payload.period,
            adjust=payload.adjust,
            limit=payload.limit,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua batch query failed: {e}") from e

@router.post("/api/v1/bagua/export")
def api_bagua_export(
    payload: BaguaExportBody = Body(...),
    async_mode: bool = Query(
        True,
        description="full-market defaults to background job; false forces sync",
    ),

    ctx: ApiContext = Depends(get_ctx),
):
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """Export bagua Excel. Full-market uses async job by default."""
    from ..service.bagua_query import export_bagua_multi_period_xlsx

    if not payload.all_stocks and not payload.codes:
        raise HTTPException(400, "codes or all_stocks required")
    periods = _bq_normalize_periods(payload.period, payload.periods)
    use_async = async_mode and _bq_should_async(
        payload.all_stocks, payload.codes, payload.limit
    )
    if use_async:
        return _bq_start_export_job(ctx, 
            date=payload.date,
            periods=periods,
            adjust=payload.adjust,
            codes=payload.codes,
            all_stocks=payload.all_stocks,
            limit=payload.limit,
        )
    try:
        path = export_bagua_multi_period_xlsx(
            cfg,
            date=payload.date,
            periods=periods,
            adjust=payload.adjust,
            codes=payload.codes,
            all_stocks=payload.all_stocks,
            limit=payload.limit,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua export failed: {e}") from e
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )

@router.get("/api/v1/bagua/export")
def api_bagua_export_get(
    date: str = Query(..., min_length=4, description="YYYY-MM-DD or YYYYMMDD"),
    period: str = Query(
        "DAY,WEEK,MONTH",
        description="comma-separated: DAY | WEEK | MONTH",
    ),
    adjust: str = Query("tdx_front"),
    all_stocks: bool = Query(True),
    codes: Optional[str] = Query(None, description="comma-separated codes if not all_stocks"),
    limit: Optional[int] = Query(None, ge=1),
    async_mode: bool = Query(True, description="full-market -> background job"),

    ctx: ApiContext = Depends(get_ctx),
):
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    """GET export: full-market returns async job JSON; small sets return xlsx."""
    from ..service.bagua_query import export_bagua_multi_period_xlsx

    code_list = None
    if codes:
        code_list = [c.strip() for c in codes.replace(";", ",").split(",") if c.strip()]
    if not all_stocks and not code_list:
        raise HTTPException(400, "codes or all_stocks required")
    periods = _bq_normalize_periods(period, None)
    use_async = async_mode and _bq_should_async(all_stocks, code_list, limit)
    if use_async:
        return _bq_start_export_job(ctx, 
            date=date,
            periods=periods,
            adjust=adjust,
            codes=code_list,
            all_stocks=all_stocks,
            limit=limit,
        )
    try:
        path = export_bagua_multi_period_xlsx(
            cfg,
            date=date,
            periods=periods,
            adjust=adjust,
            codes=code_list,
            all_stocks=all_stocks,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"bagua export failed: {e}") from e
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )

@router.get("/api/v1/bagua/export/jobs/{job_id}")
def api_bagua_export_job(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    with _bq_export_lock:
        job = _bq_export_jobs.get(job_id)
        if not job:
            raise HTTPException(404, f"export job not found: {job_id}")
        return {
            k: v
            for k, v in job.items()
            if k != "path"
        }

@router.get("/api/v1/bagua/export/jobs/{job_id}/download")
def api_bagua_export_download(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> FileResponse:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    with _bq_export_lock:
        job = _bq_export_jobs.get(job_id)
        if not job:
            raise HTTPException(404, f"export job not found: {job_id}")
        if job.get("status") != "done":
            raise HTTPException(409, f"job not ready: {job.get('status')}")
        path_s = job.get("path")
        filename = job.get("filename") or "bagua_export.xlsx"
    if not path_s:
        raise HTTPException(404, "export file missing")
    path = Path(path_s)
    if not path.exists():
        raise HTTPException(404, f"export file not found: {path}")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )

@router.post("/api/v1/gua/import")
async def api_gua_import(file: UploadFile = File(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    _bq_export_jobs = ctx.bq_export_jobs
    _bq_export_lock = ctx.bq_export_lock
    _wl_cache = ctx.wl_cache
    from ..service.gua import reimport_excel
    import tempfile
    import shutil

    suffix = Path(file.filename or "gua.xlsx").suffix or ".xlsx"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = Path(tmp.name)
        report = reimport_excel(tmp_path, cfg=cfg, archive_previous=True)
        return report
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
