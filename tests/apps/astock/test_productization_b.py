# -*- coding: utf-8 -*-
"""Productization B: continuous scheduler beat + cross-section + API."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.research.platform import ResearchPlatform
from wtpy.apps.astock.research.schedule_runner import (
    FIRE_MAX_TRIALS_CAP,
    ScheduleBeatStore,
    due_schedules,
    fire_schedule,
)
from wtpy.apps.astock.research.cross_section import (
    board_of_symbol,
    cross_section_summary,
    slice_metrics_by_board,
)
from wtpy.apps.astock.research.evaluation import evaluate_trials
from wtpy.apps.astock.research.data_update_trigger import monitor_drift_and_alert


def test_board_of_symbol_boards():
    assert board_of_symbol("SSE.600000") == "main"
    assert board_of_symbol("SZSE.300750") == "chinext"
    assert board_of_symbol("SSE.688981") == "star"


def test_cross_section_summary_and_slices():
    symbol_metrics = [
        {"std_code": "SSE.600000", "total_return": 0.10},
        {"std_code": "SZSE.000001", "total_return": -0.05},
        {"std_code": "SZSE.300750", "total_return": 0.20},
        {"std_code": "SSE.688981", "total_return": 0.05},
        {"std_code": "SSE.600519", "total_return": 0.30},
        {"std_code": "SZSE.002594", "total_return": 0.02},
    ]
    summary = cross_section_summary(symbol_metrics)
    assert summary["n"] == 6
    assert 0.0 < summary["pct_profitable"] <= 1.0
    assert "median_return" in summary
    assert "board_slices" in summary
    assert "concentration_top5" in summary
    slices = slice_metrics_by_board(symbol_metrics)
    assert "main" in slices
    assert "chinext" in slices
    assert "star" in slices
    assert slices["chinext"]["n"] == 1
    assert slices["star"]["n"] == 1


def test_evaluate_trials_attaches_cross_section():
    trials = [
        {
            "id": "t1",
            "total_return": 0.12,
            "max_drawdown": 0.1,
            "win_rate": 0.5,
            "n_trades": 20,
            "symbol_metrics": [
                {"std_code": "SSE.600000", "total_return": 0.1},
                {"std_code": "SZSE.300001", "total_return": -0.02},
                {"std_code": "SSE.688001", "total_return": 0.08},
            ],
        }
    ]
    out = evaluate_trials(trials)
    assert "cross_section" in out
    cs = out["cross_section"]
    assert cs.get("n") == 3
    assert "board_slices" in cs
    assert "pct_profitable" in cs


def test_due_and_fire_dry_run(tmp_path: Path):
    store = ScheduleBeatStore(tmp_path / "beat.json")
    # Monday 2024-01-01 is a holiday-agnostic weekday; daily fires hour_utc=1
    # Use a weekday at 12:00 UTC so daily/nightly slots may be due depending config
    now = datetime(2024, 1, 3, 12, 0, 0)  # Wednesday
    due = due_schedules(now, store)
    assert "daily" in due  # daily Mon-Fri hour 1 already passed

    plat = ResearchPlatform(tmp_path / "plat", use_memory_queue=True)
    try:
        dry = fire_schedule("daily", plat, dry_run=True, store=store, now=now)
        assert dry["ok"] is True
        assert dry["dry_run"] is True
        assert dry["enqueued"] == 0
        assert dry["would_enqueue"] == min(50, FIRE_MAX_TRIALS_CAP)
        assert dry["budget"] == FIRE_MAX_TRIALS_CAP

        # store unchanged on dry_run
        assert store.get_last_fire("daily") is None

        real = fire_schedule("daily", plat, dry_run=False, store=store, now=now, n=3)
        assert real["ok"] is True
        assert real["enqueued"] == 3
        assert store.get_last_fire("daily") is not None

        # after fire, not due again same day
        due2 = due_schedules(now, store)
        assert "daily" not in due2
    finally:
        plat.close()


def test_monitor_drift_and_alert_shape():
    out = monitor_drift_and_alert(
        {"total_return": 0.0, "max_drawdown": 0.4, "win_rate": 0.3},
        {"total_return": 0.2, "max_drawdown": 0.1, "win_rate": 0.5},
    )
    assert out["ok"] is True
    assert out["drift"] is True
    assert "alert" in out
    assert out["alert"]["alert"] is True
    assert out["alert"]["level"] in ("low", "medium", "high")


def test_api_productization_b_endpoints(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.output_root = tmp_path / "out"
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)

    r_due = client.get("/api/v1/research/schedules/due", params={"now": "2024-01-03T12:00:00"})
    assert r_due.status_code == 200, r_due.text
    b_due = r_due.json()
    assert b_due.get("ok") is True
    assert isinstance(b_due.get("due"), list)

    r_fire = client.post(
        "/api/v1/research/schedules/daily/fire",
        json={"dry_run": True},
    )
    assert r_fire.status_code == 200, r_fire.text
    b_fire = r_fire.json()
    assert b_fire.get("ok") is True
    assert b_fire.get("dry_run") is True
    assert b_fire.get("would_enqueue", 0) > 0 or b_fire.get("budget", 0) > 0

    r_mon = client.post(
        "/api/v1/research/drift/monitor",
        json={
            "recent": {"total_return": 0.0, "max_drawdown": 0.4, "win_rate": 0.3},
            "baseline": {"total_return": 0.2, "max_drawdown": 0.1, "win_rate": 0.5},
        },
    )
    assert r_mon.status_code == 200, r_mon.text
    b_mon = r_mon.json()
    assert b_mon.get("ok") is True
    assert "alert" in b_mon

    r_cs = client.post(
        "/api/v1/research/cross_section",
        json={
            "symbol_metrics": [
                {"std_code": "SSE.600000", "total_return": 0.1},
                {"std_code": "SZSE.300750", "total_return": 0.2},
            ]
        },
    )
    assert r_cs.status_code == 200, r_cs.text
    b_cs = r_cs.json()
    assert b_cs.get("ok") is True
    assert b_cs.get("n") == 2
    assert "board_slices" in b_cs
    assert "concentration_top5" in b_cs
