# -*- coding: utf-8 -*-
"""Phase 4 gate: durable task/trial platform without Redis/Postgres."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from wtpy.apps.astock.research.platform import ResearchPlatform
from wtpy.apps.astock.research.queue_backend import (
    MemoryQueueBackend,
    SqliteQueueBackend,
)
from wtpy.apps.astock.research.trial_store import TrialStore
from wtpy.apps.astock.research.worker import ResearchWorker


@pytest.fixture()
def tmp_store(tmp_path: Path) -> Path:
    root = tmp_path / "storage"
    root.mkdir(parents=True)
    return root


def test_enqueue_claim_ack_succeeds(tmp_store: Path):
    q = SqliteQueueBackend(tmp_store / "research_platform.db")
    try:
        task = q.enqueue("default", {"x": 1}, max_attempts=2)
        assert task["status"] == "queued"
        claimed = q.claim("w1", ["default"])
        assert claimed is not None
        assert claimed["task_id"] == task["task_id"]
        assert claimed["status"] == "running"
        assert claimed["attempts"] == 1
        assert q.ack(claimed["task_id"], "w1", result={"ok": True})
        done = q.get(task["task_id"])
        assert done["status"] == "succeeded"
    finally:
        q.close()


def test_idempotent_trial_insert(tmp_store: Path):
    store = TrialStore(tmp_store / "research_platform.db")
    try:
        a = store.insert_trial(
            experiment_id="exp1",
            idempotency_key="exp1:hashA",
            params={"n": 1},
            param_hash="hashA",
        )
        b = store.insert_trial(
            experiment_id="exp1",
            idempotency_key="exp1:hashA",
            params={"n": 1},
            param_hash="hashA",
        )
        assert a["trial_id"] == b["trial_id"]
        assert store.count_by_experiment("exp1") == 1
        rows = store.list_by_experiment("exp1")
        assert len(rows) == 1
    finally:
        store.close()


def test_cancel_while_queued_prevents_execution(tmp_store: Path):
    q = MemoryQueueBackend()
    executed = []

    def handler(payload):
        executed.append(payload)
        return {"ran": True}

    t = q.enqueue("research", {"trial_id": "t1"}, max_attempts=2)
    assert q.cancel(t["task_id"])
    got = q.get(t["task_id"])
    assert got["status"] == "cancelled"
    # claim should not pick cancelled
    claimed = q.claim("w1", ["research"])
    assert claimed is None
    w = ResearchWorker("w1", q, handler=handler, queues=["research"])
    assert w.run_once() is None
    assert executed == []


def test_nack_retry_then_fail(tmp_store: Path):
    q = MemoryQueueBackend()
    max_attempts = 3
    t = q.enqueue("q", {"n": 1}, max_attempts=max_attempts)

    for i in range(max_attempts):
        claimed = q.claim("w1", ["q"])
        assert claimed is not None
        assert claimed["attempts"] == i + 1
        ok = q.nack(claimed["task_id"], "w1", error=f"boom{i}", retry=True)
        assert ok
        cur = q.get(t["task_id"])
        if i + 1 < max_attempts:
            assert cur["status"] == "queued"
        else:
            assert cur["status"] == "failed"

    # no more claims
    assert q.claim("w1", ["q"]) is None
    final = q.get(t["task_id"])
    assert final["attempts"] == max_attempts
    assert final["status"] == "failed"


def test_reclaim_stale_running_becomes_claimable(tmp_store: Path):
    q = SqliteQueueBackend(tmp_store / "q.db")
    try:
        t = q.enqueue("default", {"x": 1}, max_attempts=5)
        claimed = q.claim("w_old", ["default"])
        assert claimed["status"] == "running"
        # force old heartbeat
        q._db.execute(
            "UPDATE research_tasks SET heartbeat_at = ? WHERE task_id = ?",
            (time.time() - 3600, t["task_id"]),
        )
        n = q.reclaim_stale(timeout_sec=60)
        assert n >= 1
        mid = q.get(t["task_id"])
        assert mid["status"] == "queued"
        again = q.claim("w_new", ["default"])
        assert again is not None
        assert again["task_id"] == t["task_id"]
        assert again["worker_id"] == "w_new"
        assert again["attempts"] == 2
    finally:
        q.close()


def test_pause_then_resume_re_enables_claim(tmp_store: Path):
    q = MemoryQueueBackend()
    t = q.enqueue("default", {"p": 1})
    assert q.pause(t["task_id"])
    assert q.get(t["task_id"])["status"] == "paused"
    assert q.claim("w1", ["default"]) is None
    assert q.resume(t["task_id"])
    assert q.get(t["task_id"])["status"] == "queued"
    claimed = q.claim("w1", ["default"])
    assert claimed is not None
    assert claimed["task_id"] == t["task_id"]


def test_sqlite_queue_durability_reopen(tmp_store: Path):
    db = tmp_store / "durable.db"
    q1 = SqliteQueueBackend(db)
    task = q1.enqueue("default", {"durable": True}, max_attempts=2)
    tid = task["task_id"]
    q1.close()

    q2 = SqliteQueueBackend(db)
    try:
        got = q2.get(tid)
        assert got is not None
        assert got["status"] == "queued"
        claimed = q2.claim("w_reopen", ["default"])
        assert claimed is not None
        assert claimed["task_id"] == tid
        assert q2.ack(tid, "w_reopen")
        assert q2.get(tid)["status"] == "succeeded"
    finally:
        q2.close()


def test_platform_enqueue_and_worker(tmp_store: Path):
    plat = ResearchPlatform(tmp_store, use_memory_queue=False)
    try:
        out = plat.enqueue_trial(experiment_id="e1", params={"a": 1})
        assert out["trial"]["trial_id"]
        # idempotent second call reuses trial
        out2 = plat.enqueue_trial(experiment_id="e1", params={"a": 1})
        assert out2["trial"]["trial_id"] == out["trial"]["trial_id"]

        def handler(payload):
            return {"score": 1.0, "trial_id": payload.get("trial_id")}

        w = plat.make_worker("worker-1", handler)
        done = w.run_once()
        assert done is not None
        assert done["status"] == "succeeded"
        snap = plat.worker_snapshot("worker-1")
        assert snap["processed"] == 1
        stats = plat.queue_stats()
        assert stats.get("succeeded", 0) >= 1
    finally:
        plat.close()
