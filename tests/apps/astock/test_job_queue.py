# -*- coding: utf-8 -*-
"""FIFO task queue: second submit waits until first finishes."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.backtest import BacktestRequest
from wtpy.apps.astock.service.jobs import JobStore


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


def test_jobs_fifo_second_waits_for_first(cfg: AStockConfig):
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


def test_queue_snapshot_public_fields(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=1)
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
            assert snap["max_workers"] == 1
            assert "recent" in snap
    finally:
        store.shutdown(wait=False)


def test_ui_always_async_and_queue_bar():
    html = (
        Path(__file__).resolve().parents[3]
        / "wtpy"
        / "apps"
        / "astock"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert "async_mode: true" in html
    assert "taskQueueBar" in html
    assert "trackJobInBackground" in html
    assert "追加任务" in html or "可继续" in html
    assert "/api/v1/backtests/jobs/queue" in html
