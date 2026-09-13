"""卦象工作台路由（PLAN-BAGUA-UX-V1.1）：统一标的检索、可用日期、按规则筛选。

筛选是独立内存任务容器 + 单工作线程 + 有界等待队列，不与导出/同卦任务混用。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .context import ApiContext, get_ctx

router = APIRouter()

_MAX_SCREEN_RULES = 50
_MAX_SCREEN_CODES = 10000


class BaguaScreenBody(BaseModel):
    rule_ids: List[str] = Field(..., min_length=1)
    match_mode: str = "any"  # any | all
    # YYYY-MM-DD / YYYYMMDD；缺省 = 数据面最新交易日
    asof: Optional[str] = None
    # scope=picked 时必填（规范标识优先，兼容六位/带前缀形式）
    scope: str = "all"  # all | picked
    codes: Optional[List[str]] = None


@router.get("/api/v1/bagua/instruments")
def api_bagua_instruments(
    q: str = Query("", max_length=64, description="名称/代码/别名（空=仅指数ETF预设）"),
    limit: int = Query(20, ge=1, le=50),
    kinds: Optional[str] = Query(
        None, description="逗号分隔过滤：STK / IDX / ETF"
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    from ..service.screening import search_instruments

    kind_list = [k.strip().upper() for k in (kinds or "").split(",") if k.strip()]
    try:
        items = search_instruments(ctx.cfg, q, limit=limit, kinds=kind_list or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"instruments search failed: {e}") from e
    return {"ok": True, "count": len(items), "items": items}


@router.get("/api/v1/bagua/options")
def api_bagua_options(
    code: str = Query(..., min_length=4, description="规范标识或任意可识别代码"),
    adjust: str = Query("raw", description="raw | tushare_qfq（指数/ETF 固定 raw）"),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    from ..service.screening import instrument_options

    try:
        return {"ok": True, **instrument_options(ctx.cfg, code, adjust=adjust)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"bagua options failed: {e}") from e


@router.get("/api/v1/bagua/screen/rules")
def api_bagua_screen_rules(ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.screening import list_screen_rules

    return list_screen_rules(ctx)


@router.post("/api/v1/bagua/screen")
def api_bagua_screen(
    payload: BaguaScreenBody,
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    from ..service import screening
    from ..service.screening import (
        ScreenDataUnavailable,
        ScreenError,
        ScreenQueueFull,
        submit_screen_job,
    )

    if len(payload.rule_ids) > _MAX_SCREEN_RULES:
        raise HTTPException(400, f"rule_ids 数量超限（最多 {_MAX_SCREEN_RULES} 条）")
    for rid in payload.rule_ids:
        if not str(rid).strip() or len(str(rid)) > 128:
            raise HTTPException(400, "rule_ids 含空项或过长 ID")
    if payload.match_mode not in ("any", "all"):
        raise HTTPException(400, "match_mode 必须是 any 或 all")
    if payload.scope not in ("all", "picked"):
        raise HTTPException(400, "scope 必须是 all 或 picked")
    if payload.scope == "picked":
        if not payload.codes:
            raise HTTPException(400, "scope=picked 需要提供 codes（不能退化为全市场）")
        if len(payload.codes) > _MAX_SCREEN_CODES:
            raise HTTPException(400, f"codes 数量超限（最多 {_MAX_SCREEN_CODES}）")
    elif payload.codes:
        if len(payload.codes) > _MAX_SCREEN_CODES:
            raise HTTPException(400, f"codes 数量超限（最多 {_MAX_SCREEN_CODES}）")
    # 不可执行规则在提交时即拒绝（不收任务再失败）
    catalog = screening.list_screen_rules(ctx)
    by_id = {r["id"]: r for r in catalog["rules"]}
    for rid in payload.rule_ids:
        info = by_id.get(str(rid).strip())
        if info is None:
            raise HTTPException(400, f"规则不存在: {rid}")
        if not info["executable"]:
            raise HTTPException(400, f"规则不可筛选：{info['name']}（{info['reason']}）")
    # 数据面 / 日期预检：不可用直接拒绝（快速失败，不占用任务队列）
    try:
        screening.resolve_screen_asof(ctx.cfg, payload.asof)
    except screening.ScreenDataUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except screening.ScreenDateNotAvailable as e:
        raise HTTPException(
            400,
            str(e),
            headers={"X-Suggested-AsOf": str(e.suggested)},
        ) from e
    except screening.ScreenError as e:
        raise HTTPException(400, str(e)) from e
    try:
        return submit_screen_job(
            ctx,
            rule_ids=[str(r).strip() for r in payload.rule_ids],
            match_mode=payload.match_mode,
            asof=payload.asof,
            codes=payload.codes if payload.scope == "picked" else None,
            scope=payload.scope,
        )
    except ScreenQueueFull as e:
        raise HTTPException(429, str(e)) from e
    except ScreenDataUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except ScreenError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/v1/bagua/screen/jobs")
def api_bagua_screen_jobs(ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.screening import list_screen_jobs

    return list_screen_jobs(ctx)


@router.get("/api/v1/bagua/screen/jobs/{job_id}")
def api_bagua_screen_job(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.screening import get_screen_job

    job = get_screen_job(ctx, job_id)
    if not job:
        raise HTTPException(404, f"screen job not found: {job_id}")
    return job


@router.get("/api/v1/bagua/screen/jobs/{job_id}/result")
def api_bagua_screen_result(job_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.screening import get_screen_job

    job = get_screen_job(ctx, job_id, with_result=True)
    if not job:
        raise HTTPException(
            404, f"筛选任务记录不存在或已失效（服务可能重启过）: {job_id}"
        )
    if job.get("status") not in ("done", "error"):
        raise HTTPException(409, f"任务未完成: {job.get('status')}")
    return job
