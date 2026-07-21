# -*- coding: utf-8 -*-
"""Trial record store with idempotent insert (SQLite)."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db_backend import SqliteDatabaseBackend

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_trials (
    trial_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    param_hash TEXT,
    params_json TEXT,
    status TEXT NOT NULL,
    task_id TEXT,
    metrics_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_trials_experiment
    ON research_trials(experiment_id, created_at);
"""

TRIAL_STATUSES = (
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex


def _row_to_trial(row: dict) -> dict:
    out = dict(row)
    for key, field in (("params", "params_json"), ("metrics", "metrics_json")):
        raw = out.get(field)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = None
        else:
            out.setdefault(key, None)
    return out


class TrialStore:
    """SQLite-backed trial registry."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db = SqliteDatabaseBackend(self.path)
        conn = self._db.connect()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        self._db.close()

    def insert_trial(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        params: Optional[dict] = None,
        param_hash: Optional[str] = None,
        status: str = "pending",
        task_id: Optional[str] = None,
        trial_id: Optional[str] = None,
    ) -> dict:
        """Insert trial; second insert with same key returns existing row."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        existing = self._db.fetchone(
            "SELECT * FROM research_trials WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if existing:
            return _row_to_trial(existing)

        tid = trial_id or _new_id()
        now = _now()
        params_json = (
            json.dumps(params, ensure_ascii=False, sort_keys=True) if params is not None else None
        )
        try:
            self._db.execute(
                """
                INSERT INTO research_trials (
                    trial_id, experiment_id, idempotency_key, param_hash,
                    params_json, status, task_id, metrics_json, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    tid,
                    experiment_id,
                    idempotency_key,
                    param_hash,
                    params_json,
                    status,
                    task_id,
                    now,
                    now,
                ),
            )
        except Exception:
            existing = self._db.fetchone(
                "SELECT * FROM research_trials WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing:
                return _row_to_trial(existing)
            raise
        row = self._db.fetchone(
            "SELECT * FROM research_trials WHERE trial_id = ?", (tid,)
        )
        return _row_to_trial(row or {})

    def get(self, trial_id: str) -> Optional[dict]:
        row = self._db.fetchone(
            "SELECT * FROM research_trials WHERE trial_id = ?", (trial_id,)
        )
        return _row_to_trial(row) if row else None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[dict]:
        row = self._db.fetchone(
            "SELECT * FROM research_trials WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        return _row_to_trial(row) if row else None

    def update_status(
        self,
        trial_id: str,
        status: str,
        *,
        error: Optional[str] = None,
        metrics: Optional[dict] = None,
        task_id: Optional[str] = None,
    ) -> Optional[dict]:
        now = _now()
        metrics_json = (
            json.dumps(metrics, ensure_ascii=False, default=str)
            if metrics is not None
            else None
        )
        # Build dynamic update
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if metrics is not None:
            sets.append("metrics_json = ?")
            params.append(metrics_json)
        if task_id is not None:
            sets.append("task_id = ?")
            params.append(task_id)
        params.append(trial_id)
        self._db.execute(
            f"UPDATE research_trials SET {', '.join(sets)} WHERE trial_id = ?",
            tuple(params),
        )
        return self.get(trial_id)

    def list_by_experiment(
        self,
        experiment_id: str,
        *,
        limit: int = 500,
    ) -> List[dict]:
        rows = self._db.fetchall(
            """
            SELECT * FROM research_trials
            WHERE experiment_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (experiment_id, int(limit)),
        )
        return [_row_to_trial(r) for r in rows]

    def count_by_experiment(self, experiment_id: str) -> int:
        row = self._db.fetchone(
            "SELECT COUNT(*) AS n FROM research_trials WHERE experiment_id = ?",
            (experiment_id,),
        )
        return int(row["n"]) if row else 0
