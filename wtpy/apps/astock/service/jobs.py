"""In-process job store for async backtests with progress reporting."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..config import AStockConfig, get_default_config
from .backtest import BacktestRequest, BacktestService


@dataclass
class JobRecord:
    job_id: str
    status: str  # queued | running | succeeded | failed
    created_at: float
    updated_at: float
    request: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: Optional[str] = None
    run_id: Optional[str] = None
    progress: Dict[str, Any] = field(default_factory=dict)


class JobStore:
    def __init__(self, cfg: Optional[AStockConfig] = None, max_workers: int = 1):
        self.cfg = cfg or get_default_config()
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def _set_progress(self, job_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            rec = self._jobs[job_id]
            prog = dict(rec.progress or {})
            prog.update(payload)
            prog["updated_at"] = time.time()
            rec.progress = prog
            rec.updated_at = time.time()

    def submit(self, req: BacktestRequest) -> JobRecord:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = time.time()
        rec = JobRecord(
            job_id=job_id,
            status="queued",
            created_at=now,
            updated_at=now,
            request=req.to_dict(),
            progress={
                "phase": "queued",
                "pct": 0.0,
                "current": 0,
                "total": 0,
                "message": "排队中",
                "code": None,
            },
        )
        with self._lock:
            self._jobs[job_id] = rec

        def _progress(payload: Dict[str, Any]) -> None:
            self._set_progress(job_id, payload)

        def _run() -> None:
            with self._lock:
                self._jobs[job_id].status = "running"
                self._jobs[job_id].updated_at = time.time()
                self._jobs[job_id].progress = {
                    "phase": "starting",
                    "pct": 1.0,
                    "current": 0,
                    "total": 0,
                    "message": "任务启动",
                    "code": None,
                    "updated_at": time.time(),
                }
            try:
                svc = BacktestService(self.cfg)
                summary = svc.run(req, progress_cb=_progress)
                with self._lock:
                    self._jobs[job_id].result = summary
                    self._jobs[job_id].run_id = summary.get("run_id")
                    st = summary.get("status") or "ok"
                    if st in ("no_go", "rejected_unconfirmed_formula") or summary.get("error"):
                        self._jobs[job_id].status = "failed"
                        self._jobs[job_id].error = (
                            summary.get("error") or summary.get("reason") or st
                        )
                        self._jobs[job_id].progress = {
                            **(self._jobs[job_id].progress or {}),
                            "phase": "failed",
                            "pct": float((self._jobs[job_id].progress or {}).get("pct") or 0),
                            "message": self._jobs[job_id].error or st,
                            "updated_at": time.time(),
                        }
                    else:
                        self._jobs[job_id].status = "succeeded"
                        self._jobs[job_id].progress = {
                            "phase": "done",
                            "pct": 100.0,
                            "current": 1,
                            "total": 1,
                            "message": "完成",
                            "code": None,
                            "run_id": summary.get("run_id"),
                            "updated_at": time.time(),
                        }
                    self._jobs[job_id].updated_at = time.time()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._jobs[job_id].status = "failed"
                    self._jobs[job_id].error = f"{e}\n{traceback.format_exc()}"
                    self._jobs[job_id].progress = {
                        "phase": "failed",
                        "pct": float((self._jobs[job_id].progress or {}).get("pct") or 0),
                        "message": str(e),
                        "updated_at": time.time(),
                    }
                    self._jobs[job_id].updated_at = time.time()

        self._pool.submit(_run)
        return rec

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return self._jobs[job_id]

    def list_public(self, *, limit: int = 20) -> list:
        with self._lock:
            items = sorted(
                self._jobs.values(), key=lambda r: r.created_at, reverse=True
            )[:limit]
            return [self.to_public(r) for r in items]

    def to_public(self, rec: JobRecord) -> dict:
        return {
            "job_id": rec.job_id,
            "status": rec.status,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "request": rec.request,
            "result": rec.result,
            "error": rec.error,
            "run_id": rec.run_id,
            "progress": dict(rec.progress or {}),
        }
