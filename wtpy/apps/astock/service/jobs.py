"""In-process job store for async backtests with a FIFO multi-worker queue.

Design:
- Submit always returns immediately with status ``queued``.
- Up to ``max_workers`` dedicated worker threads pull jobs in order and run
  them concurrently (default 6, hard cap 8; runtime-adjustable via
  :meth:`JobStore.set_max_workers` with the persisted app setting winning
  over the ASTOCK_BT_MAX_WORKERS env fallback).
- Additional submits beyond capacity stay queued until a worker is free.
- Queue order is FIFO by submit sequence; parallel slots fill from the head.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import AStockConfig, get_default_config
from .backtest import BacktestRequest, BacktestService

# Product defaults: parallel backtests (6 mid-point on 8-core+ machines; hard cap 8).
DEFAULT_BT_MAX_WORKERS = 6
HARD_MAX_BT_WORKERS = 8


class _WorkerRetire:
    """Sentinel: exactly one worker should exit (resize-down).

    Distinct from the ``None`` shutdown sentinel, which re-signals its peers
    so the whole pool drains; retiring one extra worker must never cascade.
    """

    __slots__ = ()


_WORKER_RETIRE = _WorkerRetire()


def parse_env_bt_max_workers() -> Optional[int]:
    """ASTOCK_BT_MAX_WORKERS parsed + clamped; None when unset or invalid."""
    raw = (os.environ.get("ASTOCK_BT_MAX_WORKERS") or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return max(1, min(n, HARD_MAX_BT_WORKERS))


def resolve_bt_max_workers(
    explicit: Optional[int] = None, persisted: Optional[str] = None
) -> int:
    """Resolve worker count: explicit > persisted DB > env > default.

    An explicit non-integer raises ValueError naming the parameter and the
    valid range (env/persisted stay tolerant and fall back to the next
    source). All results are clamped to [1, HARD_MAX_BT_WORKERS].
    """
    if explicit is not None:
        try:
            n = int(explicit)
        except (TypeError, ValueError):
            raise ValueError(
                "invalid max_workers=%r: must be an integer in [1, %d]"
                % (explicit, HARD_MAX_BT_WORKERS)
            ) from None
    else:
        n = None
        if persisted is not None:
            try:
                n = int(str(persisted).strip())
            except (TypeError, ValueError):
                n = None
        if n is None:
            n = parse_env_bt_max_workers()
        if n is None:
            n = DEFAULT_BT_MAX_WORKERS
    return max(1, min(int(n or 1), HARD_MAX_BT_WORKERS))


def bt_max_workers_info(persisted: Optional[str] = None) -> Dict[str, Any]:
    """Effective concurrency + provenance for API payloads.

    Priority matches :func:`resolve_bt_max_workers`: persisted DB setting >
    ASTOCK_BT_MAX_WORKERS > default. ``source`` names the winning layer.
    """
    env = parse_env_bt_max_workers()
    db_val: Optional[int] = None
    if persisted is not None:
        try:
            db_val = int(str(persisted).strip())
        except (TypeError, ValueError):
            db_val = None
    if db_val is not None:
        n = max(1, min(db_val, HARD_MAX_BT_WORKERS))
        source = "db"
    elif env is not None:
        n = env
        source = "env"
    else:
        n = DEFAULT_BT_MAX_WORKERS
        source = "default"
    return {
        "max_workers": n,
        "hard_max_workers": HARD_MAX_BT_WORKERS,
        "default_workers": DEFAULT_BT_MAX_WORKERS,
        "env_override": env,
        "source": source,
    }


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
    def __init__(self, cfg: Optional[AStockConfig] = None, max_workers: Optional[int] = None):
        self.cfg = cfg or get_default_config()
        self.max_workers = resolve_bt_max_workers(max_workers)
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._worker_seq = 0
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._stop = threading.Event()
        self._workers: List[threading.Thread] = []
        for _ in range(self.max_workers):
            self._start_worker_unlocked()

    def _start_worker_unlocked(self) -> threading.Thread:
        """Start one worker; caller must hold ``self._lock``."""
        self._worker_seq += 1
        t = threading.Thread(
            target=self._worker_loop,
            name=f"astock-bt-queue-worker-{self._worker_seq}",
            daemon=True,
        )
        t.start()
        self._workers.append(t)
        return t

    def _prune_dead_workers_unlocked(self) -> None:
        """Drop exited worker threads; caller must hold ``self._lock``."""
        self._workers = [t for t in self._workers if t.is_alive()]

    def set_max_workers(self, n: int) -> int:
        """Resize the live worker pool, clamped to [1, HARD_MAX_BT_WORKERS].

        Growing starts the missing number of threads. Shrinking posts one
        retire sentinel per surplus worker; each sentinel retires exactly one
        worker and is never re-signalled, so a resize-down cannot cascade like
         the ``None`` shutdown sentinel. Returns the effective value.
        """
        if self._stop.is_set():
            # shutdown 之后不得再扩容：新线程会在循环条件处立即退出
            return self.max_workers
        n = max(1, min(int(n), HARD_MAX_BT_WORKERS))
        with self._lock:
            self._prune_dead_workers_unlocked()
            current = self.max_workers
            if n == current:
                return current
            if n > current:
                for _ in range(n - current):
                    self._start_worker_unlocked()
            else:
                for _ in range(current - n):
                    self._q.put_nowait(_WORKER_RETIRE)
            self.max_workers = n
            self._refresh_queue_messages_unlocked()
        return n

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
        slots = max(0, self.max_workers - n_r)
        for r in self._jobs.values():
            if r.status != "queued":
                continue
            pos = self._queue_position_unlocked(r.job_id)
            prog = dict(r.progress or {})
            prog["phase"] = "queued"
            prog["queue_position"] = pos
            prog["n_queued"] = n_q
            prog["n_running"] = n_r
            prog["max_workers"] = self.max_workers
            if pos <= slots:
                prog["message"] = "排队中，即将开始（并行槽位空闲）"
            elif pos > 1:
                ahead = pos - 1
                prog["message"] = (
                    "排队中（前面还有 %d 个任务，并行 %d/%d）"
                    % (ahead, n_r, self.max_workers)
                )
            else:
                prog["message"] = "排队中（并行 %d/%d）" % (n_r, self.max_workers)
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
            prog["max_workers"] = self.max_workers
            rec.progress = prog
            rec.updated_at = time.time()

    def submit(self, req: BacktestRequest) -> JobRecord:
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._lock:
            self._seq += 1
            seq = self._seq
            n_q = self._count_status_unlocked("queued")
            n_r = self._count_status_unlocked("running")
            free = max(0, self.max_workers - n_r)
            ahead = max(0, n_q + 1 - free)
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
                        "排队中，即将开始（并行槽位空闲）"
                        if ahead == 0
                        else "排队中（前面还有 %d 个任务，并行 %d/%d）"
                        % (ahead, n_r, self.max_workers)
                    ),
                    "code": None,
                    "queue_position": n_q + 1,
                    "n_queued": n_q + 1,
                    "n_running": n_r,
                    "max_workers": self.max_workers,
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
                # re-signal other workers to exit
                try:
                    self._q.put_nowait(None)
                except Exception:
                    pass
                break
            if job_id is _WORKER_RETIRE:
                # Resize-down marker: retire this worker only, never cascade.
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
                "message": "任务启动（并行 %d/%d）"
                % (self._count_status_unlocked("running"), self.max_workers),
                "code": None,
                "queue_position": 0,
                "n_queued": self._count_status_unlocked("queued"),
                "n_running": self._count_status_unlocked("running"),
                "max_workers": self.max_workers,
                "updated_at": time.time(),
            }
            self._refresh_queue_messages_unlocked()

        def _progress(payload: Dict[str, Any]) -> None:
            # Cooperative cancel: raise so run_backtest unwinds at next progress tick.
            if self.is_cancelled(job_id):
                raise InterruptedError("job cancelled by user")
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
                        "max_workers": self.max_workers,
                        "updated_at": time.time(),
                    }
                rec.updated_at = time.time()
                self._refresh_queue_messages_unlocked()
        except InterruptedError:
            with self._lock:
                rec = self._jobs.get(job_id)
                if not rec:
                    return
                rec.status = "cancelled"
                rec.error = rec.error or "用户取消"
                rec.progress = {
                    **(rec.progress or {}),
                    "phase": "cancelled",
                    "message": "已取消",
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

    def cancel(self, job_id: str) -> JobRecord:
        """Cancel a queued or running job.

        Queued jobs are marked cancelled and skipped by workers.
        Running jobs are flagged; progress callbacks raise so the worker exits
        cooperatively (best-effort; may finish current heavy step first).
        """
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                raise KeyError(job_id)
            st = rec.status
            if st in ("succeeded", "failed", "cancelled"):
                return rec
            was_running = st == "running"
            rec.status = "cancelled"
            rec.updated_at = time.time()
            rec.error = rec.error or ("用户取消（运行中）" if was_running else "用户取消（排队中）")
            rec.progress = {
                **(rec.progress or {}),
                "phase": "cancelled",
                "message": rec.error,
                "updated_at": time.time(),
                "n_queued": self._count_status_unlocked("queued"),
                "n_running": self._count_status_unlocked("running"),
                "max_workers": self.max_workers,
            }
            self._refresh_queue_messages_unlocked()
            return rec

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            rec = self._jobs.get(job_id)
            return bool(rec and rec.status == "cancelled")

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
                "hard_max_workers": HARD_MAX_BT_WORKERS,
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
        prog.setdefault("max_workers", self.max_workers)
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
        """Stop workers (for tests). Daemon threads also exit with process."""
        self._stop.set()
        with self._lock:
            self._prune_dead_workers_unlocked()
            workers = list(self._workers)
        for _ in workers:
            try:
                self._q.put_nowait(None)
            except Exception:
                self._q.put(None)
        if wait:
            for t in workers:
                if t.is_alive():
                    t.join(timeout=2.0)
