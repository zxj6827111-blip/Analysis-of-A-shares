"""Experiment center routes."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .context import ApiContext, get_ctx

router = APIRouter()

@router.get("/api/v1/experiments/presets")
def api_experiment_presets(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.experiments import GUA_PRESETS, WEEKDAY_TEMPLATES
    from ..service.yao_rules import (
        HOLD_TEMPLATE_DAYS,
        DEMO_CODES,
        load_yao_manifest,
        manifest_rules,
    )

    man = load_yao_manifest()
    confirmed = manifest_rules(status=["confirmed"])
    return {
        "gua_presets": [
            {"key": k, "label": v.get("label")} for k, v in GUA_PRESETS.items()
        ],
        "weekday_templates": [
            {"key": k, "label": v.get("label")} for k, v in WEEKDAY_TEMPLATES.items()
        ],
        "hold_templates": list(HOLD_TEMPLATE_DAYS),
        "demo_codes": list(DEMO_CODES),
        "yao_manifest": {
            "version": man.get("version"),
            "exists": man.get("exists"),
            "path": man.get("path"),
            "n_rules": len(man.get("rules") or []),
            "n_confirmed": len(confirmed),
            "rules": man.get("rules") or [],
        },
        "default_max_variants": 50,
        "hard_max_variants": __import__(
            "wtpy.apps.astock.service.experiments", fromlist=["HARD_MAX_VARIANTS"]
        ).HARD_MAX_VARIANTS,
    }

@router.post("/api/v1/experiments/estimate")
def api_experiment_estimate(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    """Preview grid size for legacy weekday_keys OR free axes.

    Response includes theoretical/rejected/actual/preview plus
    estimated_variants (alias of actual) for existing UI.
    """
    from ..service.experiments import estimate_grid_from_payload

    try:
        return estimate_grid_from_payload(payload or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

@router.post("/api/v1/experiments")
def api_create_experiment(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.experiments import create_experiment_from_grid
    from ..data.dataset_binding import DatasetBindingError

    try:
        return create_experiment_from_grid(
            cfg,
            name=str(payload.get("name") or "瀹為獙"),
            rule_ids=payload.get("rule_ids") or [],
            gua_keys=payload.get("gua_keys") or ["none"],
            weekday_keys=payload.get("weekday_keys"),
            stop_loss_list=payload.get("stop_loss_list"),
            period=payload.get("period") or "DAY",
            periods=payload.get("periods"),
            codes=payload.get("codes"),
            universe=payload.get("universe"),
            gua_filters=payload.get("gua_filters"),
            start=payload.get("start"),
            end=payload.get("end"),
            account_mode=payload.get("account_mode") or "portfolio",
            research_unadjusted=bool(payload.get("research_unadjusted")),
            max_variants=int(payload.get("max_variants") or 50),
            concurrency=int(payload.get("concurrency") or 1),
            force=bool(payload.get("force")),
            note=str(payload.get("note") or ""),
            signal_weekdays_options=payload.get("signal_weekdays_options"),
            buy_options=payload.get("buy_options"),
            sell_options=payload.get("sell_options"),
            take_profit_list=payload.get("take_profit_list"),
            holiday_policy=str(payload.get("holiday_policy") or "next_trading_day"),
            engine=str(payload.get("engine") or "fast"),
            artifact_level=str(payload.get("artifact_level") or "summary"),
            use_signal_cache=bool(
                True if payload.get("use_signal_cache") is None else payload.get("use_signal_cache")
            ),
            promote_top_n=int(payload.get("promote_top_n") if payload.get("promote_top_n") is not None else 3),
            promote_metric=str(payload.get("promote_metric") or "total_return"),
            signal_data_source=payload.get("signal_data_source"),
            signal_adjustment=payload.get("signal_adjustment"),
            dataset_id=payload.get("dataset_id"),
            weekly_bar_mode=str(payload.get("weekly_bar_mode") or "local_aggregate"),
            execution_data_source=str(payload.get("execution_data_source") or "internal"),
            execution_dataset_id=payload.get("execution_dataset_id"),
            dual_source_compare=bool(payload.get("dual_source_compare")),
            signal_variants=payload.get("signal_variants"),
            bagua_period=str(payload.get("bagua_period") or "WEEK"),
            bagua_price_plane=payload.get("bagua_price_plane"),
            bagua_price_planes=payload.get("bagua_price_planes"),
            baseline=payload.get("baseline"),
            universe_dataset_id=payload.get("universe_dataset_id"),
            delist_exit_scenario=payload.get("delist_exit_scenario"),
            delist_recovery_discount=payload.get("delist_recovery_discount"),
        )
    except DatasetBindingError as e:
        raise HTTPException(e.http_status, e.to_payload()) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        from ..service.baseline import BaselineUnavailableError

        if isinstance(e, BaselineUnavailableError):
            raise HTTPException(409, str(e)) from e
        raise HTTPException(500, str(e)) from e

@router.get("/api/v1/experiments")
def api_list_experiments(limit: int = Query(50, ge=1, le=200), ctx: ApiContext = Depends(get_ctx)) -> List[dict]:
    cfg = ctx.cfg
    from ..service.db import list_experiments

    return list_experiments(cfg, limit=limit)

@router.get("/api/v1/experiments/{experiment_id}")
def api_get_experiment(experiment_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.db import get_experiment

    try:
        return get_experiment(cfg, experiment_id)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None

@router.delete("/api/v1/experiments/variants/{variant_id}")
def api_delete_experiment_variant(
    variant_id: str,
    remove_runs: bool = Query(True, description="also delete linked backtest run/files"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    from ..service.db import delete_experiment_variant

    try:
        return delete_experiment_variant(cfg, variant_id, remove_runs=remove_runs)
    except FileNotFoundError:
        raise HTTPException(404, "variant not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.delete("/api/v1/experiments/{experiment_id}")
def api_delete_experiment(
    experiment_id: str,
    remove_runs: bool = Query(True, description="also delete linked backtest runs/files"),

    ctx: ApiContext = Depends(get_ctx),
) -> dict:
    cfg = ctx.cfg
    from ..service.db import delete_experiment
    from ..service.experiments import get_runner

    try:
        # best-effort cancel if still tracked by runner
        try:
            get_runner(cfg).cancel(experiment_id)
        except Exception:
            pass
        return delete_experiment(cfg, experiment_id, remove_runs=remove_runs)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.post("/api/v1/experiments/{experiment_id}/start")
def api_start_experiment(experiment_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.experiments import get_runner

    try:
        return get_runner(cfg).start(experiment_id)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e

@router.post("/api/v1/experiments/{experiment_id}/cancel")
def api_cancel_experiment(experiment_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.experiments import get_runner
    from ..service.db import get_experiment, update_experiment_status

    get_runner(cfg).cancel(experiment_id)
    try:
        update_experiment_status(cfg, experiment_id, "cancelled")
        return get_experiment(cfg, experiment_id)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None

@router.get("/api/v1/experiments/{experiment_id}/results")
def api_experiment_results(experiment_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..service.db import experiment_results_table

    try:
        return experiment_results_table(cfg, experiment_id)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None

@router.get("/api/v1/experiments/{experiment_id}/export.xlsx")
def api_experiment_export(experiment_id: str, ctx: ApiContext = Depends(get_ctx)):
    cfg = ctx.cfg
    from ..service.experiments import write_experiment_excel

    try:
        path = write_experiment_excel(cfg, experiment_id)
    except FileNotFoundError:
        raise HTTPException(404, "experiment not found") from None
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )
