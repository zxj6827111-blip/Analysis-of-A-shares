# -*- coding: utf-8 -*-
"""Phase-4 API smoke using FastAPI TestClient (no live Redis)."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

from fastapi.testclient import TestClient

from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.config import AStockConfig


def test_research_platform_api_enqueue_cancel_stats(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.output_root = tmp_path / "out"
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)

    r = client.get("/api/v1/research/queue")
    assert r.status_code == 200
    assert r.json().get("ok") is True

    r = client.post(
        "/api/v1/research/tasks",
        json={
            "experiment_id": "exp_api",
            "params": {"rule_ids": ["x"], "hold": 1},
            "idempotency_key": "api-key-1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    task = body.get("task") or {}
    task_id = task.get("task_id")
    assert task_id

    r2 = client.post(
        "/api/v1/research/tasks",
        json={
            "experiment_id": "exp_api",
            "params": {"rule_ids": ["x"], "hold": 1},
            "idempotency_key": "api-key-1",
        },
    )
    assert r2.status_code == 200
    assert r2.json().get("created") is False

    g = client.get(f"/api/v1/research/tasks/{task_id}")
    assert g.status_code == 200
    assert g.json()["task"]["task_id"] == task_id

    c = client.post(f"/api/v1/research/tasks/{task_id}/cancel")
    assert c.status_code == 200
    assert c.json().get("ok") is True

    rc = client.post("/api/v1/research/workers/reclaim", json={"timeout_sec": 1})
    assert rc.status_code == 200
    assert rc.json().get("ok") is True
