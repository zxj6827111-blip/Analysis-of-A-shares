# -*- coding: utf-8 -*-
"""Task queue backends: memory (tests) + SQLite (durable). Redis hook later."""
from __future__ import annotations

import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .db_backend import SqliteDatabaseBackend

TASK_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "paused",
)


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex


def _row_to_task(row: dict) -> dict:
    out = dict(row)
    payload = out.get("payload_json")
    if isinstance(payload, str):
        try:
            out["payload"] = json.loads(payload)
        except json.JSONDecodeError:
            out["payload"] = {}
    elif "payload" not in out:
        out["payload"] = payload if isinstance(payload, dict) else {}
    return out


class QueueBackend(ABC):
    """Abstract task queue."""

    @abstractmethod
    def enqueue(
        self,
        queue: str,
        payload: dict,
        *,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
        priority: int = 0,
        task_id: Optional[str] = None,
    ) -> dict:
        ...

    @abstractmethod
    def claim(self, worker_id: str, queues: Sequence[str]) -> Optional[dict]:
        ...

    @abstractmethod
    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        ...

    @abstractmethod
    def ack(self, task_id: str, worker_id: str, result: Any = None) -> bool:
        ...

    @abstractmethod
    def nack(
        self,
        task_id: str,
        worker_id: str,
        error: str = "",
        *,
        retry: bool = True,
    ) -> bool:
        ...

    @abstractmethod
    def cancel(self, task_id: str) -> bool:
        ...

    @abstractmethod
    def pause(self, task_id: str) -> bool:
        ...

    @abstractmethod
    def resume(self, task_id: str) -> bool:
        ...

    @abstractmethod
    def get(self, task_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def list_by_status(
        self,
        status: str,
        *,
        queue: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        ...

    def reclaim_stale(self, timeout_sec: float) -> int:
        """Re-queue running tasks whose heartbeat is older than timeout_sec."""
        return 0

    def close(self) -> None:
        pass

    def stats(self, queue: Optional[str] = None) -> Dict[str, int]:
        counts: Dict[str, int] = {s: 0 for s in TASK_STATUSES}
        for s in TASK_STATUSES:
            rows = self.list_by_status(s, queue=queue, limit=10_000)
            counts[s] = len(rows)
        return counts


class MemoryQueueBackend(QueueBackend):
    """In-process queue for unit tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, dict] = {}
        self._by_idem: Dict[str, str] = {}

    def enqueue(
        self,
        queue: str,
        payload: dict,
        *,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
        priority: int = 0,
        task_id: Optional[str] = None,
    ) -> dict:
        with self._lock:
            if idempotency_key and idempotency_key in self._by_idem:
                existing = self._tasks[self._by_idem[idempotency_key]]
                return deepcopy(existing)
            tid = task_id or _new_id()
            now = _now()
            task = {
                "task_id": tid,
                "queue": queue,
                "payload": dict(payload or {}),
                "payload_json": json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                "status": "queued",
                "idempotency_key": idempotency_key,
                "worker_id": None,
                "attempts": 0,
                "max_attempts": int(max_attempts),
                "error": None,
                "result": None,
                "created_at": now,
                "updated_at": now,
                "heartbeat_at": None,
                "priority": int(priority),
            }
            self._tasks[tid] = task
            if idempotency_key:
                self._by_idem[idempotency_key] = tid
            return deepcopy(task)

    def claim(self, worker_id: str, queues: Sequence[str]) -> Optional[dict]:
        with self._lock:
            qset = set(queues)
            candidates = [
                t
                for t in self._tasks.values()
                if t["status"] == "queued" and t["queue"] in qset
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda t: (-int(t.get("priority") or 0), t["created_at"]))
            t = candidates[0]
            now = _now()
            t["status"] = "running"
            t["worker_id"] = worker_id
            t["attempts"] = int(t.get("attempts") or 0) + 1
            t["updated_at"] = now
            t["heartbeat_at"] = now
            return deepcopy(t)

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "running" or t.get("worker_id") != worker_id:
                return False
            now = _now()
            t["heartbeat_at"] = now
            t["updated_at"] = now
            return True

    def ack(self, task_id: str, worker_id: str, result: Any = None) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "running" or t.get("worker_id") != worker_id:
                return False
            now = _now()
            t["status"] = "succeeded"
            t["result"] = result
            t["error"] = None
            t["updated_at"] = now
            return True

    def nack(
        self,
        task_id: str,
        worker_id: str,
        error: str = "",
        *,
        retry: bool = True,
    ) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "running" or t.get("worker_id") != worker_id:
                return False
            now = _now()
            t["error"] = error or ""
            t["updated_at"] = now
            attempts = int(t.get("attempts") or 0)
            max_a = int(t.get("max_attempts") or 1)
            if retry and attempts < max_a:
                t["status"] = "queued"
                t["worker_id"] = None
                t["heartbeat_at"] = None
            else:
                t["status"] = "failed"
                t["worker_id"] = None
            return True

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            if t["status"] in ("succeeded", "failed", "cancelled"):
                return t["status"] == "cancelled"
            t["status"] = "cancelled"
            t["updated_at"] = _now()
            t["worker_id"] = None
            return True

    def pause(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "queued":
                return False
            t["status"] = "paused"
            t["updated_at"] = _now()
            return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "paused":
                return False
            t["status"] = "queued"
            t["updated_at"] = _now()
            return True

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            t = self._tasks.get(task_id)
            return deepcopy(t) if t else None

    def list_by_status(
        self,
        status: str,
        *,
        queue: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        with self._lock:
            rows = [
                deepcopy(t)
                for t in self._tasks.values()
                if t["status"] == status and (queue is None or t["queue"] == queue)
            ]
            rows.sort(key=lambda t: t["created_at"])
            return rows[:limit]

    def reclaim_stale(self, timeout_sec: float) -> int:
        with self._lock:
            now = _now()
            n = 0
            for t in self._tasks.values():
                if t["status"] != "running":
                    continue
                hb = t.get("heartbeat_at")
                if hb is None or (now - float(hb)) >= float(timeout_sec):
                    t["status"] = "queued"
                    t["worker_id"] = None
                    t["heartbeat_at"] = None
                    t["updated_at"] = now
                    n += 1
            return n


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_tasks (
    task_id TEXT PRIMARY KEY,
    queue TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    worker_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    heartbeat_at REAL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_research_tasks_status_queue
    ON research_tasks(status, queue, priority DESC, created_at);
"""


class SqliteQueueBackend(QueueBackend):
    """Durable SQLite task queue (single-process atomic claim via UPDATE...WHERE)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db = SqliteDatabaseBackend(self.path)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = self._db.connect()
        conn.executescript(_SQLITE_SCHEMA)
        conn.commit()

    def close(self) -> None:
        self._db.close()

    def enqueue(
        self,
        queue: str,
        payload: dict,
        *,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
        priority: int = 0,
        task_id: Optional[str] = None,
    ) -> dict:
        with self._lock:
            if idempotency_key:
                existing = self._db.fetchone(
                    "SELECT * FROM research_tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                if existing:
                    return _row_to_task(existing)
            tid = task_id or _new_id()
            now = _now()
            payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
            try:
                self._db.execute(
                    """
                    INSERT INTO research_tasks (
                        task_id, queue, payload_json, status, idempotency_key,
                        worker_id, attempts, max_attempts, error, result_json,
                        created_at, updated_at, heartbeat_at, priority
                    ) VALUES (?, ?, ?, 'queued', ?, NULL, 0, ?, NULL, NULL, ?, ?, NULL, ?)
                    """,
                    (
                        tid,
                        queue,
                        payload_json,
                        idempotency_key,
                        int(max_attempts),
                        now,
                        now,
                        int(priority),
                    ),
                )
            except Exception:
                # race on unique idempotency_key
                if idempotency_key:
                    existing = self._db.fetchone(
                        "SELECT * FROM research_tasks WHERE idempotency_key = ?",
                        (idempotency_key,),
                    )
                    if existing:
                        return _row_to_task(existing)
                raise
            row = self._db.fetchone(
                "SELECT * FROM research_tasks WHERE task_id = ?", (tid,)
            )
            return _row_to_task(row or {})

    def claim(self, worker_id: str, queues: Sequence[str]) -> Optional[dict]:
        if not queues:
            return None
        with self._lock:
            # pick candidate then atomic update
            placeholders = ",".join("?" * len(queues))
            rows = self._db.fetchall(
                f"""
                SELECT task_id FROM research_tasks
                WHERE status = 'queued' AND queue IN ({placeholders})
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                tuple(queues),
            )
            if not rows:
                return None
            tid = rows[0]["task_id"]
            now = _now()
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'running',
                    worker_id = ?,
                    attempts = attempts + 1,
                    updated_at = ?,
                    heartbeat_at = ?
                WHERE task_id = ? AND status = 'queued'
                """,
                (worker_id, now, now, tid),
            )
            if cur.rowcount != 1:
                return None
            row = self._db.fetchone(
                "SELECT * FROM research_tasks WHERE task_id = ?", (tid,)
            )
            return _row_to_task(row) if row else None

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        with self._lock:
            now = _now()
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET heartbeat_at = ?, updated_at = ?
                WHERE task_id = ? AND status = 'running' AND worker_id = ?
                """,
                (now, now, task_id, worker_id),
            )
            return cur.rowcount == 1

    def ack(self, task_id: str, worker_id: str, result: Any = None) -> bool:
        with self._lock:
            now = _now()
            result_json = (
                json.dumps(result, ensure_ascii=False, default=str)
                if result is not None
                else None
            )
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'succeeded', error = NULL, result_json = ?,
                    updated_at = ?
                WHERE task_id = ? AND status = 'running' AND worker_id = ?
                """,
                (result_json, now, task_id, worker_id),
            )
            return cur.rowcount == 1

    def nack(
        self,
        task_id: str,
        worker_id: str,
        error: str = "",
        *,
        retry: bool = True,
    ) -> bool:
        with self._lock:
            row = self._db.fetchone(
                "SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)
            )
            if not row or row["status"] != "running" or row.get("worker_id") != worker_id:
                return False
            now = _now()
            attempts = int(row.get("attempts") or 0)
            max_a = int(row.get("max_attempts") or 1)
            if retry and attempts < max_a:
                cur = self._db.execute(
                    """
                    UPDATE research_tasks
                    SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                        error = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'running' AND worker_id = ?
                    """,
                    (error or "", now, task_id, worker_id),
                )
            else:
                cur = self._db.execute(
                    """
                    UPDATE research_tasks
                    SET status = 'failed', worker_id = NULL,
                        error = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'running' AND worker_id = ?
                    """,
                    (error or "", now, task_id, worker_id),
                )
            return cur.rowcount == 1

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            row = self._db.fetchone(
                "SELECT status FROM research_tasks WHERE task_id = ?", (task_id,)
            )
            if not row:
                return False
            if row["status"] in ("succeeded", "failed", "cancelled"):
                return row["status"] == "cancelled"
            now = _now()
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'cancelled', worker_id = NULL, updated_at = ?
                WHERE task_id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled')
                """,
                (now, task_id),
            )
            return cur.rowcount == 1

    def pause(self, task_id: str) -> bool:
        with self._lock:
            now = _now()
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'paused', updated_at = ?
                WHERE task_id = ? AND status = 'queued'
                """,
                (now, task_id),
            )
            return cur.rowcount == 1

    def resume(self, task_id: str) -> bool:
        with self._lock:
            now = _now()
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'queued', updated_at = ?
                WHERE task_id = ? AND status = 'paused'
                """,
                (now, task_id),
            )
            return cur.rowcount == 1

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.fetchone(
                "SELECT * FROM research_tasks WHERE task_id = ?", (task_id,)
            )
            return _row_to_task(row) if row else None

    def list_by_status(
        self,
        status: str,
        *,
        queue: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        with self._lock:
            if queue is None:
                rows = self._db.fetchall(
                    """
                    SELECT * FROM research_tasks
                    WHERE status = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (status, int(limit)),
                )
            else:
                rows = self._db.fetchall(
                    """
                    SELECT * FROM research_tasks
                    WHERE status = ? AND queue = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (status, queue, int(limit)),
                )
            return [_row_to_task(r) for r in rows]

    def reclaim_stale(self, timeout_sec: float) -> int:
        with self._lock:
            cutoff = _now() - float(timeout_sec)
            now = _now()
            # NULL heartbeat treated as stale
            cur = self._db.execute(
                """
                UPDATE research_tasks
                SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE status = 'running'
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (now, cutoff),
            )
            return int(cur.rowcount or 0)
