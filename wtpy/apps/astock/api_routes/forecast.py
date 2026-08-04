"""Forecast module routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .context import ApiContext, get_ctx

router = APIRouter()

class ForecastBatchBody(BaseModel):
    codes: Optional[List[str]] = None
    all_stocks: bool = False
    limit: Optional[int] = None

@router.get("/api/v1/forecast/health")
def forecast_health(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    return forecast.health()

@router.post("/api/v1/forecast/kb/import")
async def forecast_kb_import(
    file: UploadFile = File(...),
    activate: bool = Query(True),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    from tempfile import NamedTemporaryFile

    suffix = Path(file.filename or "kb.xlsx").suffix or ".xlsx"
    tmp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            data = await file.read()
            tmp.write(data)
            tmp_path = Path(tmp.name)
        return forecast.import_kb_xlsx(tmp_path, activate=activate)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

@router.post("/api/v1/forecast/kb/seed")
def forecast_kb_seed(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    try:
        return forecast.seed_kb_from_backtest()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e

@router.get("/api/v1/forecast/kb/versions")
def forecast_kb_versions(ctx: ApiContext = Depends(get_ctx)) -> list:
    cfg = ctx.cfg
    forecast = ctx.forecast
    return forecast.list_kb_versions()

@router.post("/api/v1/forecast/kb/activate/{version_id}")
def forecast_kb_activate(version_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    try:
        return forecast.activate_kb_version(version_id)
    except FileNotFoundError:
        raise HTTPException(404, f"version not found: {version_id}") from None

@router.post("/api/v1/forecast/weekly/upload")
async def forecast_weekly_upload(
    file: UploadFile = File(...),
    activate: bool = Query(True),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    from tempfile import NamedTemporaryFile

    suffix = Path(file.filename or "weekly.xlsx").suffix or ".xlsx"
    tmp_path = None
    try:
        with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            data = await file.read()
            tmp.write(data)
            tmp_path = Path(tmp.name)
        # keep original basename for week_key detection (no path segments)
        safe_name = Path(file.filename or tmp_path.name).name
        if not safe_name or safe_name in (".", ".."):
            safe_name = tmp_path.name
        named = tmp_path.with_name(safe_name)
        if named != tmp_path:
            named.write_bytes(tmp_path.read_bytes())
            tmp_path.unlink(missing_ok=True)
            tmp_path = named
        return forecast.upload_weekly(tmp_path, activate=activate)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

@router.get("/api/v1/forecast/weekly")
def forecast_weekly_list(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    try:
        return forecast.list_weeks()
    except Exception as e:
        raise HTTPException(500, f"list weeks failed: {e}") from e

@router.post("/api/v1/forecast/weekly/{week_key}/activate")
def forecast_weekly_activate(week_key: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    try:
        return forecast.activate_week(week_key)
    except FileNotFoundError:
        raise HTTPException(404, f"week not found: {week_key}") from None

@router.get("/api/v1/forecast/search")
def forecast_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),

    ctx: ApiContext = Depends(get_ctx),
) -> list:
    cfg = ctx.cfg
    forecast = ctx.forecast
    return forecast.search(q, limit=limit)

@router.get("/api/v1/forecast/quote")
def forecast_quote(
    code: Optional[str] = Query(None),
    q: Optional[str] = Query(None),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    query = (code or q or "").strip()
    if not query:
        raise HTTPException(400, "code or q required")
    return forecast.quote(query)

@router.post("/api/v1/forecast/batch/query")
def forecast_batch(payload: ForecastBatchBody = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    forecast = ctx.forecast
    if not payload.all_stocks and not payload.codes:
        raise HTTPException(400, "codes or all_stocks required")
    return forecast.batch_query(
        payload.codes, all_stocks=payload.all_stocks, limit=payload.limit
    )

@router.get("/api/v1/forecast/export")
def forecast_export(ctx: ApiContext = Depends(get_ctx)) -> FileResponse:
    cfg = ctx.cfg
    forecast = ctx.forecast
    try:
        path = forecast.export_xlsx()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )
