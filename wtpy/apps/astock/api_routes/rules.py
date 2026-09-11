"""Rules + yao routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .context import ApiContext, get_ctx

router = APIRouter()

class RuleCreate(BaseModel):
    name: str = Field(..., max_length=64)
    formula_text: str
    description: str = Field("", max_length=500)
    category: str = Field("", max_length=20)
    periods: Optional[List[str]] = None

class RuleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    formula_text: Optional[str] = None
    description: Optional[str] = Field(None, max_length=500)
    category: Optional[str] = Field(None, max_length=20)

class RuleValidate(BaseModel):
    formula_text: str
    name: str = "draft"

class RuleImport(BaseModel):
    filename: str = Field(..., max_length=255)
    content: str

class RuleBatchValidate(BaseModel):
    ids: Optional[List[str]] = Field(None, max_length=200)

class RuleCategory(BaseModel):
    name: str

class BenchmarkBody(BaseModel):
    allow_research_proxy: bool = False

@router.get("/api/v1/rules")
def api_list_rules(
    include_archived: bool = False,
    include_hidden: bool = False,
    ctx: ApiContext = Depends(get_ctx),
) -> List[dict]:
    cfg = ctx.cfg
    rules = ctx.rules
    # include_hidden 供卦象导出勾选面板带出隐藏规则（735/5日外）；默认 False
    # 保持规则中心等其他调用方行为不变
    return rules.list_rules(include_archived=include_archived, include_hidden=include_hidden)

# 静态子路径必须注册在 GET /api/v1/rules/{rule_id} 之前，避免被 path 参数捕获
@router.post("/api/v1/rules/import")
def api_import_rule(payload: RuleImport, ctx: ApiContext = Depends(get_ctx)) -> dict:
    rules = ctx.rules
    try:
        return rules.import_rule(filename=payload.filename, content=payload.content)
    except FileExistsError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.post("/api/v1/rules/batch-validate")
def api_batch_validate(
    payload: RuleBatchValidate, ctx: ApiContext = Depends(get_ctx)
) -> dict:
    rules = ctx.rules
    return rules.batch_validate(payload.ids)

@router.get("/api/v1/rules/categories")
def api_list_categories(ctx: ApiContext = Depends(get_ctx)) -> dict:
    rules = ctx.rules
    return {"categories": rules.list_categories()}

@router.post("/api/v1/rules/categories")
def api_add_category(payload: RuleCategory, ctx: ApiContext = Depends(get_ctx)) -> dict:
    rules = ctx.rules
    try:
        cats = rules.add_category(payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"categories": cats}

@router.post("/api/v1/rules/validate")
def api_validate(payload: RuleValidate, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    return rules.validate_formula(payload.formula_text, name=payload.name)

@router.post("/api/v1/rules")
def api_create_rule(payload: RuleCreate, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.create_rule(
            name=payload.name,
            formula_text=payload.formula_text,
            description=payload.description,
            category=payload.category,
            periods=payload.periods,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.get("/api/v1/rules/benchmark-profile")
def api_benchmark_profile(ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.rule_benchmark import build_benchmark_profile

    return {"profile": build_benchmark_profile(ctx.cfg)}

@router.get("/api/v1/rules/{rule_id}")
def api_get_rule(rule_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.get_rule(rule_id, include_formula=True)
    except KeyError:
        raise HTTPException(404, f"rule not found: {rule_id}") from None

@router.get("/api/v1/rules/{rule_id}/performance")
def api_rule_performance(rule_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    from ..service.rule_benchmark import get_rule_performance

    try:
        return get_rule_performance(ctx, rule_id)
    except KeyError:
        raise HTTPException(404, f"rule not found: {rule_id}") from None

@router.post("/api/v1/rules/{rule_id}/benchmark")
def api_run_rule_benchmark(
    rule_id: str,
    payload: BenchmarkBody = BenchmarkBody(),
    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    from ..service.rule_benchmark import (
        BenchmarkQueueFullError,
        submit_rule_benchmark,
    )

    try:
        return submit_rule_benchmark(
            ctx, rule_id, allow_research_proxy=payload.allow_research_proxy
        )
    except KeyError:
        raise HTTPException(404, f"rule not found: {rule_id}") from None
    except BenchmarkQueueFullError as e:
        raise HTTPException(429, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.patch("/api/v1/rules/{rule_id}")
def api_update_rule(rule_id: str, payload: RuleUpdate, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.update_rule(
            rule_id,
            name=payload.name,
            formula_text=payload.formula_text,
            description=payload.description,
            category=payload.category,
        )
    except KeyError:
        raise HTTPException(404, "rule not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.delete("/api/v1/rules/{rule_id}")
def api_delete_rule(
    rule_id: str,
    permanent: bool = Query(True, description="user rules hard-delete when true"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.delete_rule(rule_id, permanent=permanent)
    except KeyError:
        raise HTTPException(404, "rule not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.post("/api/v1/rules/{rule_id}/restore")
def api_restore_rule(rule_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.restore_rule(rule_id)
    except KeyError:
        raise HTTPException(404, "rule not found") from None

@router.get("/api/v1/yao/rules")
def api_yao_rules(
    status: Optional[str] = Query(None),
    group: Optional[str] = Query(None),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    """List 爻辞规则 manifest for experiment center."""
    from ..service.yao_rules import load_yao_manifest, manifest_rules

    st = [s.strip() for s in (status or "").split(",") if s.strip()] or None
    gr = [s.strip() for s in (group or "").split(",") if s.strip()] or None
    man = load_yao_manifest()
    rules = manifest_rules(status=st, groups=gr)
    return {
        "ok": True,
        "version": man.get("version"),
        "exists": bool(man.get("exists")),
        "path": man.get("path"),
        "rules": rules,
        "count": len(rules),
    }
