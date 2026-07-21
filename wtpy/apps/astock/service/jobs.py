"""In-process job store for async backtests with FIFO queue (max_workers=1 by default).

Multiple submits are accepted immediately; only ``max_workers`` run at once.
Others stay ``queued`` until a worker is free — first submitted starts first.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import AStockConfig, get_default_config
from .backtest import BacktestRequest, BacktestService


@dataclass
class JobRecord:
    job_id: str
    status: str  # queued | running | succeeded | failed | cancelled
    created_at: float
    updated_at: float
    request: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: Optional[str] = None
    run_id: Optional[str] = None
    progress: Dict[str, Any] = field(default_factory=dict)
    title_hint: str = ""
    queue_seq: int = 0  # monotonic submit order for FIFO display


class JobStore:
    def __init__(self, cfg: Optional[AStockConfig] = None, max_workers: int = 1):
        self.cfg = cfg or get_default_config()
        self.max_workers = max(1, int(max_workers or 1))
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="astock-bt-job"
        )
        self._seq = 0

    def _set_progress(self, job_id: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            rec = self._jobs[job_id]
            if rec.status == "cancelled":
                return
            prog = dict(rec.progress or {})
            prog.update(payload)
            prog["updated_at"] = time.time()
            # attach live queue meta for UI
            prog["queue_position"] = self._queue_position_unlocked(job_id)
            prog["n_queued"] = self._count_status_unlocked("queued")
            prog["n_running"] = self._count_status_unlocked("running")
            rec.progress = prog
            rec.updated_at = time.time()

    def _count_status_unlocked(self, status: str) -> int:
        return sum(1 for r in self._jobs.values() if r.status == status)

    def _queue_position_unlocked(self, job_id: str) -> int:
        """1-based position among queued jobs (0 if not queued)."""
        rec = self._jobs.get(job_id)
        if not rec or rec.status != "queued":
            return 0
        queued = sorted(
            [r for r in self._jobs.values() if r.status == "queued"],
            key=lambda r: r.queue_seq,
        )
        for i, r in enumerate(queued, 1):
            if r.job_id == job_id:
                return i
        return 0

    def _title_hint(self, req: BacktestRequest) -> str:
        ids = list(getattr(req, "rule_ids", None) or [])
        head = "、".join(str(x) for x in ids[:2]) if ids else "回测"
        if len(ids) > 2:
            head += "…"
        gf = getattr(req, "gua_filter", None) or {}
        if isinstance(gf, dict) and gf.get("enabled"):
            head += " +卦象"
        return head[:80]

    def submit(self, req: BacktestRequest) -> JobRecord:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._lock:
            self._seq += 1
            seq = self._seq
            n_ahead = self._count_status_unlocked("queued") + self._count_status_unlocked(
                "running"
            )
            rec = JobRecord(
                job_id=job_id,
                status="queued",
                created_at=now,
                updated_at=now,
                request=req.to_dict(),
                title_hint=self._title_hint(req),
                queue_seq=seq,
                progress={
                    "phase": "queued",
                    "pct": 0.0,
                    "current": 0,
                    "total": 0,
                    "message": (
                        "排队中（前面还有 %d 个任务）" % n_ahead
                        if n_ahead
                        else "排队中，即将开始"
                    ),
                    "code": None,
                    "queue_position": n_ahead + 1,
                    "n_queued": self._count_status_unlocked("queued") + 1,
                    "n_running": self._count_status_unlocked("running"),
                    "updated_at": now,
                },
            )
            self._jobs[job_id] = rec

        def _progress(payload: Dict[str, Any]) -> None:
            self._set_progress(job_id, payload)

        def _run() -> None:
            with self._lock:
                rec0 = self._jobs.get(job_id)
                if not rec0 or rec0.status == "cancelled":
                    return
                self._jobs[job_id].status = "running"
                self._jobs[job_id].updated_at = time.time()
                self._jobs[job_id].progress = {
                    "phase": "starting",
                    "pct": 1.0,
                    "current": 0,
                    "total": 0,
                    "message": "任务启动",
                    "code": None,
                    "queue_position": 0,
                    "n_queued": self._count_status_unlocked("queued"),
                    "n_running": self._count_status_unlocked("running"),
                    "updated_at": time.time(),
                }
            try:
                svc = BacktestService(self.cfg)
                summary = svc.run(req, progress_cb=_progress)
                with self._lock:
                    if self._jobs.get(job_id) and self._jobs[job_id].status == "cancelled":
                        return
                    self._jobs[job_id].result = summary
                    self._jobs[job_id].run_id = summary.get("run_id")
                    st = summary.get("status") or "ok"
                    if st in ("no_go", "rejected_unconfirmed_formula") or summary.get(
                        "error"
                    ):
                        self._jobs[job_id].status = "failed"
                        self._jobs[job_id].error = (
                            summary.get("error") or summary.get("reason") or st
                        )
                        self._jobs[job_id].progress = {
                            **(self._jobs[job_id].progress or {}),
                            "phase": "failed",
                            "pct": float(
                                (self._jobs[job_id].progress or {}).get("pct") or 0
                            ),
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
                            "queue_position": 0,
                            "updated_at": time.time(),
                        }
                    self._jobs[job_id].updated_at = time.time()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    if job_id not in self._jobs:
                        return
                    if self._jobs[job_id].status == "cancelled":
                        return
                    self._jobs[job_id].status = "failed"
                    self._jobs[job_id].error = f"{e}\n{traceback.format_exc()}"
                    self._jobs[job_id].progress = {
                        "phase": "failed",
                        "pct": float(
                            (self._jobs[job_id].progress or {}).get("pct") or 0
                        ),
                        "message": str(e),
                        "updated_at": time.time(),
                    }
                    self._jobs[job_id].updated_at = time.time()

        # ThreadPoolExecutor with max_workers=1 runs FIFO among submitted callables
        self._pool.submit(_run)
        return rec

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return self._jobs[job_id]

    def list_public(self, *, limit: int = 50) -> list:
        with self._lock:
            items = sorted(
                self._jobs.values(), key=lambda r: r.queue_seq, reverse=True
            )[:limit]
            return [self.to_public(r) for r in items]

    def queue_snapshot(self) -> Dict[str, Any]:
        """Summary for task-center UI."""
        with self._lock:
            queued = sorted(
                [r for r in self._jobs.values() if r.status == "queued"],
                key=lambda r: r.queue_seq,
            )
            running = [r for r in self._jobs.values() if r.status == "running"]
            recent = sorted(
                self._jobs.values(), key=lambda r: r.updated_at, reverse=True
            )[:30]
            return {
                "max_workers": self.max_workers,
                "n_queued": len(queued),
                "n_running": len(running),
                "n_total": len(self._jobs),
                "queued": [self.to_public(r) for r in queued],
                "running": [self.to_public(r) for r in running],
                "recent": [self.to_public(r) for r in recent],
            }

    def to_public(self, rec: JobRecord) -> dict:
        with self._lock:
            qpos = self._queue_position_unlocked(rec.job_id) if rec.status == "queued" else 0
            n_q = self._count_status_unlocked("queued")
            n_r = self._count_status_unlocked("running")
        prog = dict(rec.progress or {})
        prog.setdefault("queue_position", qpos)
        prog.setdefault("n_queued", n_q)
        prog.setdefault("n_running", n_r)
        return {
            "job_id": rec.job_id,
            "status": rec.status,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "request": rec.request,
            "result": rec.result,
            "error": rec.error,
            "run_id": rec.run_id,
            "progress": prog,
            "title_hint": rec.title_hint,
            "queue_position": qpos,
            "queue_seq": rec.queue_seq,
        }
