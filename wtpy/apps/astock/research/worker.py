# -*- coding: utf-8 -*-
"""Research worker: claim → handle → ack/nack with cancel checks."""
from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from .queue_backend import QueueBackend
from .trial_store import TrialStore

Handler = Callable[[dict], Any]


class ResearchWorker:
    """Single-step or loop worker over a QueueBackend."""

    def __init__(
        self,
        worker_id: str,
        queue: QueueBackend,
        trial_store: Optional[TrialStore] = None,
        handler: Optional[Handler] = None,
        *,
        queues: Optional[Sequence[str]] = None,
        default_queue: str = "default",
    ):
        self.worker_id = worker_id
        self.queue = queue
        self.trial_store = trial_store
        self.handler = handler
        self.default_queue = default_queue
        self.queues = list(queues) if queues else [default_queue]
        self.last_error: Optional[str] = None
        self.processed: int = 0

    def run_once(self, handler: Optional[Handler] = None) -> Optional[dict]:
        """
        Claim one task, run handler, ack on success, nack/retry on failure.
        Returns the task dict after processing, or None if no work.
        """
        h = handler or self.handler
        if h is None:
            raise ValueError("handler is required")

        task = self.queue.claim(self.worker_id, self.queues)
        if task is None:
            return None

        task_id = task["task_id"]
        # Cancelled between enqueue and process (status check)
        latest = self.queue.get(task_id)
        if latest and latest.get("status") == "cancelled":
            return latest

        # If cancelled while still only claimable as queued, claim would not happen;
        # also support cancel after claim by re-checking before handler
        if latest and latest.get("status") not in ("running",):
            return latest

        trial_id = (task.get("payload") or {}).get("trial_id")
        if self.trial_store and trial_id:
            self.trial_store.update_status(trial_id, "running", task_id=task_id)

        try:
            # mid-flight cancel check
            mid = self.queue.get(task_id)
            if mid and mid.get("status") == "cancelled":
                if self.trial_store and trial_id:
                    self.trial_store.update_status(trial_id, "cancelled")
                return mid

            result = h(task.get("payload") or {})

            # cancel after handler start still acknowledged only if still running
            post = self.queue.get(task_id)
            if post and post.get("status") == "cancelled":
                if self.trial_store and trial_id:
                    self.trial_store.update_status(trial_id, "cancelled")
                return post

            ok = self.queue.ack(task_id, self.worker_id, result=result)
            if ok and self.trial_store and trial_id:
                metrics = result if isinstance(result, dict) else {"result": result}
                self.trial_store.update_status(
                    trial_id, "succeeded", metrics=metrics, task_id=task_id
                )
            self.processed += 1
            self.last_error = None
            return self.queue.get(task_id)
        except Exception as exc:  # noqa: BLE001 — worker must not crash loop
            err = f"{type(exc).__name__}: {exc}"
            self.last_error = err
            self.queue.nack(task_id, self.worker_id, error=err, retry=True)
            after = self.queue.get(task_id)
            if self.trial_store and trial_id:
                st = (after or {}).get("status") or "failed"
                if st == "queued":
                    self.trial_store.update_status(trial_id, "queued", error=err, task_id=task_id)
                else:
                    self.trial_store.update_status(trial_id, "failed", error=err, task_id=task_id)
            return after

    def snapshot(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "queues": list(self.queues),
            "processed": self.processed,
            "last_error": self.last_error,
        }
