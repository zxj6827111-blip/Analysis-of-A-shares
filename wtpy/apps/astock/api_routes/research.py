"""Research platform routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .context import ApiContext, get_ctx

router = APIRouter()

def _research_platform(ctx: ApiContext, ):
    cfg = ctx.cfg
    from ..research.platform import ResearchPlatform
    root = Path(cfg.storage_root)
    return ResearchPlatform(root)

@router.get("/api/v1/research/queue")
def api_research_queue_stats(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        return {"ok": True, "stats": plat.queue_stats(), "workers": plat.worker_snapshot()}
    finally:
        plat.close()

@router.post("/api/v1/research/tasks")
def api_research_enqueue(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        qname = str(payload.get("queue") or "research")
        idem = payload.get("idempotency_key")
        experiment_id = str(payload.get("experiment_id") or "adhoc")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        if not params:
            params = {
                k: v
                for k, v in (payload or {}).items()
                if k
                not in (
                    "queue",
                    "idempotency_key",
                    "experiment_id",
                    "max_attempts",
                    "priority",
                    "params",
                    "payload",
                )
            }
        max_attempts = int(payload.get("max_attempts") or 3)
        priority = int(payload.get("priority") or 0)
        out = plat.enqueue_trial(
            experiment_id=experiment_id,
            params=params,
            queue=qname,
            idempotency_key=idem,
            max_attempts=max_attempts,
            priority=priority,
        )
        return {"ok": True, **out}
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    finally:
        plat.close()

@router.get("/api/v1/research/tasks/{task_id}")
def api_research_task_get(task_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        tsk = plat.queue.get(task_id)
        if not tsk:
            raise HTTPException(404, "task not found")
        return {"ok": True, "task": tsk}
    finally:
        plat.close()

@router.post("/api/v1/research/tasks/{task_id}/cancel")
def api_research_task_cancel(task_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        ok = plat.cancel_trial(task_id)
        tsk = plat.queue.get(task_id)
        return {"ok": bool(ok), "task": tsk}
    finally:
        plat.close()

@router.post("/api/v1/research/tasks/{task_id}/pause")
def api_research_task_pause(task_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        ok = plat.pause(task_id)
        return {"ok": bool(ok), "task": plat.queue.get(task_id)}
    finally:
        plat.close()

@router.post("/api/v1/research/tasks/{task_id}/resume")
def api_research_task_resume(task_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        ok = plat.resume(task_id)
        return {"ok": bool(ok), "task": plat.queue.get(task_id)}
    finally:
        plat.close()

@router.post("/api/v1/research/workers/reclaim")
def api_research_reclaim(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        timeout = int((payload or {}).get("timeout_sec") or 60)
        n = plat.reclaim_stale(timeout)
        return {"ok": True, "reclaimed": n, "stats": plat.queue_stats()}
    finally:
        plat.close()

@router.get("/api/v1/research/trials/{trial_id}")
def api_research_trial_get(trial_id: str, ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    plat = _research_platform(ctx, )
    try:
        tr = plat.trial_store.get(trial_id)
        if not tr:
            raise HTTPException(404, "trial not found")
        return {"ok": True, "trial": tr}
    finally:
        plat.close()

@router.post("/api/v1/research/evaluate")
def api_research_evaluate(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    """Phase 5: evaluation center — rank / pareto / gua gains / heatmaps."""
    from ..research.evaluation import evaluate_trials

    if not isinstance(payload, dict):
        payload = {}
    trials = payload.get("trials")
    if not isinstance(trials, list):
        raise HTTPException(400, "body.trials must be a list")
    rank_mode = str(payload.get("rank_mode") or "composite")
    hard_rules = payload.get("hard_rules")
    if hard_rules is not None and not isinstance(hard_rules, dict):
        hard_rules = None
    result = evaluate_trials(
        trials,
        rank_mode=rank_mode,
        hard_rules=hard_rules,
        heatmap_x=str(payload.get("heatmap_x") or "exit_weekday"),
        heatmap_y=str(payload.get("heatmap_y") or "sell_on"),
        heatmap_metric=str(payload.get("heatmap_metric") or "total_return"),
    )
    return {"ok": True, **result}

@router.post("/api/v1/research/search")
def api_research_search(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    """Budgeted parameter search (grid / random / staged)."""
    from ..research.continuous import run_budgeted_search

    if not isinstance(payload, dict):
        payload = {}
    space = payload.get("space") or payload.get("space_axes") or {}
    if not isinstance(space, dict):
        raise HTTPException(400, "body.space must be a dict of axes")
    method = str(payload.get("method") or "random")
    budget = int(payload.get("budget") or 20)
    seed = int(payload.get("seed") or 0)
    try:
        trials = run_budgeted_search(space, method=method, budget=budget, seed=seed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "ok": True,
        "method": method,
        "budget": budget,
        "seed": seed,
        "n": len(trials),
        "trials": trials,
    }

@router.get("/api/v1/research/schedules")
def api_research_schedules(ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..research.schedules import list_schedules

    items = list_schedules()
    return {"ok": True, "schedules": items, "names": [s["name"] for s in items]}

@router.get("/api/v1/research/schedules/due")
def api_research_schedules_due(now: Optional[str] = Query(None), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from datetime import datetime

    from ..research.schedule_runner import ScheduleBeatStore, due_schedules

    when = None
    if now:
        try:
            when = datetime.fromisoformat(str(now).replace("Z", ""))
        except ValueError as e:
            raise HTTPException(400, f"invalid now: {e}") from e
    store_path = Path(cfg.storage_root) / "schedule_beat.json"
    store = ScheduleBeatStore(store_path)
    due = due_schedules(when, store)
    return {
        "ok": True,
        "now": (when.isoformat() if when else datetime.utcnow().isoformat()),
        "due": due,
        "last_fire": store.all_last_fires(),
        "store": str(store_path),
    }

@router.post("/api/v1/research/schedules/{name}/fire")
def api_research_schedule_fire(name: str, payload: Optional[dict] = Body(None), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from datetime import datetime

    from ..research.schedule_runner import ScheduleBeatStore, fire_schedule

    body = payload if isinstance(payload, dict) else {}
    dry_run = bool(body.get("dry_run") or body.get("dryRun"))
    store_path = Path(cfg.storage_root) / "schedule_beat.json"
    store = ScheduleBeatStore(store_path)
    plat = _research_platform(ctx, )
    try:
        result = fire_schedule(
            name,
            plat,
            dry_run=dry_run,
            store=None if dry_run else store,
            now=datetime.utcnow(),
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    finally:
        plat.close()
    return {"ok": True, **result}

@router.post("/api/v1/research/drift/monitor")
def api_research_drift_monitor(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..research.data_update_trigger import monitor_drift_and_alert

    if not isinstance(payload, dict):
        payload = {}
    recent = payload.get("recent") or payload.get("recent_metrics") or {}
    baseline = payload.get("baseline") or payload.get("baseline_metrics") or {}
    if not isinstance(recent, dict) or not isinstance(baseline, dict):
        raise HTTPException(400, "recent and baseline must be metric dicts")
    thresholds = payload.get("thresholds")
    if thresholds is not None and not isinstance(thresholds, dict):
        thresholds = None
    result = monitor_drift_and_alert(
        recent, baseline, thresholds=thresholds, source=str(payload.get("source") or "api")
    )
    return result

@router.post("/api/v1/research/cross_section")
def api_research_cross_section(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..research.cross_section import cross_section_summary, slice_metrics_by_board

    if not isinstance(payload, dict):
        payload = {}
    symbol_metrics = payload.get("symbol_metrics")
    if not isinstance(symbol_metrics, list):
        raise HTTPException(400, "body.symbol_metrics must be a list")
    summary = cross_section_summary(symbol_metrics)
    return {
        "ok": True,
        **summary,
        "board_slices": slice_metrics_by_board(symbol_metrics),
    }

@router.post("/api/v1/research/drift")
def api_research_drift(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..research.drift import detect_drift

    if not isinstance(payload, dict):
        payload = {}
    recent = payload.get("recent") or payload.get("recent_metrics") or {}
    baseline = payload.get("baseline") or payload.get("baseline_metrics") or {}
    if not isinstance(recent, dict) or not isinstance(baseline, dict):
        raise HTTPException(400, "recent and baseline must be metric dicts")
    thresholds = payload.get("thresholds")
    if thresholds is not None and not isinstance(thresholds, dict):
        thresholds = None
    result = detect_drift(recent, baseline, thresholds=thresholds)
    return {"ok": True, **result}

@router.post("/api/v1/research/summary")
def api_research_summary(payload: dict = Body(...), ctx: ApiContext = Depends(get_ctx)) -> dict:
    cfg = ctx.cfg
    from ..research.reports_auto import build_research_summary
    from ..research.evaluation import evaluate_trials

    if not isinstance(payload, dict):
        payload = {}
    trials = payload.get("trials")
    if not isinstance(trials, list):
        raise HTTPException(400, "body.trials must be a list")
    experiment_id = payload.get("experiment_id")
    evaluate_result = payload.get("evaluate_result")
    if evaluate_result is None and trials:
        evaluate_result = evaluate_trials(trials)
    drift_result = payload.get("drift_result") or payload.get("drift")
    summary = build_research_summary(
        experiment_id,
        trials,
        evaluate_result=evaluate_result,
        drift_result=drift_result if isinstance(drift_result, dict) else None,
    )
    return {"ok": True, **summary}
