"""Shared runtime context for AStock API routers.

Holds the per-app services and mutable sync/export state that used to be
closure variables inside ``api.create_app``. One instance is created per
``create_app()`` call, mounted on ``app.state.astock``, and injected into
route handlers via :func:`get_ctx` (FastAPI dependency).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import Request

from ..config import AStockConfig
from ..forecast.service import ForecastService
from ..service.backtest import BacktestService
from ..service.jobs import JobStore
from ..service.rules import RuleService


@dataclass
class ApiContext:
    cfg: AStockConfig
    rules: RuleService
    jobs: JobStore
    bt_svc: BacktestService
    forecast: ForecastService

    sync_lock: threading.Lock = field(default_factory=threading.Lock)
    sync_proc: Dict[str, Any] = field(default_factory=lambda: {"proc": None})
    sync_state: Dict[str, Any] = field(
        default_factory=lambda: {
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
    )

    bq_export_lock: threading.Lock = field(default_factory=threading.Lock)
    bq_export_jobs: Dict[str, Any] = field(default_factory=dict)

    wl_cache: Dict[str, Any] = field(
        default_factory=lambda: {"key": None, "ts": 0.0, "payload": None}
    )

    # market-data/status scans every manifest + blob dir (slow on large
    # warehouses); 30s TTL cache keeps page refreshes fast.
    md_status_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "payload": None}
    )

    # Blob-dir size/count scan walks ~100k files (many seconds); blob set
    # only changes during sync, so cache it for 5 minutes independently so a
    # market-data/status cache expiry never re-triggers the full blob scan.
    md_blob_stats_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "count": 0, "size": 0}
    )

    # eod-sync/status runs a full tushare product health scan (base/factor
    # selection + per-symbol blob integrity + pair validation — seconds on
    # large warehouses); 30s TTL cache keeps datastore-page refreshes fast.
    eod_sync_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "payload": None}
    )

    calendar_range_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "data": None}
    )

    ca_file_count_cache: Dict[str, Any] = field(
        default_factory=lambda: {"mtime": None, "count": 0}
    )

    dashboard_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "payload": None}
    )

    quick_cache: Dict[str, Any] = field(
        default_factory=lambda: {"ts": 0.0, "payload": {}}
    )

    def __post_init__(self) -> None:
        self.sync_state.setdefault("output", [])
        self.sync_proc.setdefault("proc", None)
        self.bq_export_jobs.setdefault("_seed", None)
        self.wl_cache.setdefault("ts", 0.0)


def get_ctx(request: Request) -> ApiContext:
    """FastAPI dependency: the per-app :class:`ApiContext`."""
    return request.app.state.astock
