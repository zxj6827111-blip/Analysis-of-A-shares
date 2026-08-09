"""FastAPI server for A-stock frontend.

Route handlers live in :mod:`wtpy.apps.astock.api_routes`; this module only
assembles the app: shared context (services + sync/export state), routers and
static mount. See ``serve()`` for the CLI entry point.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import AStockConfig, get_default_config
from .forecast.service import ForecastService
from .service.backtest import BacktestService
from .service.jobs import JobStore
from .service.rules import RuleService
from .version import get_version_string
from .api_routes import (
    ApiContext,
    backtests,
    bagua,
    experiments,
    forecast,
    research,
    rules,
    system,
)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# Backward-compatible re-exports: request models previously lived here.
from .api_routes.backtests import BacktestBody  # noqa: E402
from .api_routes.bagua import BaguaBatchBody, BaguaExportBody  # noqa: E402
from .api_routes.forecast import ForecastBatchBody  # noqa: E402
from .api_routes.rules import RuleCreate, RuleUpdate, RuleValidate  # noqa: E402
from .api_routes.system import SyncStartBody  # noqa: E402

_ALL_ROUTERS = (
    rules.router,
    backtests.router,
    experiments.router,
    research.router,
    forecast.router,
    bagua.router,
    system.router,
)


def eod_sync_decide(
    *,
    lag: Optional[int],
    now,
    sync_time: str = "18:30",
    min_lag: int = 1,
    last_trigger_day=None,
) -> tuple:
    """Decide whether an automatic EOD sync should fire.

    Returns ``(trigger: bool, reason: str, today_key)`` where ``today_key``
    is the date the caller should remember as the last trigger day (None when
    not triggering). Weekends, pre-``sync_time`` hours, unknown lag, a lag
    below ``min_lag`` and an already-triggered day all short-circuit to False.
    """
    import datetime as _dt

    if now.weekday() >= 5:
        return False, "周末，不触发", None
    if now.strftime("%H:%M") < sync_time:
        return False, f"未到自动同步时间（{sync_time}）", None
    today = now.date()
    if last_trigger_day is not None and last_trigger_day == today:
        return False, "今日已触发过自动同步", None
    if lag is None:
        return False, "无法判断数据新鲜度（跳过）", None
    if lag < min_lag:
        return False, f"数据已最新（lag={lag}）", None
    return True, f"raw 数据滞后 {lag} 个交易日", today


def _auto_eod_sync(cfg: AStockConfig, ctx: "ApiContext") -> None:
    """Startup + scheduled EOD auto-sync of Tushare market data.

    Scheduling: on weekdays it sleeps straight to ``ASTOCK_EOD_SYNC_TIME``
    (no idle polling before it); at/after the time it checks freshness and
    spawns the same incremental sync the UI button uses
    (``--source tushare --mode incremental``) plus ``--fresh``. If the data
    is not yet lagged (Tushare publishes late) it retries every
    ``ASTOCK_EOD_SYNC_POLL_SECONDS`` (default 30 min); once fired, the day is
    done and it sleeps to the next day's sync time. Weekends sleep to Monday.

    The trigger record is persisted to ``storage/astock/eod_sync_state.json``
    so the UI can show "上次自动同步时间".

    Env switches (all optional):
      ASTOCK_EOD_SYNC_ENABLED=0|1        (default 1)
      ASTOCK_EOD_SYNC_STARTUP=0|1        (default 1, run once on startup)
      ASTOCK_EOD_SYNC_TIME=HH:MM         (default 18:30)
      ASTOCK_EOD_SYNC_MIN_LAG_DAYS=N     (default 1)
      ASTOCK_EOD_SYNC_POLL_SECONDS=N     (default 1800, min 60)
    """
    import datetime as _dt
    import json as _json
    import os as _os
    import time as _time

    def _env_flag(name: str, default: str = "1") -> bool:
        return _os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")

    if not _env_flag("ASTOCK_EOD_SYNC_ENABLED", "1"):
        print("[EOD_SYNC] 已禁用（ASTOCK_EOD_SYNC_ENABLED=0），跳过自动更新")
        return
    if not cfg.market_data_root.exists():
        print("[EOD_SYNC] 数据目录不存在，跳过自动更新")
        return

    sync_time = _os.environ.get("ASTOCK_EOD_SYNC_TIME", "18:30")
    try:
        min_lag = max(0, int(_os.environ.get("ASTOCK_EOD_SYNC_MIN_LAG_DAYS", "1")))
    except ValueError:
        min_lag = 1
    try:
        # retry interval AFTER the sync time when data is not yet lagged
        poll_sec = max(60, int(_os.environ.get("ASTOCK_EOD_SYNC_POLL_SECONDS", "1800")))
    except ValueError:
        poll_sec = 1800
    startup_check = _env_flag("ASTOCK_EOD_SYNC_STARTUP", "1")

    # persisted trigger record for the UI status card
    state_path = Path(__file__).resolve().parents[3] / "storage" / "astock" / "eod_sync_state.json"

    last_trigger_day = None

    def _load_state() -> Dict[str, Any]:
        try:
            if state_path.exists():
                return _json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            st = _load_state()
            st.update({
                "enabled": True,
                "sync_time": sync_time,
                "min_lag_days": min_lag,
                "poll_seconds": poll_sec,
                "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            if extra:
                st.update(extra)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                _json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _lag_days() -> Optional[int]:
        from .data.dataset_store import DatasetStore
        from .data.tushare_product import tushare_product_data_health

        health = tushare_product_data_health(DatasetStore(cfg.market_data_root))
        return health.get("trading_day_lag", {}).get("raw")

    def _sync_in_progress() -> bool:
        try:
            with ctx.sync_lock:
                return bool(ctx.sync_state.get("running"))
        except Exception:
            return False

    def _trigger(reason: str) -> None:
        nonlocal last_trigger_day
        script = str(Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py")
        today = int(_dt.date.today().strftime("%Y%m%d"))
        cmd = [
            sys.executable, "-u", script,
            "--source", "tushare", "--mode", "incremental",
            "--end-date", str(today), "--fresh",
            "--storage-root", str(cfg.market_data_root),
        ]
        token = _os.environ.get("TUSHARE_TOKEN", "").strip()
        if token:
            cmd += ["--token", token]
        env = dict(_os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["MARKET_DATA_ROOT"] = str(cfg.market_data_root)
        print(f"[EOD_SYNC] {reason}，自动启动 Tushare 增量同步…")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
            )
            print(f"[EOD_SYNC] 已启动后台进程 PID={proc.pid}")
            last_trigger_day = _dt.date.today()
            _save_state({
                "last_trigger_date": last_trigger_day.strftime("%Y-%m-%d"),
                "last_sync_started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_reason": reason,
                "sync_pid": int(proc.pid or 0),
            })
        except Exception as e:
            print(f"[EOD_SYNC] 启动失败: {e}")

    def _check(label: str) -> bool:
        """Returns True when a sync was triggered."""
        now = _dt.datetime.now()
        trigger, reason, today_key = eod_sync_decide(
            lag=_lag_days(),
            now=now,
            sync_time=sync_time,
            min_lag=min_lag,
            last_trigger_day=last_trigger_day,
        )
        if trigger:
            if _sync_in_progress():
                print(f"[EOD_SYNC] {label}：{reason}，但已有手动同步在运行，稍后重试")
                return False
            _trigger(f"{label}：{reason}")
            return True
        print(f"[EOD_SYNC] {label}：{reason}")
        return False

    def _sleep_until(hhmm: str) -> None:
        """Sleep until today's hh:mm (or tomorrow if already passed)."""
        now = _dt.datetime.now()
        try:
            target = now.replace(
                hour=int(hhmm[:2]), minute=int(hhmm[3:5]), second=0, microsecond=0
            )
        except (ValueError, IndexError):
            target = now.replace(hour=18, minute=30, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        _time.sleep(max(1.0, (target - now).total_seconds()))

    if startup_check:
        try:
            _check("启动检查")
        except Exception as e:
            print(f"[EOD_SYNC] 启动检查异常: {type(e).__name__}: {e}")
    _save_state()

    while True:
        try:
            now = _dt.datetime.now()
            if now.weekday() >= 5:
                # weekend: sleep to Monday 00:05
                days_until_mon = (7 - now.weekday()) % 7
                target = (now + _dt.timedelta(days=days_until_mon)).replace(
                    hour=0, minute=5, second=0, microsecond=0
                )
                _time.sleep(max(1.0, (target - now).total_seconds()))
                continue
            if now.strftime("%H:%M") < sync_time:
                _sleep_until(sync_time)
                continue
            fired = _check("收盘后定时检查")
            if fired:
                # day done: sleep to tomorrow's sync time
                _sleep_until(sync_time)
            else:
                _time.sleep(poll_sec)
        except Exception as e:
            print(f"[EOD_SYNC] 定时检查异常: {type(e).__name__}: {e}")
            _time.sleep(300)


def create_app(cfg: Optional[AStockConfig] = None) -> FastAPI:
    cfg = cfg or get_default_config()
    cfg.ensure_dirs()
    app = FastAPI(
        title="AStock Backtest Console",
        version=get_version_string(),
    )
    app.state.astock = ApiContext(
        cfg=cfg,
        rules=RuleService(cfg),
        jobs=JobStore(cfg),
        bt_svc=BacktestService(cfg),
        forecast=ForecastService(cfg),
    )
    for router in _ALL_ROUTERS:
        app.include_router(router)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
    product_line = "—"
    try:
        from .data.dataset_store import DatasetStore
        from .data.repository import MarketDataRepository
        from .data.tushare_product import resolve_active_tushare_product_pair

        if cfg.market_data_root.exists():
            store = DatasetStore(cfg.market_data_root)
            repo = MarketDataRepository(store)
            all_ds = repo.list_datasets()
            ready_count = sum(1 for d in all_ds if d.status == "ready")
            pair = resolve_active_tushare_product_pair(store)
            if pair is not None:
                product_line = (
                    f"L1={pair.l1_dataset_id} | L2={pair.l2_dataset_id} "
                    f"| cutoff={pair.cutoff}"
                )
            else:
                product_line = "formal L1/L2 not ready (Tushare-only)"
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
    print(f"  Tushare product   : {product_line}")
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
            cmd = [sys.executable, "-u", _CA_SCRIPT, "--mode", "incremental", "--days", "90",
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

    def _auto_tushare_product_reconcile() -> None:
        """Build formal products from existing local Tushare parents only."""
        try:
            from .data.dataset_store import DatasetStore
            from .data.tushare_product import reconcile_tushare_product_datasets

            if not cfg.market_data_root.exists():
                print("[TUSHARE_PRODUCT] data root missing; waiting for sync")
                return
            result = reconcile_tushare_product_datasets(
                DatasetStore(cfg.market_data_root)
            )
            print(
                "[TUSHARE_PRODUCT] "
                f"status={result.status} "
                f"L1={result.l1_dataset_id or '-'} "
                f"L2={result.l2_dataset_id or '-'} "
                f"missing={result.missing or '-'} "
                f"issues={result.issues or '-'}"
            )
        except Exception as exc:
            # Product migration must never prevent the API from starting.
            print(
                "[TUSHARE_PRODUCT] reconcile failed: "
                f"{type(exc).__name__}: {exc}"
            )

    _thr.Thread(
        target=_auto_tushare_product_reconcile,
        daemon=True,
        name="astock-tushare-product-reconcile",
    ).start()

    # Automatic EOD market-data sync: startup freshness check + scheduled
    # weekday sync after market close (env-configurable, see _auto_eod_sync).
    _thr.Thread(
        target=_auto_eod_sync,
        args=(cfg, app.state.astock),
        daemon=True,
        name="astock-eod-sync",
    ).start()

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
