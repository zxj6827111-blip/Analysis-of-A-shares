"""FastAPI server for A-stock frontend: rules + backtests."""

from __future__ import annotations

import json
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
from .version import get_version_info, get_version_string

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


class ForecastBatchBody(BaseModel):
    codes: Optional[List[str]] = None
    all_stocks: bool = False
    limit: Optional[int] = None


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


class SyncStartBody(BaseModel):
    task: str
    end_date: Optional[int] = None
    start_date: Optional[int] = None
    resume: bool = False
    fresh: bool = False


def create_app(cfg: Optional[AStockConfig] = None) -> FastAPI:
    cfg = cfg or get_default_config()
    cfg.ensure_dirs()
    app = FastAPI(
        title="AStock Backtest Console",
        version=get_version_string(),
    )
    rules = RuleService(cfg)
    # Parallel backtest jobs: default 6, hard cap 8 (env ASTOCK_BT_MAX_WORKERS).
    jobs = JobStore(cfg)
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
            "market_data_root": str(cfg.market_data_root),
            "market_data_root_is_external": cfg.market_data_root_is_external,
        }

    @app.get("/api/v1/version")
    def version() -> dict:
        import platform

        info = get_version_info()
        info["python_version"] = platform.python_version()
        info["platform"] = platform.platform()
        return info

    @app.get("/api/v1/market-data/status")
    def market_data_status() -> dict:
        from .data.dataset_store import DatasetStore
        from .data.repository import MarketDataRepository
        md_root = cfg.market_data_root
        is_test = not cfg.market_data_root_is_external
        result = {
            "data_root": str(md_root),
            "is_test_root": is_test,
            "astock_env": cfg.astock_env,
            "exists": md_root.exists(),
        }
        if not md_root.exists():
            result.update({
                "manifest_count": 0, "ready_dataset_count": 0,
                "datasets": [], "latest_local_vendor": None,
            })
            return result
        store = DatasetStore(md_root)
        repo = MarketDataRepository(store)
        all_ds = repo.list_datasets()
        ready = [d for d in all_ds if d.status == "ready"]
        partial = [d for d in all_ds if d.status == "partial"]
        failed = [d for d in all_ds if d.status == "failed"]
        total_bars = sum(d.row_count for d in all_ds)
        total_symbols = sum(d.symbol_count for d in all_ds)
        blob_dir = md_root / "blobs"
        total_size = 0
        blob_count = 0
        if blob_dir.exists():
            for f in blob_dir.iterdir():
                if f.is_file():
                    total_size += f.stat().st_size
                    blob_count += 1

        def _date_range(d):
            firsts = [s.first_date for s in d.symbols if s.first_date]
            lasts = [s.last_date for s in d.symbols if s.last_date]
            return (min(firsts) if firsts else None, max(lasts) if lasts else None)

        datasets_info = []
        for d in all_ds:
            earliest, latest = _date_range(d)
            datasets_info.append({
                "dataset_id": d.dataset_id,
                "source": d.source,
                "adjustment": d.adjustment,
                "status": d.status,
                "symbol_count": d.symbol_count,
                "row_count": d.row_count,
                "created_at": d.created_at,
                "earliest_date": earliest,
                "latest_date": latest,
                "data_cutoff_date": d.data_cutoff_date,
                "survivorship_bias": d.survivorship_bias,
                "universe_type": d.universe_type,
                "warning_text": d.warning_text,
            })

        def _pick_source_freshness(key, label, match_fn):
            """Pick best dataset for a UI source tile.

            ready > partial; then freshest cutoff, fullest symbol_count,
            most rows (blocks empty/tiny shells), then newest created_at.
            """
            cands = [
                d for d in all_ds
                if match_fn(d) and (d.status or "") not in ("superseded", "failed", "building")
            ]
            if not cands:
                # fall back: still show something if only superseded exists
                cands = [d for d in all_ds if match_fn(d)]
            if not cands:
                return {
                    "key": key, "label": label, "status": "missing",
                    "dataset_id": None, "earliest_date": None, "latest_date": None,
                    "data_cutoff_date": None, "updated_to": None,
                    "symbol_count": 0, "row_count": 0, "created_at": None,
                    "source": None, "adjustment": None,
                }
            rank = {"ready": 3, "partial": 2, "building": 1, "failed": 0, "superseded": -1}

            def _score(d):
                earliest, latest = _date_range(d)
                updated = d.data_cutoff_date or latest or 0
                return (
                    rank.get(d.status or "", -1),
                    int(updated or 0),
                    int(d.symbol_count or 0),
                    int(d.row_count or 0),
                    int(latest or 0),
                    d.created_at or "",
                )

            best = max(cands, key=_score)
            earliest, latest = _date_range(best)
            updated = best.data_cutoff_date or latest
            return {
                "key": key,
                "label": label,
                "status": best.status,
                "dataset_id": best.dataset_id,
                "source": best.source,
                "adjustment": best.adjustment,
                "earliest_date": earliest,
                "latest_date": latest,
                "data_cutoff_date": best.data_cutoff_date,
                "updated_to": updated,
                "symbol_count": best.symbol_count,
                "row_count": best.row_count,
                "created_at": best.created_at,
            }

        source_freshness = [
            _pick_source_freshness(
                "tdx", "通达信",
                lambda d: d.source == "tdxquant" and d.adjustment == "front"
                and getattr(d, "period", "1d") in ("1d", "", None),
            ),
            _pick_source_freshness(
                "tushare", "Tushare日线",
                lambda d: d.source == "tushare" and d.adjustment in ("none", "qfq")
                and getattr(d, "period", "1d") in ("1d", "", None),
            ),
            _pick_source_freshness(
                "factor", "Tushare前复权因子",
                lambda d: d.source == "tushare" and d.adjustment == "adj_factor",
            ),
            _pick_source_freshness(
                "derive", "派生QFQ",
                lambda d: d.source == "internal" and d.adjustment == "tushare_factor_qfq",
            ),
        ]

        latest_lv = None
        lv_ready = [
            d for d in ready
            if d.source == "local_vendor"
            and (d.adjustment or "none") == "none"
            and int(d.row_count or 0) > 0
        ]
        if not lv_ready:
            lv_ready = [d for d in ready if d.source == "local_vendor"]
        if lv_ready:
            d = max(
                lv_ready,
                key=lambda x: (
                    int(x.data_cutoff_date or 0),
                    int(x.symbol_count or 0),
                    int(x.row_count or 0),
                    x.created_at or "",
                ),
            )
            earliest, latest = _date_range(d)
            latest_lv = {
                "dataset_id": d.dataset_id,
                "status": d.status,
                "symbol_count": d.symbol_count,
                "row_count": d.row_count,
                "earliest_date": earliest,
                "latest_date": latest,
                "coverage_start_year": d.coverage_start_year,
                "coverage_end_year": d.coverage_end_year,
                "survivorship_bias": d.survivorship_bias,
                "warning_text": d.warning_text,
            }

        result.update({
            "manifest_count": len(all_ds),
            "blob_count": blob_count,
            "total_size_bytes": total_size,
            "ready_dataset_count": len(ready),
            "partial_dataset_count": len(partial),
            "failed_dataset_count": len(failed),
            "total_bar_count": total_bars,
            "total_symbol_count": total_symbols,
            "datasets": datasets_info,
            "source_freshness": source_freshness,
            "latest_local_vendor": latest_lv,
        })
        return result

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

    _calendar_range_cache: Dict[str, Any] = {"ts": 0.0, "data": None}

    @app.get("/api/v1/calendar/range")
    def calendar_range() -> dict:
        """Year/month bounds for date dropdowns (trading calendar).

        max_date reflects the freshest ready dataset cutoff (so the backtest
        end-date dropdown tracks real data), not just the static calendar file.
        """
        import time as _time

        now = _time.time()
        cached = _calendar_range_cache.get("data")
        if cached and (now - float(_calendar_range_cache.get("ts") or 0)) < 60:
            return cached

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

        # Extend max_date to the freshest ready dataset cutoff so the UI
        # end-date dropdown matches the data the user actually synced.
        try:
            from .data.dataset_store import DatasetStore
            from .data.repository import MarketDataRepository

            if cfg.market_data_root.exists():
                store = DatasetStore(cfg.market_data_root)
                repo = MarketDataRepository(store)
                best_cut = 0
                for d in repo.list_datasets():
                    if (d.status or "") not in ("ready", "partial"):
                        continue
                    cut = int(d.data_cutoff_date or 0)
                    if not cut:
                        lasts = [s.last_date for s in d.symbols if s.last_date]
                        cut = max(lasts) if lasts else 0
                    if cut > best_cut:
                        best_cut = cut
                if best_cut and best_cut > int(max_d or 0):
                    max_d = best_cut
                    top_year = best_cut // 10000
                    if top_year not in years:
                        years = sorted(set(years) | {top_year})
        except Exception:
            pass

        data = {
            "years": years,
            "min_date": min_d,
            "max_date": max_d,
            "months": list(range(1, 13)),
            "days": list(range(1, 32)),
        }
        _calendar_range_cache["data"] = data
        _calendar_range_cache["ts"] = now
        return data

    @app.post("/api/v1/backtests")
    def api_backtest(payload: BacktestBody) -> dict:
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
            from .service.baseline import (
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
        from .data.dataset_binding import DatasetBindingError
        from .data.repository import DatasetNotFoundError, DatasetNotReadyError

        if payload.async_mode:
            # Gate C D1/D4: validate dataset bindings BEFORE creating the job
            # so mismatches are rejected with 4xx and no run is created.
            if req.signal_data_source in ("tdxquant", "tushare", "internal", "raw"):
                from .service.backtest import (
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

    @app.post("/api/v1/backtests/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: str) -> dict:
        """Cancel a queued or running async backtest job."""
        try:
            rec = jobs.cancel(job_id)
            return {"ok": True, **jobs.to_public(rec)}
        except KeyError:
            raise HTTPException(404, "job not found") from None
        except Exception as e:
            raise HTTPException(500, str(e)) from e

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
        from .service.yao_rules import (
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

    @app.get("/api/v1/yao/rules")
    def api_yao_rules(
        status: Optional[str] = Query(None),
        group: Optional[str] = Query(None),
    ) -> dict:
        """List 爻辞规则 manifest for experiment center."""
        from .service.yao_rules import load_yao_manifest, manifest_rules

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
        from .data.dataset_binding import DatasetBindingError

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
                execution_data_source=str(payload.get("execution_data_source") or "local_vendor"),
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
            from .service.baseline import BaselineUnavailableError

            if isinstance(e, BaselineUnavailableError):
                raise HTTPException(409, str(e)) from e
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

    # Register static /variants/ path before /{experiment_id} to avoid ambiguity.
    @app.delete("/api/v1/experiments/variants/{variant_id}")
    def api_delete_experiment_variant(
        variant_id: str,
        remove_runs: bool = Query(True, description="also delete linked backtest run/files"),
    ) -> dict:
        from .service.db import delete_experiment_variant

        try:
            return delete_experiment_variant(cfg, variant_id, remove_runs=remove_runs)
        except FileNotFoundError:
            raise HTTPException(404, "variant not found") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    @app.delete("/api/v1/experiments/{experiment_id}")
    def api_delete_experiment(
        experiment_id: str,
        remove_runs: bool = Query(True, description="also delete linked backtest runs/files"),
    ) -> dict:
        from .service.db import delete_experiment
        from .service.experiments import get_runner

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

    # Watchlist availability involves warehouse manifest scans + TDX stat;
    # cache briefly so the bagua page renders instantly on revisit.
    _wl_cache: Dict = {"key": None, "ts": 0.0, "payload": None}

    @app.get("/api/v1/bagua/watchlist")
    def api_bagua_watchlist(
        kind: str = Query("all", description="all | index | etf"),
    ) -> dict:
        """Index (沪深指数) and ETF presets with TDX data availability."""
        from .service.index_etf import watchlist

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

    @app.get("/api/v1/bagua/constituents")
    def api_bagua_constituents(
        code: str = Query(..., min_length=1, description="index/ETF code e.g. sh510300 / sz399006"),
        limit: Optional[int] = Query(None, ge=1, le=2000, description="max constituent count"),
    ) -> dict:
        """Constituent stocks (成分股) of an index or ETF (via tracked index)."""
        from .service.bagua_query import normalize_query_code
        from .service.index_etf import resolve_constituents

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

    @app.get("/api/v1/bagua/query")
    def api_bagua_query(
        code: str = Query(..., min_length=1, description="stock code e.g. 600000 / sh600000"),
        date: str = Query(..., min_length=4, description="YYYY-MM-DD or YYYYMMDD"),
        period: str = Query("DAY", description="DAY | WEEK | MONTH"),
        adjust: str = Query(
            "tdx_front",
            description="tdx_front (通达信前复权) | tushare_qfq (Tushare前复权) | raw (未复权)",
        ),
    ) -> dict:
        """Query hexagram for one stock on a date (OHLC digit-sum algorithm)."""
        from .service.bagua_query import query_bagua

        try:
            return query_bagua(cfg, code=code, date=date, period=period, adjust=adjust)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except Exception as e:
            raise HTTPException(500, f"bagua query failed: {e}") from e

    @app.post("/api/v1/bagua/batch/query")
    def api_bagua_batch(payload: BaguaBatchBody = Body(...)) -> dict:
        """Multi-stock or full-market hexagram query (same OHLC algorithm)."""
        from .service.bagua_query import batch_query_bagua

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

    # ---- Bagua export jobs (async for full-market; sync for small batches) ----
    import threading as _bq_threading
    import time as _bq_time
    import uuid as _bq_uuid

    _bq_export_lock = _bq_threading.Lock()
    _bq_export_jobs: Dict[str, Dict[str, Any]] = {}

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

    def _bq_run_export_job(job_id: str, params: Dict[str, Any]) -> None:
        from .service.bagua_query import export_bagua_multi_period_xlsx

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

    def _bq_start_export_job(
        *,
        date: str,
        periods: List[str],
        adjust: str,
        codes: Optional[List[str]],
        all_stocks: bool,
        limit: Optional[int],
    ) -> dict:
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

    @app.post("/api/v1/bagua/export")
    def api_bagua_export(
        payload: BaguaExportBody = Body(...),
        async_mode: bool = Query(
            True,
            description="full-market defaults to background job; false forces sync",
        ),
    ):
        """Export bagua Excel. Full-market uses async job by default."""
        from .service.bagua_query import export_bagua_multi_period_xlsx

        if not payload.all_stocks and not payload.codes:
            raise HTTPException(400, "codes or all_stocks required")
        periods = _bq_normalize_periods(payload.period, payload.periods)
        use_async = async_mode and _bq_should_async(
            payload.all_stocks, payload.codes, payload.limit
        )
        if use_async:
            return _bq_start_export_job(
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

    @app.get("/api/v1/bagua/export")
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
    ):
        """GET export: full-market returns async job JSON; small sets return xlsx."""
        from .service.bagua_query import export_bagua_multi_period_xlsx

        code_list = None
        if codes:
            code_list = [c.strip() for c in codes.replace(";", ",").split(",") if c.strip()]
        if not all_stocks and not code_list:
            raise HTTPException(400, "codes or all_stocks required")
        periods = _bq_normalize_periods(period, None)
        use_async = async_mode and _bq_should_async(all_stocks, code_list, limit)
        if use_async:
            return _bq_start_export_job(
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

    @app.get("/api/v1/bagua/export/jobs/{job_id}")
    def api_bagua_export_job(job_id: str) -> dict:
        with _bq_export_lock:
            job = _bq_export_jobs.get(job_id)
            if not job:
                raise HTTPException(404, f"export job not found: {job_id}")
            return {
                k: v
                for k, v in job.items()
                if k != "path"
            }

    @app.get("/api/v1/bagua/export/jobs/{job_id}/download")
    def api_bagua_export_download(job_id: str) -> FileResponse:
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


    # ---- Data Sync API ----
    import subprocess
    import threading
    import time as _time
    import re as _re

    _sync_lock = threading.Lock()
    _sync_state: Dict[str, Any] = {
        "running": False,
        "task": None,
        "started_at": None,
        "finished_at": None,
        "status": "idle",
        "output": [],
        "error": None,
        "progress_done": 0,
        "progress_total": 0,
        "progress_phase": "",
    }

    _SYNC_SCRIPT = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py")
    _PROGRESS_RE = _re.compile(r"\[SYNC_PROGRESS\] done=(\d+) total=(\d+) phase=(\S+)")
    _sync_proc: Dict[str, Any] = {"proc": None}

    _CHECKPOINT_FILES = {
        "tdx": "checkpoint_tdxquant_front_1d.json",
        "factor": "checkpoint_tushare_adj_factor_1d.json",
        "tushare": "checkpoint_tushare_incremental_1d.json",
        "local_vendor": "checkpoint_local_vendor_none_1d.json",
    }

    def _checkpoint_exists(task: str) -> bool:
        fname = _CHECKPOINT_FILES.get(task)
        if not fname:
            return False
        return (cfg.market_data_root / "sync_logs" / fname).exists()

    def _latest_factor_universe_file() -> Optional[str]:
        """Reuse the latest ready adj_factor universe for UI-launched refreshes."""
        import os as _os
        from .data.dataset_store import DatasetStore

        for key in ("TUSHARE_FACTOR_UNIVERSE_FILE", "ASTOCK_FACTOR_UNIVERSE_FILE"):
            explicit = _os.environ.get(key, "").strip()
            if explicit and Path(explicit).exists():
                return explicit
        store = DatasetStore(cfg.market_data_root)
        candidates = []
        for mid in store.list_manifests():
            m = store.load_manifest(mid)
            if not m:
                continue
            if m.source != "tushare" or m.adjustment != "adj_factor":
                continue
            uf = (m.universe_file or "").strip()
            if not uf or not Path(uf).exists():
                continue
            candidates.append((int(m.data_cutoff_date or 0), int(m.symbol_count or 0), m.created_at or "", uf))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][3]

    def _run_sync_process(cmd: List[str], task_name: str) -> None:
        try:
            import os as _os
            env = dict(_os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            # Always pin formal data root so UI sync never writes to internal test tree.
            try:
                env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
                if cfg.astock_env:
                    env["ASTOCK_ENV"] = str(cfg.astock_env)
            except Exception:
                pass
            # Explicit --storage-root wins even if script ignores env on some paths.
            if "--storage-root" not in cmd:
                cmd = list(cmd) + ["--storage-root", str(cfg.market_data_root)]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(Path(__file__).resolve().parents[3]),
                env=env,
            )
            with _sync_lock:
                _sync_proc["proc"] = proc
            for line in proc.stdout:
                with _sync_lock:
                    _sync_state["output"].append(line.rstrip())
                    if len(_sync_state["output"]) > 500:
                        _sync_state["output"] = _sync_state["output"][-500:]
                    m = _PROGRESS_RE.search(line)
                    if m:
                        _sync_state["progress_done"] = int(m.group(1))
                        _sync_state["progress_total"] = int(m.group(2))
                        _sync_state["progress_phase"] = m.group(3)
            proc.wait()
            with _sync_lock:
                if _sync_state["status"] == "stopping":
                    _sync_state["status"] = "stopped"
                    _sync_state["error"] = "用户手动停止"
                else:
                    _sync_state["status"] = "done" if proc.returncode == 0 else "error"
                    _sync_state["error"] = f"exit code {proc.returncode}" if proc.returncode != 0 else None
        except Exception as e:
            with _sync_lock:
                _sync_state["status"] = "error"
                _sync_state["error"] = str(e)
        finally:
            with _sync_lock:
                _sync_state["running"] = False
                _sync_state["finished_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
                _sync_proc["proc"] = None

    @app.post("/api/v1/data-sync/start")
    def data_sync_start(payload: SyncStartBody) -> dict:
        nonlocal _sync_state
        import datetime
        today = int(datetime.date.today().strftime("%Y%m%d"))
        end_date = payload.end_date or today
        start_date = payload.start_date

        if payload.task not in ("tdx", "tushare", "factor", "derive", "ca"):
            raise HTTPException(400, f"未知任务类型: {payload.task}")

        if payload.task == "tdx":
            cmd = ["python", "-u", _SYNC_SCRIPT, "--source", "tdxquant", "--mode", "incremental", "--end-date", str(end_date), "--skip-ca-detect"]
        elif payload.task == "tushare":
            cmd = ["python", "-u", _SYNC_SCRIPT, "--source", "tushare", "--mode", "incremental", "--end-date", str(end_date)]
            if start_date:
                cmd += ["--start-date", str(start_date)]
        elif payload.task == "factor":
            cmd = ["python", "-u", _SYNC_SCRIPT, "--source", "tushare", "--adjustment", "adj_factor", "--mode", "full", "--end-date", str(end_date)]
            universe_file = _latest_factor_universe_file()
            if not universe_file:
                raise HTTPException(400, "Tushare adj_factor sync requires --universe-file")
            cmd += ["--universe-file", universe_file]
        elif payload.task == "ca":
            _CA_SCRIPT = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_ca_events.py")
            cmd = ["python", "-u", _CA_SCRIPT, "--mode", "incremental", "--days", "90",
                   "--storage-root", str(cfg.market_data_root)]
        else:
            cmd = ["python", "-u", _SYNC_SCRIPT, "--source", "internal", "--mode", "derive",
                   "--adjustment", "tushare_factor_qfq", "--cutoff", str(end_date)]

        # Resume only when client asks; leftover checkpoint must not force resume.
        if payload.task in ("tdx", "tushare", "factor"):
            if payload.fresh:
                cmd += ["--fresh"]
            elif payload.resume:
                cmd += ["--resume"]

        # Claim under lock so concurrent POSTs cannot both start a sync.
        with _sync_lock:
            if _sync_state["running"]:
                raise HTTPException(409, f"同步任务正在进行中: {_sync_state['task']}")
            _sync_state = {
                "running": True,
                "task": payload.task,
                "started_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "status": "running",
                "output": [f"[CMD] {' '.join(cmd)}"],
                "error": None,
                "progress_done": 0,
                "progress_total": 0,
                "progress_phase": "",
            }

        t = threading.Thread(target=_run_sync_process, args=(cmd, payload.task), daemon=True)
        t.start()
        return {
            "ok": True,
            "task": payload.task,
            "message": f"已启动: {payload.task}",
            "resume": bool(payload.resume and not payload.fresh),
            "fresh": bool(payload.fresh),
            "checkpoint_present": _checkpoint_exists(payload.task) if payload.task in _CHECKPOINT_FILES else False,
        }

    @app.get("/api/v1/data-sync/status")
    def data_sync_status() -> dict:
        with _sync_lock:
            return {
                "running": _sync_state["running"],
                "task": _sync_state["task"],
                "status": _sync_state["status"],
                "started_at": _sync_state["started_at"],
                "finished_at": _sync_state["finished_at"],
                "error": _sync_state["error"],
                "progress_done": _sync_state.get("progress_done", 0),
                "progress_total": _sync_state.get("progress_total", 0),
                "progress_phase": _sync_state.get("progress_phase", ""),
                "output_tail": _sync_state["output"][-50:],
                "output_lines": len(_sync_state["output"]),
                "checkpoints": {t: _checkpoint_exists(t) for t in _CHECKPOINT_FILES},
            }

    @app.post("/api/v1/data-sync/stop")
    def data_sync_stop() -> dict:
        with _sync_lock:
            if not _sync_state["running"]:
                return {"ok": False, "message": "当前没有运行中的任务"}
            proc = _sync_proc.get("proc")
            if proc is None:
                return {"ok": False, "message": "进程句柄丢失"}
            _sync_state["status"] = "stopping"
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"ok": True, "message": "已停止同步任务"}
        except Exception as e:
            return {"ok": False, "message": f"停止失败: {e}"}

    _ca_file_count_cache: Dict[str, Any] = {"mtime": None, "count": 0}

    @app.get("/api/v1/ca-events/status")
    def ca_events_status() -> dict:
        ca_dir = cfg.market_data_root / "ca_events"
        meta_file = ca_dir / "_meta.json"
        if not meta_file.exists():
            return {"exists": False, "last_sync_at": None, "total_files": 0}
        try:
            meta_mtime = meta_file.stat().st_mtime
        except OSError:
            meta_mtime = None
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        n_files = meta.get("success", 0)
        if not n_files:
            if _ca_file_count_cache["mtime"] != meta_mtime:
                _ca_file_count_cache["count"] = sum(
                    1 for p in ca_dir.glob("*.json") if p.name != "_meta.json"
                ) if ca_dir.exists() else 0
                _ca_file_count_cache["mtime"] = meta_mtime
            n_files = _ca_file_count_cache["count"]
        return {
            "exists": True,
            "last_sync_at": meta.get("last_sync_at"),
            "last_sync_mode": meta.get("last_sync_mode"),
            "sync_range": meta.get("sync_range"),
            "total_stocks": meta.get("total_stocks", 0),
            "success": meta.get("success", 0),
            "failed": meta.get("failed", 0),
            "total_files": n_files,
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # HTML entry pages: no-cache so root cutover and rollbacks take effect immediately.
    _HTML_NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    def _html_page(filename: str, missing_title: str) -> HTMLResponse:
        index_path = STATIC_DIR / filename
        if index_path.exists():
            return HTMLResponse(
                index_path.read_text(encoding="utf-8"),
                headers=dict(_HTML_NO_CACHE_HEADERS),
            )
        return HTMLResponse(
            f"<h1>{missing_title}</h1><p>static/{filename} missing</p>",
            headers=dict(_HTML_NO_CACHE_HEADERS),
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        """Official main UI: V3 (index_v3.html). Rollback: serve index.html instead."""
        return _html_page("index_v3.html", "AStock")

    @app.get("/legacy", response_class=HTMLResponse)
    def index_legacy() -> HTMLResponse:
        """Original V1 UI (index.html); kept for quick rollback and bookmarks."""
        return _html_page("index.html", "AStock legacy")

    @app.get("/v2", response_class=HTMLResponse)
    def index_v2() -> HTMLResponse:
        """V2 transition UI (index_v2.html)."""
        return _html_page("index_v2.html", "AStock v2")

    @app.get("/v3", response_class=HTMLResponse)
    def index_v3() -> HTMLResponse:
        """V3 formal UI (same shell as /)."""
        return _html_page("index_v3.html", "AStock v3")

    @app.get("/v3/task-detail", response_class=HTMLResponse)
    def index_v3_task_detail() -> HTMLResponse:
        """Independent task detail page; same SPA shell as V3 main."""
        return _html_page("index_v3.html", "AStock v3")

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, cfg: Optional[AStockConfig] = None) -> None:
    import uvicorn

    from .config import load_env_file, market_data_root_guard

    # Machine-local settings (MARKET_DATA_ROOT, ASTOCK_ENV, ...) live in .env
    # at the project root; existing environment variables always win.
    load_env_file()

    cfg = cfg or get_default_config()
    guard = market_data_root_guard(cfg)

    ready_count = None
    latest_lv = None
    try:
        from .data.dataset_store import DatasetStore
        from .data.repository import MarketDataRepository

        if cfg.market_data_root.exists():
            repo = MarketDataRepository(DatasetStore(cfg.market_data_root))
            all_ds = repo.list_datasets()
            ready_count = sum(1 for d in all_ds if d.status == "ready")
            lv = sorted(
                (d for d in all_ds if d.source == "local_vendor" and d.status == "ready"),
                key=lambda d: d.created_at or "",
            )
            latest_lv = lv[-1].dataset_id if lv else None
        else:
            ready_count = 0
    except Exception as e:  # pragma: no cover - startup banner must never crash
        ready_count = f"unavailable ({e})"

    print("=" * 64)
    print("AStock Console startup")
    print(f"  ASTOCK_ENV        : {guard['astock_env']}")
    print(f"  MARKET_DATA_ROOT  : {guard['market_data_root']}"
          + ("  [INTERNAL TEST ROOT]" if guard["is_internal"] else "  [external]"))
    print(f"  env var set       : {guard['market_data_root_env_set']}")
    print(f"  ready datasets    : {ready_count}")
    print(f"  latest local_vendor ready dataset: {latest_lv or '—'}")
    print("=" * 64)

    if guard["blocked"]:
        print("!! STARTUP BLOCKED (production data-root guard)")
        print(f"!! reason  : {guard['reason']}")
        print("!! fix     : set MARKET_DATA_ROOT to the production data root in .env")
        print(f"!! override: {guard['override_allowed_by']} (NOT recommended)")
        raise SystemExit(2)
    if guard["is_internal"] and guard["astock_env"] != "production":
        print("WARNING: using INTERNAL project test data root "
              "(set MARKET_DATA_ROOT / ASTOCK_ENV=production for formal use)")

    app = create_app(cfg)

    # Auto-check CA data freshness on startup; trigger incremental sync if stale (>30 days).
    def _auto_ca_check():
        import datetime as _dt
        ca_meta = cfg.market_data_root / "ca_events" / "_meta.json"
        need_sync = False
        reason = ""
        if not ca_meta.exists():
            need_sync = True
            reason = "CA数据从未同步"
        else:
            try:
                with open(ca_meta, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                last = meta.get("last_sync_at", "")
                if last:
                    last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                    days_ago = (_dt.datetime.now() - last_dt).days
                    if days_ago > 30:
                        need_sync = True
                        reason = f"CA数据已 {days_ago} 天未更新"
                else:
                    need_sync = True
                    reason = "CA元数据缺少同步时间"
            except Exception:
                need_sync = True
                reason = "CA元数据读取失败"
        if need_sync:
            print(f"[CA_AUTO] {reason}，后台自动启动增量同步…")
            _CA_SCRIPT = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_ca_events.py")
            cmd = ["python", "-u", _CA_SCRIPT, "--mode", "incremental", "--days", "90",
                   "--storage-root", str(cfg.market_data_root)]
            try:
                import os as _os
                env = dict(_os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                print(f"[CA_AUTO] 已启动后台进程 PID={proc.pid}")
            except Exception as e:
                print(f"[CA_AUTO] 启动失败: {e}")
        else:
            print("[CA_AUTO] CA数据在有效期内，跳过自动同步")

    import threading as _thr
    _thr.Thread(target=_auto_ca_check, daemon=True).start()

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

