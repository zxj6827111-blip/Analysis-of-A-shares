"""FastAPI server for A-stock frontend: rules + backtests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import AStockConfig, get_default_config
from .service.backtest import BacktestRequest, BacktestService
from .service.jobs import JobStore
from .service.rules import RuleService
from .service.runs import (
    compare_runs,
    list_runs,
    load_equity_curve,
    load_run_summary,
    read_artifact,
    delete_run,
)
from .forecast.service import ForecastService

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


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
    research_unadjusted: bool = False
    research_unconfirmed_formula: bool = False
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    account_mode: str = "portfolio"  # portfolio | per_symbol
    run_id: Optional[str] = None
    async_mode: bool = False


class ForecastBatchBody(BaseModel):
    codes: Optional[List[str]] = None
    all_stocks: bool = False
    limit: Optional[int] = None


def create_app(cfg: Optional[AStockConfig] = None) -> FastAPI:
    cfg = cfg or get_default_config()
    cfg.ensure_dirs()
    app = FastAPI(title="AStock Backtest Console", version="0.5.0")
    rules = RuleService(cfg)
    jobs = JobStore(cfg, max_workers=1)
    bt_svc = BacktestService(cfg)
    forecast = ForecastService(cfg)

    @app.get("/api/v1/health")
    def health() -> dict:
        return {
            "ok": True,
            "storage_root": str(cfg.storage_root),
            "output_root": str(cfg.output_root),
            "registry_path": str(cfg.registry_path),
            "registry_exists": Path(cfg.registry_path).exists(),
        }

    @app.get("/api/v1/rules")
    def api_list_rules(include_archived: bool = False) -> List[dict]:
        return rules.list_rules(include_archived=include_archived)

    @app.get("/api/v1/rules/{rule_id}")
    def api_get_rule(rule_id: str) -> dict:
        try:
            return rules.get_rule(rule_id, include_formula=True)
        except KeyError:
            raise HTTPException(404, f"rule not found: {rule_id}") from None

    @app.post("/api/v1/rules/validate")
    def api_validate(payload: RuleValidate) -> dict:
        return rules.validate_formula(payload.formula_text, name=payload.name)

    @app.post("/api/v1/rules")
    def api_create_rule(payload: RuleCreate) -> dict:
        try:
            return rules.create_rule(
                name=payload.name,
                formula_text=payload.formula_text,
                description=payload.description,
                periods=payload.periods,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.patch("/api/v1/rules/{rule_id}")
    def api_update_rule(rule_id: str, payload: RuleUpdate) -> dict:
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

    @app.delete("/api/v1/rules/{rule_id}")
    def api_delete_rule(
        rule_id: str,
        permanent: bool = Query(True, description="user rules hard-delete when true"),
    ) -> dict:
        try:
            return rules.delete_rule(rule_id, permanent=permanent)
        except KeyError:
            raise HTTPException(404, "rule not found") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/v1/rules/{rule_id}/restore")
    def api_restore_rule(rule_id: str) -> dict:
        try:
            return rules.restore_rule(rule_id)
        except KeyError:
            raise HTTPException(404, "rule not found") from None

    @app.get("/api/v1/universe/summary")
    def universe_summary() -> dict:
        from .data.universe import AShareUniverse

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
                from .data.calendar import TradeCalendar

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
            from .data.minline_reader import min60_coverage_summary
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

    @app.get("/api/v1/calendar/range")
    def calendar_range() -> dict:
        """Year/month bounds for date dropdowns (trading calendar)."""
        from .data.calendar import TradeCalendar

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
        return {
            "years": years,
            "min_date": min_d,
            "max_date": max_d,
            "months": list(range(1, 13)),
            "days": list(range(1, 32)),
        }

    @app.post("/api/v1/backtests")
    def api_backtest(payload: BacktestBody) -> dict:
        if not payload.rule_ids:
            raise HTTPException(400, "rule_ids required")
        codes = payload.codes
        if payload.use_full_market:
            codes = ["ALL"]
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
            research_unadjusted=payload.research_unadjusted,
            research_unconfirmed_formula=payload.research_unconfirmed_formula,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
            account_mode=payload.account_mode or "portfolio",
            run_id=payload.run_id,
        )
        if payload.async_mode:
            rec = jobs.submit(req)
            return {"mode": "async", **jobs.to_public(rec)}
        try:
            summary = bt_svc.run(req)
            return {"mode": "sync", **summary}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.get("/api/v1/backtests/jobs/queue")
    def api_jobs_queue() -> dict:
        """FIFO task-center snapshot: running + queued + recent.

        Must be registered BEFORE ``/jobs/{job_id}`` so ``queue`` is not
        captured as a job_id path parameter.
        """
        return jobs.queue_snapshot()

    @app.get("/api/v1/backtests/jobs")
    def api_jobs(limit: int = Query(50, ge=1, le=200)) -> List[dict]:
        return jobs.list_public(limit=limit)

    @app.get("/api/v1/backtests/jobs/{job_id}")
    def api_job(job_id: str) -> dict:
        try:
            return jobs.to_public(jobs.get(job_id))
        except KeyError:
            raise HTTPException(404, "job not found") from None

    @app.get("/api/v1/backtests/{run_id}")
    def api_run(run_id: str) -> dict:
        try:
            return load_run_summary(cfg, run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found") from None

    @app.get("/api/v1/backtests/{run_id}/equity")
    def api_run_equity(
        run_id: str, max_points: int = Query(4000, ge=50, le=20000)
    ) -> dict:
        try:
            points = load_equity_curve(cfg, run_id, max_points=max_points)
        except FileNotFoundError:
            raise HTTPException(404, "run not found") from None
        return {"run_id": run_id, "points": points, "n": len(points)}

    @app.post("/api/v1/runs/compare")
    def api_compare_runs(payload: Dict[str, Any] = Body(...)) -> dict:
        raw = payload.get("run_ids") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise HTTPException(400, "run_ids list required")
        try:
            return compare_runs(cfg, [str(x) for x in raw])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/v1/backtests/{run_id}/artifacts/{name}")
    def api_artifact(run_id: str, name: str):
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

    @app.get("/api/v1/runs")
    def api_runs(limit: int = Query(50, ge=1, le=200)) -> List[dict]:
        return list_runs(cfg, limit=limit)

    @app.delete("/api/v1/runs/{run_id}")
    def api_delete_run(
        run_id: str,
        remove_files: bool = Query(True, description="delete outputs folder too"),
    ) -> dict:
        try:
            return delete_run(cfg, run_id, remove_files=remove_files)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.delete("/api/v1/backtests/{run_id}")
    def api_delete_backtest_run(
        run_id: str,
        remove_files: bool = Query(True),
    ) -> dict:
        try:
            return delete_run(cfg, run_id, remove_files=remove_files)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(500, str(e)) from e



    # ----- Stage D: SQLite registry -----
    @app.post("/api/v1/db/migrate")
    def api_db_migrate() -> dict:
        from .service.db import migrate_runs_index_to_sqlite, db_path, init_db

        init_db(cfg)
        report = migrate_runs_index_to_sqlite(cfg)
        report["db_path"] = str(db_path(cfg))
        return report

    @app.get("/api/v1/db/stats")
    def api_db_stats() -> dict:
        from .service.db import count_runs_db, db_path, init_db, list_experiments

        init_db(cfg)
        return {
            "db_path": str(db_path(cfg)),
            "n_runs": count_runs_db(cfg),
            "n_experiments": len(list_experiments(cfg, limit=200)),
        }

    # ----- Stage E: experiment center -----
    @app.get("/api/v1/experiments/presets")
    def api_experiment_presets() -> dict:
        from .service.experiments import GUA_PRESETS, WEEKDAY_TEMPLATES

        return {
            "gua_presets": [
                {"key": k, "label": v.get("label")} for k, v in GUA_PRESETS.items()
            ],
            "weekday_templates": [
                {"key": k, "label": v.get("label")} for k, v in WEEKDAY_TEMPLATES.items()
            ],
            "default_max_variants": 50,
            "hard_max_variants": 500,
        }

    @app.post("/api/v1/experiments/estimate")
    def api_experiment_estimate(payload: dict = Body(...)) -> dict:
        """Preview grid size for legacy weekday_keys OR free axes.

        Response includes theoretical/rejected/actual/preview plus
        estimated_variants (alias of actual) for existing UI.
        """
        from .service.experiments import estimate_grid_from_payload

        try:
            return estimate_grid_from_payload(payload or {})
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/v1/experiments")
    def api_create_experiment(payload: dict = Body(...)) -> dict:
        from .service.experiments import create_experiment_from_grid

        try:
            return create_experiment_from_grid(
                cfg,
                name=str(payload.get("name") or "瀹為獙"),
                rule_ids=payload.get("rule_ids") or [],
                gua_keys=payload.get("gua_keys") or ["none"],
                weekday_keys=payload.get("weekday_keys"),
                stop_loss_list=payload.get("stop_loss_list"),
                period=payload.get("period") or "DAY",
                codes=payload.get("codes"),
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
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.get("/api/v1/experiments")
    def api_list_experiments(limit: int = Query(50, ge=1, le=200)) -> List[dict]:
        from .service.db import list_experiments

        return list_experiments(cfg, limit=limit)

    @app.get("/api/v1/experiments/{experiment_id}")
    def api_get_experiment(experiment_id: str) -> dict:
        from .service.db import get_experiment

        try:
            return get_experiment(cfg, experiment_id)
        except FileNotFoundError:
            raise HTTPException(404, "experiment not found") from None

    @app.post("/api/v1/experiments/{experiment_id}/start")
    def api_start_experiment(experiment_id: str) -> dict:
        from .service.experiments import get_runner

        try:
            return get_runner(cfg).start(experiment_id)
        except FileNotFoundError:
            raise HTTPException(404, "experiment not found") from None
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.post("/api/v1/experiments/{experiment_id}/cancel")
    def api_cancel_experiment(experiment_id: str) -> dict:
        from .service.experiments import get_runner
        from .service.db import get_experiment, update_experiment_status

        get_runner(cfg).cancel(experiment_id)
        try:
            update_experiment_status(cfg, experiment_id, "cancelled")
            return get_experiment(cfg, experiment_id)
        except FileNotFoundError:
            raise HTTPException(404, "experiment not found") from None

    @app.get("/api/v1/experiments/{experiment_id}/results")
    def api_experiment_results(experiment_id: str) -> dict:
        from .service.db import experiment_results_table

        try:
            return experiment_results_table(cfg, experiment_id)
        except FileNotFoundError:
            raise HTTPException(404, "experiment not found") from None

    @app.get("/api/v1/experiments/{experiment_id}/export.xlsx")
    def api_experiment_export(experiment_id: str):
        from .service.experiments import write_experiment_excel

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


    # ----- Phase 4: research platform (queue / trials / workers) -----
    def _research_platform():
        from .research.platform import ResearchPlatform
        root = Path(cfg.storage_root)
        return ResearchPlatform(root)

    @app.get("/api/v1/research/queue")
    def api_research_queue_stats() -> dict:
        plat = _research_platform()
        try:
            return {"ok": True, "stats": plat.queue_stats(), "workers": plat.worker_snapshot()}
        finally:
            plat.close()

    @app.post("/api/v1/research/tasks")
    def api_research_enqueue(payload: dict = Body(...)) -> dict:
        plat = _research_platform()
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

    @app.get("/api/v1/research/tasks/{task_id}")
    def api_research_task_get(task_id: str) -> dict:
        plat = _research_platform()
        try:
            tsk = plat.queue.get(task_id)
            if not tsk:
                raise HTTPException(404, "task not found")
            return {"ok": True, "task": tsk}
        finally:
            plat.close()

    @app.post("/api/v1/research/tasks/{task_id}/cancel")
    def api_research_task_cancel(task_id: str) -> dict:
        plat = _research_platform()
        try:
            ok = plat.cancel_trial(task_id)
            tsk = plat.queue.get(task_id)
            return {"ok": bool(ok), "task": tsk}
        finally:
            plat.close()

    @app.post("/api/v1/research/tasks/{task_id}/pause")
    def api_research_task_pause(task_id: str) -> dict:
        plat = _research_platform()
        try:
            ok = plat.pause(task_id)
            return {"ok": bool(ok), "task": plat.queue.get(task_id)}
        finally:
            plat.close()

    @app.post("/api/v1/research/tasks/{task_id}/resume")
    def api_research_task_resume(task_id: str) -> dict:
        plat = _research_platform()
        try:
            ok = plat.resume(task_id)
            return {"ok": bool(ok), "task": plat.queue.get(task_id)}
        finally:
            plat.close()

    @app.post("/api/v1/research/workers/reclaim")
    def api_research_reclaim(payload: dict = Body(...)) -> dict:
        plat = _research_platform()
        try:
            timeout = int((payload or {}).get("timeout_sec") or 60)
            n = plat.reclaim_stale(timeout)
            return {"ok": True, "reclaimed": n, "stats": plat.queue_stats()}
        finally:
            plat.close()

    @app.get("/api/v1/research/trials/{trial_id}")
    def api_research_trial_get(trial_id: str) -> dict:
        plat = _research_platform()
        try:
            tr = plat.trial_store.get(trial_id)
            if not tr:
                raise HTTPException(404, "trial not found")
            return {"ok": True, "trial": tr}
        finally:
            plat.close()


    @app.post("/api/v1/research/evaluate")
    def api_research_evaluate(payload: dict = Body(...)) -> dict:
        """Phase 5: evaluation center — rank / pareto / gua gains / heatmaps."""
        from .research.evaluation import evaluate_trials

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

    # ----- Phase 6: search / schedules / drift / summary -----
    @app.post("/api/v1/research/search")
    def api_research_search(payload: dict = Body(...)) -> dict:
        """Budgeted parameter search (grid / random / staged)."""
        from .research.continuous import run_budgeted_search

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

    @app.get("/api/v1/research/schedules")
    def api_research_schedules() -> dict:
        from .research.schedules import list_schedules

        items = list_schedules()
        return {"ok": True, "schedules": items, "names": [s["name"] for s in items]}

    @app.get("/api/v1/research/schedules/due")
    def api_research_schedules_due(now: Optional[str] = Query(None)) -> dict:
        from datetime import datetime

        from .research.schedule_runner import ScheduleBeatStore, due_schedules

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

    @app.post("/api/v1/research/schedules/{name}/fire")
    def api_research_schedule_fire(name: str, payload: Optional[dict] = Body(None)) -> dict:
        from datetime import datetime

        from .research.schedule_runner import ScheduleBeatStore, fire_schedule

        body = payload if isinstance(payload, dict) else {}
        dry_run = bool(body.get("dry_run") or body.get("dryRun"))
        store_path = Path(cfg.storage_root) / "schedule_beat.json"
        store = ScheduleBeatStore(store_path)
        plat = _research_platform()
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

    @app.post("/api/v1/research/drift/monitor")
    def api_research_drift_monitor(payload: dict = Body(...)) -> dict:
        from .research.data_update_trigger import monitor_drift_and_alert

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

    @app.post("/api/v1/research/cross_section")
    def api_research_cross_section(payload: dict = Body(...)) -> dict:
        from .research.cross_section import cross_section_summary, slice_metrics_by_board

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

    @app.post("/api/v1/research/drift")
    def api_research_drift(payload: dict = Body(...)) -> dict:
        from .research.drift import detect_drift

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

    @app.post("/api/v1/research/summary")
    def api_research_summary(payload: dict = Body(...)) -> dict:
        from .research.reports_auto import build_research_summary
        from .research.evaluation import evaluate_trials

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

    # ----- Forecast module (isolated) -----


    # ---- Gua (hexagram) catalogue & filter preview ----
    @app.get("/api/v1/gua/states")
    def api_gua_states(
        search: Optional[str] = None,
        main_hexagram_id: Optional[int] = None,
        action_signal: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=384),
    ) -> dict:
        from .service.gua import list_states

        return list_states(
            cfg,
            search=search,
            main_hexagram_id=main_hexagram_id,
            action_signal=action_signal,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v1/gua/hexagrams")
    def api_gua_hexagrams() -> dict:
        from .service.gua import list_hexagrams, rule_version, load_kb

        kb = load_kb(cfg)
        return {
            "items": list_hexagrams(cfg),
            "rule_version": rule_version(cfg),
            "count_gua": kb.get("count_gua"),
            "count_yao": kb.get("count_yao"),
            "action_signal_counts": kb.get("action_signal_counts") or {},
            "empty_biangua_count": kb.get("empty_biangua_count"),
        }

    @app.post("/api/v1/gua/preview")
    def api_gua_preview(payload: dict = Body(...)) -> dict:
        from .service.gua import preview_filter

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

    @app.get("/api/v1/backtests/{run_id}/bagua-metrics")
    def api_run_bagua_metrics(run_id: str) -> dict:
        from .service.gua import run_bagua_metrics_for_run

        try:
            return run_bagua_metrics_for_run(cfg, run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found") from None
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.post("/api/v1/gua/import")
    async def api_gua_import(file: UploadFile = File(...)) -> dict:
        from .service.gua import reimport_excel
        import tempfile
        import shutil

        suffix = Path(file.filename or "gua.xlsx").suffix or ".xlsx"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = Path(tmp.name)
        try:
            report = reimport_excel(tmp_path, cfg=cfg, archive_previous=True)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return report

    @app.get("/api/v1/forecast/health")
    def forecast_health() -> dict:
        return forecast.health()

    @app.post("/api/v1/forecast/kb/import")
    async def forecast_kb_import(
        file: UploadFile = File(...),
        activate: bool = Query(True),
    ) -> dict:
        from tempfile import NamedTemporaryFile

        suffix = Path(file.filename or "kb.xlsx").suffix or ".xlsx"
        try:
            with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                data = await file.read()
                tmp.write(data)
                tmp_path = Path(tmp.name)
            return forecast.import_kb_xlsx(tmp_path, activate=activate)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    @app.post("/api/v1/forecast/kb/seed")
    def forecast_kb_seed() -> dict:
        try:
            return forecast.seed_kb_from_backtest()
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/v1/forecast/kb/versions")
    def forecast_kb_versions() -> list:
        return forecast.list_kb_versions()

    @app.post("/api/v1/forecast/kb/activate/{version_id}")
    def forecast_kb_activate(version_id: str) -> dict:
        try:
            return forecast.activate_kb_version(version_id)
        except FileNotFoundError:
            raise HTTPException(404, f"version not found: {version_id}") from None

    @app.post("/api/v1/forecast/weekly/upload")
    async def forecast_weekly_upload(
        file: UploadFile = File(...),
        activate: bool = Query(True),
    ) -> dict:
        from tempfile import NamedTemporaryFile

        suffix = Path(file.filename or "weekly.xlsx").suffix or ".xlsx"
        tmp_path = None
        try:
            with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                data = await file.read()
                tmp.write(data)
                tmp_path = Path(tmp.name)
            # keep original name for week_key detection
            named = tmp_path.with_name(file.filename or tmp_path.name)
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

    @app.get("/api/v1/forecast/weekly")
    def forecast_weekly_list() -> dict:
        try:
            return forecast.list_weeks()
        except Exception as e:
            raise HTTPException(500, f"list weeks failed: {e}") from e

    @app.post("/api/v1/forecast/weekly/{week_key}/activate")
    def forecast_weekly_activate(week_key: str) -> dict:
        try:
            return forecast.activate_week(week_key)
        except FileNotFoundError:
            raise HTTPException(404, f"week not found: {week_key}") from None

    @app.get("/api/v1/forecast/search")
    def forecast_search(
        q: str = Query(..., min_length=1),
        limit: int = Query(20, ge=1, le=50),
    ) -> list:
        return forecast.search(q, limit=limit)

    @app.get("/api/v1/forecast/quote")
    def forecast_quote(
        code: Optional[str] = Query(None),
        q: Optional[str] = Query(None),
    ) -> dict:
        query = (code or q or "").strip()
        if not query:
            raise HTTPException(400, "code or q required")
        return forecast.quote(query)

    @app.post("/api/v1/forecast/batch/query")
    def forecast_batch(payload: ForecastBatchBody = Body(...)) -> dict:
        if not payload.all_stocks and not payload.codes:
            raise HTTPException(400, "codes or all_stocks required")
        return forecast.batch_query(
            payload.codes, all_stocks=payload.all_stocks, limit=payload.limit
        )

    @app.get("/api/v1/forecast/export")
    def forecast_export() -> FileResponse:
        try:
            path = forecast.export_xlsx()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=path.name,
        )


    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>AStock</h1><p>static/index.html missing</p>")

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, cfg: Optional[AStockConfig] = None) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="astock-serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tdx-root", default=None)
    p.add_argument("--storage", default=None)
    args = p.parse_args(argv)
    overrides: Dict[str, Any] = {}
    if args.tdx_root:
        overrides["tdx_root"] = Path(args.tdx_root)
    if args.storage:
        overrides["storage_root"] = Path(args.storage)
    cfg = get_default_config(**overrides) if overrides else get_default_config()
    serve(host=args.host, port=args.port, cfg=cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

