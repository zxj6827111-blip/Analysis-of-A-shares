# -*- coding: utf-8 -*-
"""High-level ResearchPlatform facade for enqueue / cancel / pause / workers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .queue_backend import MemoryQueueBackend, QueueBackend, SqliteQueueBackend
from .trial_store import TrialStore
from .worker import ResearchWorker


def default_platform_db_path(storage_root: str | Path) -> Path:
    return Path(storage_root) / "research_platform.db"


def _param_hash(params: dict) -> str:
    raw = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class ResearchPlatform:
    """
    Facade used by API later:
    - enqueue_trial / cancel_trial / pause / resume
    - worker_snapshot / queue_stats
    """

    def __init__(
        self,
        storage_root: str | Path,
        *,
        queue: Optional[QueueBackend] = None,
        trial_store: Optional[TrialStore] = None,
        default_queue: str = "research",
        use_memory_queue: bool = False,
    ):
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.db_path = default_platform_db_path(self.storage_root)
        self.default_queue = default_queue

        if queue is not None:
            self.queue = queue
        elif use_memory_queue:
            self.queue = MemoryQueueBackend()
        else:
            self.queue = SqliteQueueBackend(self.db_path)

        self.trial_store = trial_store or TrialStore(self.db_path)
        self._workers: Dict[str, ResearchWorker] = {}

    def close(self) -> None:
        if hasattr(self.queue, "close"):
            self.queue.close()
        if hasattr(self.trial_store, "close"):
            self.trial_store.close()

    def enqueue_trial(
        self,
        *,
        experiment_id: str,
        params: dict,
        idempotency_key: Optional[str] = None,
        queue: Optional[str] = None,
        max_attempts: int = 3,
        priority: int = 0,
        extra_payload: Optional[dict] = None,
    ) -> dict:
        """Create trial (idempotent) and enqueue a task. Returns trial + task info."""
        ph = _param_hash(params)
        key = idempotency_key or f"{experiment_id}:{ph}"
        trial = self.trial_store.insert_trial(
            experiment_id=experiment_id,
            idempotency_key=key,
            params=params,
            param_hash=ph,
            status="queued",
        )
        # If already linked to a task, return without re-enqueue
        if trial.get("task_id"):
            task = self.queue.get(trial["task_id"])
            return {"trial": trial, "task": task, "created": False}

        qname = queue or self.default_queue
        payload = {
            "trial_id": trial["trial_id"],
            "experiment_id": experiment_id,
            "params": params,
            "param_hash": ph,
            "idempotency_key": key,
        }
        if extra_payload:
            payload.update(extra_payload)

        task = self.queue.enqueue(
            qname,
            payload,
            idempotency_key=f"task:{key}",
            max_attempts=max_attempts,
            priority=priority,
        )
        trial = self.trial_store.update_status(
            trial["trial_id"],
            "queued",
            task_id=task["task_id"],
        ) or trial
        return {"trial": trial, "task": task, "created": True}

    def cancel_trial(self, trial_id: str) -> dict:
        trial = self.trial_store.get(trial_id)
        if not trial:
            return {"ok": False, "error": "trial_not_found"}
        task_id = trial.get("task_id")
        cancelled = False
        if task_id:
            cancelled = self.queue.cancel(task_id)
        self.trial_store.update_status(trial_id, "cancelled")
        return {
            "ok": True,
            "trial_id": trial_id,
            "task_cancelled": cancelled,
            "trial": self.trial_store.get(trial_id),
        }

    def pause(self, task_id: str) -> bool:
        return self.queue.pause(task_id)

    def resume(self, task_id: str) -> bool:
        return self.queue.resume(task_id)

    def make_worker(
        self,
        worker_id: str,
        handler: Callable[[dict], Any],
        *,
        queues: Optional[Sequence[str]] = None,
    ) -> ResearchWorker:
        w = ResearchWorker(
            worker_id=worker_id,
            queue=self.queue,
            trial_store=self.trial_store,
            handler=handler,
            queues=queues or [self.default_queue],
            default_queue=self.default_queue,
        )
        self._workers[worker_id] = w
        return w

    def worker_snapshot(self, worker_id: Optional[str] = None) -> dict:
        if worker_id:
            w = self._workers.get(worker_id)
            return w.snapshot() if w else {"worker_id": worker_id, "missing": True}
        return {wid: w.snapshot() for wid, w in self._workers.items()}

    def queue_stats(self, queue: Optional[str] = None) -> Dict[str, int]:
        return self.queue.stats(queue=queue or self.default_queue)

    def reclaim_stale(self, timeout_sec: float) -> int:
        return self.queue.reclaim_stale(timeout_sec)
