"""卦象工作台路由（PLAN-BAGUA-UX-V1.1）：统一标的检索、可用日期、按规则筛选。

筛选是独立内存任务容器 + 单工作线程 + 有界等待队列，不与导出/同卦任务混用。
cache-first：发布快照可完整服务时直接组装（不占队列）；未覆盖走原任务路径。
"""

from __future__ import annotations

import time
import uuid
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
    # True=绕过快照缓存强制现算（手动重算不落官方快照）
    force_recompute: bool = False


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


@router.get("/api/v1/bagua/screen/latest")
def api_bagua_screen_latest(
    rule_ids: Optional[str] = Query(
        None, description="逗号分隔规则 ID（缺省=快照全部规则）"
    ),
    match_mode: str = Query("any", description="any | all"),
    codes: Optional[str] = Query(
        None, description="逗号分隔规范标识（picked 范围；缺省=全市场）"
    ),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    """最近发布快照的筛选视图：前端进筛选页签的默认数据源（秒回）。

    不做规则可执行性预检（快照里有哪些规则是事实，缺的如实报 uncovered）。
    与 POST /screen 的 cache 分支共用 try_screen_from_snapshot——同快照
    同规则同范围必然同结果（契约 §A4 验收口径）。
    """
    from ..service import screening

    rid_list = [r.strip() for r in (rule_ids or "").split(",") if r.strip()]
    if match_mode not in ("any", "all"):
        raise HTTPException(400, "match_mode 必须是 any 或 all")
    code_list = None
    if codes is not None:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
    try:
        # latest：不指定 asof → resolve_screen_asof 默认取数据面最新交易日，
        # 与快照 week 对齐（快照周即最新已发布周）
        result = screening.try_screen_from_snapshot(
            ctx.cfg,
            rule_ids=rid_list or _snapshot_rule_ids(ctx),
            match_mode=match_mode,
            asof=None,
            codes=code_list,
        )
    except screening.ScreenError as e:
        raise HTTPException(400, str(e)) from e
    if result is None:
        return {
            "ok": True,
            "available": False,
            "reason": "no_published_snapshot",
            "message": "尚无已发布的预筛快照（周五链首次运行后可用）",
        }
    return {"ok": True, "available": True, **result}


def _snapshot_rule_ids(ctx: ApiContext) -> List[str]:
    """快照内全部规则 ID（latest 未显式指定规则时用）。"""
    from ..service import screen_snapshots as ss

    snap = ss.latest_published_snapshot(ctx.cfg)
    if snap is None:
        return []
    return [str(r.get("rule_id")) for r in snap.get("rules") or []]


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
        # cache-first：发布快照能完整服务本组规则时直接组装结果（秒回，
        # 不占任务队列）；picked 越界/规则缺失/公式已改 → 落回现算。
        # force_recompute=1 显式绕过（手动重算场景）。
        if not payload.force_recompute:
            cached = screening.try_screen_from_snapshot(
                ctx.cfg,
                rule_ids=[str(r).strip() for r in payload.rule_ids],
                match_mode=payload.match_mode,
                asof=payload.asof,
                codes=payload.codes if payload.scope == "picked" else None,
            )
            if cached is not None:
                job_id = "bqscr_" + uuid.uuid4().hex[:12]
                rec = {
                    "job_id": job_id,
                    "kind": "screen",
                    "status": "done",
                    "created_at": time.time(),
                    "created_hm": time.strftime("%H:%M:%S", time.localtime()),
                    "started_at": time.time(),
                    "finished_at": time.time(),
                    "message": (
                        f"筛选完成（快照缓存）：命中 {cached.get('matched_count', 0)} 只"
                        f"（数据日 {cached.get('asof')}）"
                    ),
                    "params": {
                        "rule_ids": [str(r).strip() for r in payload.rule_ids],
                        "match_mode": payload.match_mode,
                        "asof": (None if payload.asof in (None, "", "latest") else str(payload.asof)),
                        "codes": payload.codes if payload.scope == "picked" else None,
                        "scope": payload.scope,
                    },
                    "scope_summary": (
                        "全部 A 股" if payload.scope == "all"
                        else f"指定 {len(payload.codes or [])} 只股票"
                    ),
                    "rules_summary": "、".join(str(r).strip() for r in payload.rule_ids),
                    "asof_used": cached.get("asof"),
                    "result_status": "ok",
                    "progress": None,
                    "result": cached,
                    "error": None,
                    "cancelled": False,
                }
                with ctx.bq_screen_lock:
                    if len(ctx.bq_screen_jobs) >= 30:
                        finished = sorted(
                            (
                                (k, v)
                                for k, v in ctx.bq_screen_jobs.items()
                                if v.get("status") in ("done", "error")
                            ),
                            key=lambda kv: float(kv[1].get("finished_at") or 0),
                        )
                        for k, _v in finished[: max(0, len(finished) - 20)]:
                            ctx.bq_screen_jobs.pop(k, None)
                    ctx.bq_screen_jobs[job_id] = rec
                return {k: v for k, v in rec.items() if k != "result"}
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
