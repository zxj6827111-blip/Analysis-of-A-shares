# -*- coding: utf-8 -*-
"""API-layer tests for same-hexagram / same-day-pillar routes."""

from __future__ import annotations

import time
from pathlib import Path


def test_same_gua_should_async():
    from wtpy.apps.astock.api_routes.bagua import _bq_same_gua_should_async

    assert _bq_same_gua_should_async(None) is True
    assert _bq_same_gua_should_async(["600000"]) is False
    assert _bq_same_gua_should_async([str(i) for i in range(51)]) is True


def test_export_job_thread_passes_ctx(monkeypatch):
    """Regression: export worker thread must receive (ctx, job_id, params).
    A missing ctx makes the thread raise TypeError on start and the job
    stays queued forever (full-market export appeared stuck)."""
    import threading
    from types import SimpleNamespace

    from wtpy.apps.astock.api_routes import bagua as br

    captured = {}
    class FakeThread:
        def __init__(self, *a, **k):
            captured["args"] = k.get("args")
        def start(self):
            pass

    monkeypatch.setattr(br._bq_threading, "Thread", FakeThread)
    jobs = {}
    lock = threading.Lock()
    ctx = SimpleNamespace(cfg=object(), bq_export_jobs=jobs, bq_export_lock=lock)

    br._bq_start_export_job(
        ctx,
        date="2024-01-03",
        periods=["WEEK", "MONTH"],
        adjust="raw",
        codes=None,
        all_stocks=True,
        limit=None,
    )
    args = captured.get("args")
    assert args is not None
    assert len(args) == 3, f"worker args must be (ctx, job_id, params), got {len(args)}"
    assert args[0] is ctx
    assert isinstance(args[1], str)
    assert isinstance(args[2], dict)


def test_same_gua_start_reuses_active_job(monkeypatch):
    """Same params while a job is queued/running -> reuse, never duplicate."""
    import threading
    from types import SimpleNamespace

    from wtpy.apps.astock.api_routes import bagua as br

    jobs = {}
    lock = threading.Lock()
    ctx = SimpleNamespace(cfg=object(), bq_export_jobs=jobs, bq_export_lock=lock)
    # do not actually spawn worker threads in the test
    monkeypatch.setattr(
        br._bq_threading,
        "Thread",
        lambda *_a, **_k: SimpleNamespace(start=lambda: None),
    )

    kw = dict(code="600000", date="2024-01-03", period="DAY", adjust="raw", scope=None, limit=None)
    r1 = br._bq_start_same_gua_job(ctx, **kw)
    r2 = br._bq_start_same_gua_job(ctx, **kw)
    assert r1["job_id"] == r2["job_id"]
    assert r2.get("reused") is True
    assert r2.get("status") in ("queued", "running")

    # different parameters -> brand new job
    r3 = br._bq_start_same_gua_job(ctx, **{**kw, "code": "000001"})
    assert r3["job_id"] != r1["job_id"]
    assert r3.get("reused") is None

    # once finished, the slot is reusable again
    with lock:
        jobs[r1["job_id"]]["status"] = "done"
    r4 = br._bq_start_same_gua_job(ctx, **kw)
    assert r4["job_id"] != r1["job_id"]
    assert r4.get("reused") is None


def test_same_gua_async_job_flow(tmp_path: Path):
    import pytest

    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # full-market (scope=None) -> background job
    r = client.post(
        "/api/v1/bagua/same-gua",
        json={"code": "600000", "date": "2024-01-03"},
        params={"async_mode": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("mode") == "async"
    job_id = body.get("job_id")
    assert job_id
    assert body.get("kind") == "same_gua"
    assert body.get("status") in ("queued", "running")

    # job status endpoint returns metadata (no result until done)
    j = client.get(f"/api/v1/bagua/same-gua/jobs/{job_id}")
    assert j.status_code == 200
    jb = j.json()
    assert jb.get("kind") == "same_gua"
    assert jb.get("code") == "600000"

    # result endpoint fails closed before done (or already done if the
    # background thread finished between the two requests)
    res = client.get(f"/api/v1/bagua/same-gua/jobs/{job_id}/result")
    assert res.status_code in (200, 409)

    # unknown job id -> 404
    assert client.get("/api/v1/bagua/same-gua/jobs/__missing__").status_code == 404


def test_same_gua_sync_small_scope(tmp_path: Path):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # small scope (< 50) stays synchronous; whatever the data availability
    # on this machine, the route must never return a 500.
    r = client.post(
        "/api/v1/bagua/same-gua",
        json={"code": "600000", "date": "2024-01-03", "scope": ["600000", "000001"]},
        params={"async_mode": "true"},
    )
    assert r.status_code < 500
    assert r.json() is not None


def test_same_rizhu_route_registered(tmp_path: Path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.service import bagua_query as bq

    # force an empty 日柱 table so the error path is deterministic regardless
    # of what is present on this machine's Desktop.
    monkeypatch.setattr(bq, "load_rizhu_map", lambda _p=None: {})
    monkeypatch.setattr(bq, "resolve_stock_name", lambda _c, _k, std_code="": "")

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    # empty 日柱 table -> structured ok=False, never 500
    r = client.get("/api/v1/bagua/same-rizhu?code=600000")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert "未在日柱表中找到" in body.get("error", "")
