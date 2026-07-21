"""In-process job store for async backtests with a true FIFO queue.

Design:
- Submit always returns immediately with status ``queued``.
- A single dedicated worker thread pulls jobs in order and runs them one by one
  (max_workers currently fixed to 1 for backtests).
- Additional submits while one is running stay queued until the worker is free.

This avoids ThreadPoolExecutor edge-cases under pytest and matches the product
requirement: create task A, then B — B waits until A finishes, then auto-starts.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
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
    queue_seq: int = 0
    # kept only for worker; not serialized
    _req_obj: Any = field(default=None, repr=False, compare=False)


class JobStore:
    def __init__(self, cfg: Optional[AStockConfig] = None, max_workers: int = 1):
        self.cfg = cfg or get_default_config()
        # Product: serial backtests. max_workers kept for API compatibility.
        self.max_workers = max(1, int(max_workers or 1))
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="astock-bt-queue-worker",
            daemon=True,
        )
        self._worker.start()

    def _title_hint(self, req: BacktestRequest) -> str:
        ids = list(getattr(req, "rule_ids", None) or [])
        head = "、".join(str(x) for x in ids[:2]) if ids else "回测"
        if len(ids) > 2:
            head += "…"
        gf = getattr(req, "gua_filter", None) or {}
        if isinstance(gf, dict) and gf.get("enabled"):
            head += " +卦象"
        return head[:80]

    def _count_status_unlocked(self, status: str) -> int:
        return sum(1 for r in self._jobs.values() if r.status == status)

    def _queue_position_unlocked(self, job_id: str) -> int:
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

    def _refresh_queue_messages_unlocked(self) -> None:
        n_q = self._count_status_unlocked("queued")
        n_r = self._count_status_unlocked("running")
        for r in self._jobs.values():
            if r.status != "queued":
                continue
            pos = self._queue_position_unlocked(r.job_id)
            prog = dict(r.progress or {})
            prog["phase"] = "queued"
            prog["queue_position"] = pos
            prog["n_queued"] = n_q
            prog["n_running"] = n_r
            prog["message"] = (
                "排队中（前面还有 %d 个任务）" % (pos - 1)
                if pos > 1
                else ("排队中，即将开始" if n_r else "排队中")
            )
            prog["updated_at"] = time.time()
            r.progress = prog

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
            prog["queue_position"] = 0 if rec.status == "running" else self._queue_position_unlocked(job_id)
            prog["n_queued"] = self._count_status_unlocked("queued")
            prog["n_running"] = self._count_status_unlocked("running")
            rec.progress = prog
            rec.updated_at = time.time()

    def submit(self, req: BacktestRequest) -> JobRecord:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._lock:
            self._seq += 1
            seq = self._seq
            n_busy = self._count_status_unlocked("queued") + self._count_status_unlocked(
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
                _req_obj=req,
                progress={
                    "phase": "queued",
                    "pct": 0.0,
                    "current": 0,
                    "total": 0,
                    "message": (
                        "排队中（前面还有 %d 个任务）" % n_busy
                        if n_busy
                        else "排队中，即将开始"
                    ),
                    "code": None,
                    "queue_position": n_busy + 1,
                    "n_queued": self._count_status_unlocked("queued") + 1,
                    "n_running": self._count_status_unlocked("running"),
                    "updated_at": now,
                },
            )
            self._jobs[job_id] = rec
            self._refresh_queue_messages_unlocked()
        self._q.put(job_id)
        return rec

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            if job_id is None:
                self._q.task_done()
                break
            try:
                self._execute_job(job_id)
            finally:
                self._q.task_done()

    def _execute_job(self, job_id: str) -> None:
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec or rec.status == "cancelled":
                return
            req = rec._req_obj
            if req is None:
                # rebuild from request dict if needed
                try:
                    req = BacktestRequest(**(rec.request or {}))
                except Exception as e:  # noqa: BLE001
                    rec.status = "failed"
                    rec.error = "invalid request: %s" % e
                    rec.updated_at = time.time()
                    return
            rec.status = "running"
            rec.updated_at = time.time()
            rec.progress = {
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
            self._refresh_queue_messages_unlocked()

        def _progress(payload: Dict[str, Any]) -> None:
            self._set_progress(job_id, payload)

        try:
            svc = BacktestService(self.cfg)
            summary = svc.run(req, progress_cb=_progress)
            with self._lock:
                rec = self._jobs.get(job_id)
                if not rec or rec.status == "cancelled":
                    return
                rec.result = summary
                rec.run_id = summary.get("run_id")
                st = summary.get("status") or "ok"
                if st in ("no_go", "rejected_unconfirmed_formula") or summary.get("error"):
                    rec.status = "failed"
                    rec.error = summary.get("error") or summary.get("reason") or st
                    rec.progress = {
                        **(rec.progress or {}),
                        "phase": "failed",
                        "pct": float((rec.progress or {}).get("pct") or 0),
                        "message": rec.error or st,
                        "updated_at": time.time(),
                    }
                else:
                    rec.status = "succeeded"
                    rec.progress = {
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
                rec.updated_at = time.time()
                self._refresh_queue_messages_unlocked()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                rec = self._jobs.get(job_id)
                if not rec or rec.status == "cancelled":
                    return
                rec.status = "failed"
                rec.error = f"{e}\n{traceback.format_exc()}"
                rec.progress = {
                    "phase": "failed",
                    "pct": float((rec.progress or {}).get("pct") or 0),
                    "message": str(e),
                    "updated_at": time.time(),
                }
                rec.updated_at = time.time()
                self._refresh_queue_messages_unlocked()

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
        with self._lock:
            queued = sorted(
                [r for r in self._jobs.values() if r.status == "queued"],
                key=lambda r: r.queue_seq,
            )
            running = sorted(
                [r for r in self._jobs.values() if r.status == "running"],
                key=lambda r: r.queue_seq,
            )
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
            qpos = (
                self._queue_position_unlocked(rec.job_id)
                if rec.status == "queued"
                else 0
            )
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

    def shutdown(self, wait: bool = False) -> None:
        """Stop worker (for tests). Daemon thread also exits with process."""
        self._stop.set()
        self._q.put(None)
        if wait and self._worker.is_alive():
            self._worker.join(timeout=2.0)
