"""Rules + yao routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .context import ApiContext, get_ctx

router = APIRouter()

class RuleCreate(BaseModel):
    name: str
    formula_text: str
    description: str = ""
    periods: Optional[List[str]] = None

class RuleUpdate(BaseModel):
    name: Optional[str] = None
    formula_text: Optional[str] = None
    description: Optional[str] = None

class RuleValidate(BaseModel):
    formula_text: str
    name: str = "draft"

@router.get("/api/v1/rules")
def api_list_rules(include_archived: bool = False, ctx: ApiContext = Depends(get_ctx)) -> List[dict]:
    cfg = ctx.cfg
    rules = ctx.rules
    return rules.list_rules(include_archived=include_archived)

@router.get("/api/v1/rules/{rule_id}")
def api_get_rule(rule_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    rules = ctx.rules
    try:
        return rules.get_rule(rule_id, include_formula=True)
    except KeyError:
        raise HTTPException(404, f"rule not found: {rule_id}") from None

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
            periods=payload.periods,
        )
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
