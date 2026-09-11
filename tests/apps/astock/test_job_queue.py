# -*- coding: utf-8 -*-
"""FIFO task queue: second submit waits until first finishes."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig, get_default_config
from wtpy.apps.astock.service.backtest import BacktestRequest
from wtpy.apps.astock.service.db import (
    connect,
    get_app_setting,
    init_db,
    set_app_setting,
)
from wtpy.apps.astock.service.jobs import (
    DEFAULT_BT_MAX_WORKERS,
    HARD_MAX_BT_WORKERS,
    JobStore,
    bt_max_workers_info,
    resolve_bt_max_workers,
)


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "out"
    c.storage_root = tmp_path / "store"
    c.output_root.mkdir(parents=True)
    c.storage_root.mkdir(parents=True)
    return c


@pytest.fixture()
def api_cfg(tmp_path: Path) -> AStockConfig:
    storage = tmp_path / "api_store"
    indicator = tmp_path / "api_ind"
    storage.mkdir()
    indicator.mkdir()
    return get_default_config(
        storage_root=storage,
        indicator_dir=indicator,
        output_root=tmp_path / "api_out",
    )


def _make_client(cfg: AStockConfig):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    app = create_app(cfg)
    return app, TestClient(app)


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


# ---------------------------------------------------------------------------
# Configurable backtest concurrency (persisted app setting + runtime resize)
# ---------------------------------------------------------------------------


def test_resolve_bt_max_workers_persisted_and_env(monkeypatch):
    monkeypatch.delenv("ASTOCK_BT_MAX_WORKERS", raising=False)
    assert resolve_bt_max_workers() == DEFAULT_BT_MAX_WORKERS == 6
    assert resolve_bt_max_workers(None, persisted="3") == 3
    assert resolve_bt_max_workers(2, persisted="3") == 2
    monkeypatch.setenv("ASTOCK_BT_MAX_WORKERS", "5")
    assert resolve_bt_max_workers(None, persisted="3") == 3
    assert resolve_bt_max_workers(None) == 5
    monkeypatch.setenv("ASTOCK_BT_MAX_WORKERS", "99")
    assert resolve_bt_max_workers(None) == HARD_MAX_BT_WORKERS == 8
    monkeypatch.setenv("ASTOCK_BT_MAX_WORKERS", "abc")
    assert resolve_bt_max_workers(None, persisted="bad") == DEFAULT_BT_MAX_WORKERS


def test_bt_max_workers_info_sources(monkeypatch):
    monkeypatch.delenv("ASTOCK_BT_MAX_WORKERS", raising=False)
    assert bt_max_workers_info() == {
        "max_workers": 6,
        "hard_max_workers": 8,
        "default_workers": 6,
        "env_override": None,
        "source": "default",
    }
    monkeypatch.setenv("ASTOCK_BT_MAX_WORKERS", "5")
    env_info = bt_max_workers_info()
    assert env_info["source"] == "env"
    assert env_info["max_workers"] == 5
    assert env_info["env_override"] == 5
    db_info = bt_max_workers_info("3")
    assert db_info["source"] == "db"
    assert db_info["max_workers"] == 3
    assert db_info["env_override"] == 5


def test_app_setting_roundtrip_and_unconditional_ddl(cfg: AStockConfig):
    # Missing table (fresh/legacy DB) must degrade to None, never raise.
    assert get_app_setting(cfg, "bt_max_workers") is None
    set_app_setting(cfg, "bt_max_workers", "3")
    assert get_app_setting(cfg, "bt_max_workers") == "3"
    set_app_setting(cfg, "bt_max_workers", "5")
    assert get_app_setting(cfg, "bt_max_workers") == "5"

    # Legacy DB path: app_settings dropped must come back on init_db.
    init_db(cfg)
    conn = connect(cfg)
    conn.execute("DROP TABLE app_settings")
    conn.commit()
    conn.close()
    assert get_app_setting(cfg, "bt_max_workers") is None
    set_app_setting(cfg, "bt_max_workers", "4")
    assert get_app_setting(cfg, "bt_max_workers") == "4"


def test_set_max_workers_expand(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=1)
    try:
        assert store.set_max_workers(1) == 1
        assert store.set_max_workers(3) == 3
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if len([t for t in store._workers if t.is_alive()]) == 3:
                break
            time.sleep(0.02)
        alive = [t for t in store._workers if t.is_alive()]
        assert len(alive) == 3
        assert store.max_workers == 3
        assert store.queue_snapshot()["max_workers"] == 3
        names = [t.name for t in store._workers]
        assert len(names) == len(set(names))
    finally:
        store.shutdown(wait=False)


def test_set_max_workers_shrink_retires_without_cascade(cfg: AStockConfig):
    store = JobStore(cfg, max_workers=3)
    try:
        assert store.set_max_workers(1) == 1
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if len([t for t in store._workers if t.is_alive()]) == 1:
                break
            time.sleep(0.02)
        alive = [t for t in store._workers if t.is_alive()]
        assert len(alive) == 1, "resize-down must retire exactly the surplus"
        assert store.max_workers == 1

        with patch(
            "wtpy.apps.astock.service.jobs.BacktestService.run",
            return_value={"run_id": "bt_resize", "status": "ok", "metrics": {}},
        ):
            rec = store.submit(BacktestRequest(rule_ids=["x"], period="DAY"))
            assert _wait(store, rec.job_id, "succeeded") == "succeeded"
    finally:
        store.shutdown(wait=True)
    assert not [t for t in store._workers if t.is_alive()]


def test_set_max_workers_after_shutdown_stays_put(cfg: AStockConfig):
    """shutdown 后扩容请求直接返回当前值，不得再生产会立即退出的线程。"""
    store = JobStore(cfg, max_workers=1)
    store.shutdown(wait=True)
    assert store.set_max_workers(4) == 1
    assert store.max_workers == 1
    assert not [t for t in store._workers if t.is_alive()]


def test_parse_max_workers_accepts_integral_float():
    """_parse_max_workers 接受整数值 float（2.5/True 仍拒绝），docstring 对齐。"""
    pytest.importorskip("fastapi")
    from wtpy.apps.astock.api_routes.backtests import _parse_max_workers

    assert _parse_max_workers(3.0) == 3
    assert _parse_max_workers(3) == 3
    assert _parse_max_workers("3") == 3
    with pytest.raises(ValueError):
        _parse_max_workers(2.5)
    with pytest.raises(ValueError):
        _parse_max_workers(True)


def test_queue_config_api_get_put_clamp_and_400(api_cfg: AStockConfig, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.delenv("ASTOCK_BT_MAX_WORKERS", raising=False)
    app, client = _make_client(api_cfg)
    try:
        r = client.get("/api/v1/backtests/queue/config")
        assert r.status_code == 200
        d = r.json()
        assert d == {
            "max_workers": 6,
            "hard_max_workers": 8,
            "default_workers": 6,
            "env_override": None,
            "source": "default",
        }

        r = client.put("/api/v1/backtests/queue/config", json={"max_workers": 3})
        assert r.status_code == 200
        d = r.json()
        assert d["max_workers"] == 3
        assert d["source"] == "db"
        assert app.state.astock.jobs.max_workers == 3
        assert get_app_setting(api_cfg, "bt_max_workers") == "3"

        d = client.get("/api/v1/backtests/queue/config").json()
        assert d["max_workers"] == 3 and d["source"] == "db"

        assert client.put(
            "/api/v1/backtests/queue/config", json={"max_workers": 8}
        ).status_code == 200
        for bad in (0, 9, -1, "abc", None, 2.5, True, {}):
            r = client.put(
                "/api/v1/backtests/queue/config", json={"max_workers": bad}
            )
            assert r.status_code == 400, bad
        assert get_app_setting(api_cfg, "bt_max_workers") == "8"
    finally:
        app.state.astock.jobs.shutdown(wait=False)


def test_queue_config_db_beats_env_at_startup(api_cfg: AStockConfig, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("ASTOCK_BT_MAX_WORKERS", "5")
    set_app_setting(api_cfg, "bt_max_workers", "2")
    app, client = _make_client(api_cfg)
    try:
        assert app.state.astock.jobs.max_workers == 2
        d = client.get("/api/v1/backtests/queue/config").json()
        assert d["max_workers"] == 2
        assert d["source"] == "db"
        assert d["env_override"] == 5
    finally:
        app.state.astock.jobs.shutdown(wait=False)


def test_experiment_presets_expose_concurrency(api_cfg: AStockConfig, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.delenv("ASTOCK_BT_MAX_WORKERS", raising=False)
    app, client = _make_client(api_cfg)
    try:
        r = client.get("/api/v1/experiments/presets")
        assert r.status_code == 200
        d = r.json()
        assert d["default_concurrency"] == 6
        assert d["hard_max_concurrency"] == 8

        client.put("/api/v1/backtests/queue/config", json={"max_workers": 4})
        d = client.get("/api/v1/experiments/presets").json()
        assert d["default_concurrency"] == 4
        assert d["hard_max_concurrency"] == 8
    finally:
        app.state.astock.jobs.shutdown(wait=False)


def test_clamp_experiment_concurrency():
    from wtpy.apps.astock.service.experiments import clamp_concurrency

    assert clamp_concurrency(None) == 1
    assert clamp_concurrency(0) == 1
    assert clamp_concurrency(9) == 8
    assert clamp_concurrency("3") == 3
    assert clamp_concurrency("bad") == 1
    assert clamp_concurrency(2.5) == 1
    assert clamp_concurrency(True) == 1

