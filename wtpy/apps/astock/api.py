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
