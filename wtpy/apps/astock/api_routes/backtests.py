"""Backtest + run routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..service.backtest import BacktestRequest
from ..service.runs import (
    compare_runs,
    delete_run,
    list_runs,
    load_equity_curve,
    load_run_summary,
    read_artifact,
)
from .context import ApiContext, get_ctx

router = APIRouter()

class BacktestBody(BaseModel):
    rule_ids: List[str] = Field(default_factory=list)
    period: str = "DAY"
    hold: int = 1
    entry_lag: int = 1
    signal_weekdays: Optional[List] = None  # 1=Mon..7=Sun; empty=all
    buy_on: str = "open"  # open | close
    sell_on: str = "open"  # open | close
    buy_weekday: Optional[int] = None  # 1=Mon..7=Sun; overrides entry_lag
    exit_weekday: Optional[int] = None  # 1=Mon..7=Sun; overrides hold
    combine: Optional[str] = None
    codes: Optional[List[str]] = None
    use_full_market: bool = False
    start: Optional[int] = None
    end: Optional[int] = None
    dwm: bool = False
    with_bagua: bool = False
    bagua_filter_mode: Optional[str] = None  # default best3 when with_bagua
    gua_filter: Optional[dict] = None  # flexible hexagram filter
    bagua_period: str = "WEEK"  # product: weekly hexagram only
    bagua_price_plane: str = "raw"  # raw | tdx_front | tushare_qfq
    research_unadjusted: bool = False
    research_unconfirmed_formula: bool = False
    # None = auto: explicit cached events -> event_ledger, otherwise fail_closed.
    corporate_action_policy: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    account_mode: str = "portfolio"  # portfolio | per_symbol
    run_id: Optional[str] = None
    async_mode: bool = False
    signal_data_source: Optional[str] = None
    signal_adjustment: Optional[str] = None
    dataset_id: Optional[str] = None
    weekly_bar_mode: str = "local_aggregate"
    execution_data_source: str = "local_vendor"
    execution_dataset_id: Optional[str] = None
    # Gate B7: survivorship-safe chain
    baseline: Optional[str] = None  # "survivorship_safe" resolves the pinned combo
    universe_dataset_id: Optional[str] = None
    delist_exit_scenario: Optional[str] = None
    delist_recovery_discount: Optional[float] = None

@router.post("/api/v1/backtests")
def api_backtest(payload: BacktestBody, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    if not payload.rule_ids:
        raise HTTPException(400, "rule_ids required")
    codes = payload.codes
    if payload.use_full_market:
        codes = ["ALL"]
    # Gate B7: survivorship-safe baseline selector (explicit fields win;
    # missing baseline datasets -> 4xx, never a silent legacy fallback)
    _sig_src = payload.signal_data_source
    _sig_adj = payload.signal_adjustment
    _sig_ds = payload.dataset_id
    _exec_src = payload.execution_data_source or "local_vendor"
    _exec_ds = payload.execution_dataset_id
    _uni_ds = payload.universe_dataset_id
    _dl_scn = payload.delist_exit_scenario
    if payload.baseline:
        if payload.baseline != "survivorship_safe":
            raise HTTPException(
                400, f"unknown baseline: {payload.baseline!r}")
        from ..service.baseline import (
            BaselineUnavailableError,
            resolve_survivorship_safe_baseline,
        )

        try:
            _bl = resolve_survivorship_safe_baseline(
                cfg,
                delist_exit_scenario=_dl_scn,
                delist_recovery_discount=payload.delist_recovery_discount,
            )
        except BaselineUnavailableError as e:
            raise HTTPException(409, str(e)) from e
        _sig_src = _sig_src or _bl["signal_data_source"]
        _sig_adj = _sig_adj or _bl["signal_adjustment"]
        _sig_ds = _sig_ds or _bl["dataset_id"]
        # Unset / product-default exec → baseline pins (internal/composite_none).
        # Match experiments.py: bare local_vendor/tdx_local without an explicit
        # execution_dataset_id must not stick and mismatch the SS exec manifest.
        if not _exec_ds and _exec_src in (None, "", "tdx_local", "local_vendor"):
            _exec_src = _bl["execution_data_source"]
        _exec_ds = _exec_ds or _bl["execution_dataset_id"]
        _uni_ds = _uni_ds or _bl["universe_dataset_id"]
        _dl_scn = _dl_scn or _bl["delist_exit_scenario"]
    req = BacktestRequest(
        rule_ids=list(payload.rule_ids),
        period=payload.period,
        hold=payload.hold,
        entry_lag=payload.entry_lag,
        signal_weekdays=payload.signal_weekdays,
        buy_on=payload.buy_on,
        sell_on=payload.sell_on,
        buy_weekday=payload.buy_weekday,
        exit_weekday=payload.exit_weekday,
        combine=payload.combine,
        codes=codes,
        start=payload.start,
        end=payload.end,
        dwm=payload.dwm,
        with_bagua=payload.with_bagua,
        bagua_filter_mode=payload.bagua_filter_mode,
        gua_filter=payload.gua_filter,
        bagua_period=payload.bagua_period or "WEEK",
        bagua_price_plane=payload.bagua_price_plane or "raw",
        research_unadjusted=payload.research_unadjusted,
        research_unconfirmed_formula=payload.research_unconfirmed_formula,
        corporate_action_policy=payload.corporate_action_policy,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        account_mode=payload.account_mode or "portfolio",
        run_id=payload.run_id,
        signal_data_source=_sig_src,
        signal_adjustment=_sig_adj,
        dataset_id=_sig_ds,
        weekly_bar_mode=payload.weekly_bar_mode or "local_aggregate",
        execution_data_source=_exec_src,
        execution_dataset_id=_exec_ds,
        universe_dataset_id=_uni_ds,
        delist_exit_scenario=_dl_scn,
        delist_recovery_discount=payload.delist_recovery_discount,
    )
    from ..data.dataset_binding import DatasetBindingError
    from ..data.repository import DatasetNotFoundError, DatasetNotReadyError

    if payload.async_mode:
        # Gate C D1/D4: validate dataset bindings BEFORE creating the job
        # so mismatches are rejected with 4xx and no run is created.
        if req.signal_data_source in ("tdxquant", "tushare", "internal", "raw"):
            from ..service.backtest import (
                resolve_market_data_bindings,
                select_universe,
            )

            try:
                resolve_market_data_bindings(
                    cfg, req, select_universe(cfg, req.codes)
                )
            except DatasetBindingError as e:
                raise HTTPException(e.http_status, e.to_payload()) from e
            except DatasetNotFoundError as e:
                raise HTTPException(404, str(e)) from e
            except DatasetNotReadyError as e:
                raise HTTPException(400, str(e)) from e
        rec = jobs.submit(req)
        return {"mode": "async", **jobs.to_public(rec)}
    try:
        summary = bt_svc.run(req)
        return {"mode": "sync", **summary}
    except DatasetBindingError as e:
        raise HTTPException(e.http_status, e.to_payload()) from e
    except DatasetNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except DatasetNotReadyError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/v1/backtests/jobs/queue")
def api_jobs_queue(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    """FIFO task-center snapshot: running + queued + recent.

    Must be registered BEFORE ``/jobs/{job_id}`` so ``queue`` is not
    captured as a job_id path parameter.
    """
    return jobs.queue_snapshot()

@router.get("/api/v1/backtests/jobs")
def api_jobs(limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_ctx)) -> List[dict]:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    return jobs.list_public(limit=limit)

@router.get("/api/v1/backtests/jobs/{job_id}")
def api_job(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        return jobs.to_public(jobs.get(job_id))
    except KeyError:
        raise HTTPException(404, "job not found") from None

@router.post("/api/v1/backtests/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    """Cancel a queued or running async backtest job."""
    try:
        rec = jobs.cancel(job_id)
        return {"ok": True, **jobs.to_public(rec)}
    except KeyError:
        raise HTTPException(404, "job not found") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/v1/backtests/{run_id}")
def api_run(run_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        return load_run_summary(cfg, run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found") from None

@router.get("/api/v1/backtests/{run_id}/equity")
def api_run_equity(
    run_id: str, max_points: int = Query(4000, ge=50, le=20000)
,
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        points = load_equity_curve(cfg, run_id, max_points=max_points)
    except FileNotFoundError:
        raise HTTPException(404, "run not found") from None
    return {"run_id": run_id, "points": points, "n": len(points)}

@router.post("/api/v1/runs/compare")
def api_compare_runs(payload: Dict[str, Any] = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    raw = payload.get("run_ids") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise HTTPException(400, "run_ids list required")
    try:
        return compare_runs(cfg, [str(x) for x in raw])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e

@router.get("/api/v1/backtests/{run_id}/artifacts/{name}")
def api_artifact(run_id: str, name: str, ctx: ApiContext = Depends(get_ctx)):
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        path = read_artifact(cfg, run_id, name)
    except FileNotFoundError:
        raise HTTPException(404, "artifact not found") from None
    suffix = path.suffix.lower()
    if suffix == ".csv":
        media = "text/csv"
    elif suffix in (".xlsx", ".xlsm"):
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif suffix == ".json":
        media = "application/json"
    else:
        media = "application/octet-stream"
    return FileResponse(path, media_type=media, filename=path.name)

@router.get("/api/v1/runs")
def api_runs(limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_ctx)) -> List[dict]:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    return list_runs(cfg, limit=limit)

@router.delete("/api/v1/runs/{run_id}")
def api_delete_run(
    run_id: str,
    remove_files: bool = Query(True, description="delete outputs folder too"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        return delete_run(cfg, run_id, remove_files=remove_files)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.delete("/api/v1/backtests/{run_id}")
def api_delete_backtest_run(
    run_id: str,
    remove_files: bool = Query(True),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    try:
        return delete_run(cfg, run_id, remove_files=remove_files)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.get("/api/v1/backtests/{run_id}/bagua-metrics")
def api_run_bagua_metrics(run_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    jobs = ctx.jobs
    bt_svc = ctx.bt_svc
    from ..service.gua import run_bagua_metrics_for_run

    try:
        return run_bagua_metrics_for_run(cfg, run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e
