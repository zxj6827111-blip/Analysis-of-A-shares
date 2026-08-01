# -*- coding: utf-8 -*-
"""FIFO task queue: second submit waits until first finishes."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.backtest import BacktestRequest
from wtpy.apps.astock.service.jobs import JobStore, resolve_bt_max_workers


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "out"
    c.storage_root = tmp_path / "store"
    c.output_root.mkdir(parents=True)
    c.storage_root.mkdir(parents=True)
    return c


def _wait(store: JobStore, job_id: str, statuses, timeout: float = 3.0) -> str:
    if isinstance(statuses, str):
        statuses = {statuses}
    else:
        statuses = set(statuses)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = store.get(job_id).status
        if last in statuses:
            return last
        time.sleep(0.02)
    return last


def test_jobs_serial_second_waits_when_workers_1(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=1)
    order: list[str] = []
    hold = {"A": True}

    def fake_run(self, req, progress_cb=None):
        rid = req.rule_ids[0]
        order.append("start:" + rid)
        if rid == "rule_A":
            t0 = time.time()
            while hold["A"] and time.time() - t0 < 2.5:
                time.sleep(0.02)
        order.append("end:" + rid)
        return {"run_id": "bt_" + rid, "status": "ok", "title": rid, "metrics": {}}

    try:
        with patch("wtpy.apps.astock.service.jobs.BacktestService.run", fake_run):
            ja = store.submit(BacktestRequest(rule_ids=["rule_A"], period="DAY"))
            jb = store.submit(BacktestRequest(rule_ids=["rule_B"], period="DAY"))
            assert _wait(store, ja.job_id, "running") == "running"
            time.sleep(0.05)
            assert store.get(jb.job_id).status == "queued"
            snap = store.queue_snapshot()
            assert snap["n_running"] == 1
            assert snap["n_queued"] >= 1
            assert snap["queued"][0]["job_id"] == jb.job_id
            hold["A"] = False
            assert _wait(store, ja.job_id, "succeeded") == "succeeded"
            assert _wait(store, jb.job_id, "succeeded") == "succeeded"
            assert order[0] == "start:rule_A"
            assert order.index("end:rule_A") < order.index("start:rule_B")
    finally:
        hold["A"] = False
        store.shutdown(wait=False)


def test_jobs_parallel_two_workers_run_together(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=2)
    started = threading.Event()
    both_running = threading.Event()
    hold = {"go": False}
    running_now = {"n": 0}
    lock = threading.Lock()

    def fake_run(self, req, progress_cb=None):
        with lock:
            running_now["n"] += 1
            if running_now["n"] >= 2:
                both_running.set()
        started.set()
        t0 = time.time()
        while not hold["go"] and time.time() - t0 < 2.5:
            time.sleep(0.02)
        with lock:
            running_now["n"] -= 1
        return {"run_id": "bt_" + req.rule_ids[0], "status": "ok", "metrics": {}}

    try:
        with patch("wtpy.apps.astock.service.jobs.BacktestService.run", fake_run):
            ja = store.submit(BacktestRequest(rule_ids=["rule_A"], period="DAY"))
            jb = store.submit(BacktestRequest(rule_ids=["rule_B"], period="DAY"))
            assert both_running.wait(2.0), "expected two jobs running in parallel"
            snap = store.queue_snapshot()
            assert snap["max_workers"] == 2
            assert snap["n_running"] == 2
            hold["go"] = True
            assert _wait(store, ja.job_id, "succeeded") == "succeeded"
            assert _wait(store, jb.job_id, "succeeded") == "succeeded"
    finally:
        hold["go"] = True
        store.shutdown(wait=False)


def test_queue_snapshot_public_fields(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=2)
    try:
        with patch(
            "wtpy.apps.astock.service.jobs.BacktestService.run",
            return_value={"run_id": "bt_x", "status": "ok", "metrics": {}},
        ):
            rec = store.submit(BacktestRequest(rule_ids=["x735"], period="DAY"))
            pub = store.to_public(rec)
            assert pub["job_id"]
            assert "title_hint" in pub
            assert "queue_seq" in pub
            assert _wait(store, rec.job_id, "succeeded") == "succeeded"
            snap = store.queue_snapshot()
            assert snap["max_workers"] == 2
            assert snap.get("hard_max_workers") == 8
            assert "recent" in snap
    finally:
        store.shutdown(wait=False)


def test_resolve_bt_max_workers_invalid_explicit_raises():
    with pytest.raises(ValueError, match="max_workers"):
        resolve_bt_max_workers("abc")
    with pytest.raises(ValueError, match="max_workers"):
        resolve_bt_max_workers([])


def test_resolve_bt_max_workers_valid_and_clamped():
    assert resolve_bt_max_workers(4) == 4
    assert resolve_bt_max_workers("6") == 6
    assert resolve_bt_max_workers(0) == 1
    assert resolve_bt_max_workers(99) == 8


def test_cancel_queued_job(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=1)
    hold = {"A": True}

    def fake_run(self, req, progress_cb=None):
        rid = req.rule_ids[0]
        if rid == "rule_A":
            t0 = time.time()
            while hold["A"] and time.time() - t0 < 2.5:
                if progress_cb:
                    progress_cb({"phase": "signals", "pct": 10, "message": "hold"})
                time.sleep(0.02)
        return {"run_id": "bt_" + rid, "status": "ok", "metrics": {}}

    try:
        with patch("wtpy.apps.astock.service.jobs.BacktestService.run", fake_run):
            ja = store.submit(BacktestRequest(rule_ids=["rule_A"], period="DAY"))
            jb = store.submit(BacktestRequest(rule_ids=["rule_B"], period="DAY"))
            assert _wait(store, ja.job_id, "running") == "running"
            assert store.get(jb.job_id).status == "queued"
            rec = store.cancel(jb.job_id)
            assert rec.status == "cancelled"
            hold["A"] = False
            assert _wait(store, ja.job_id, "succeeded") == "succeeded"
            assert store.get(jb.job_id).status == "cancelled"
    finally:
        hold["A"] = False
        store.shutdown(wait=False)


def test_cancel_running_job_cooperative(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=1)

    def fake_run(self, req, progress_cb=None):
        # Simulate long run with progress ticks; cancel raises InterruptedError.
        for i in range(50):
            if progress_cb:
                progress_cb({"phase": "signals", "pct": i, "message": "tick"})
            time.sleep(0.02)
        return {"run_id": "bt_long", "status": "ok", "metrics": {}}

    try:
        with patch("wtpy.apps.astock.service.jobs.BacktestService.run", fake_run):
            ja = store.submit(BacktestRequest(rule_ids=["rule_long"], period="DAY"))
            assert _wait(store, ja.job_id, "running") == "running"
            store.cancel(ja.job_id)
            st = _wait(store, ja.job_id, ("cancelled", "succeeded", "failed"), timeout=3.0)
            assert st == "cancelled"
    finally:
        store.shutdown(wait=False)


def test_ui_always_async_and_queue_bar():
    # Product console is index_v3.html (legacy index.html is not the live path).
    root = Path(__file__).resolve().parents[3] / "wtpy" / "apps" / "astock" / "web" / "static"
    html = (root / "index_v3.html").read_text(encoding="utf-8")
    assert "async_mode: true" in html
    assert "taskQueueBar" in html
    assert "/api/v1/backtests/jobs/queue" in html
    assert "/api/v1/backtests/jobs/" in html
    assert "execution_data_source" in html and "local_vendor" in html
