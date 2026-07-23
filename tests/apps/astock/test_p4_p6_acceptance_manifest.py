# -*- coding: utf-8 -*-
"""P4–P6 acceptance manifest: boards + shipped surface imports (not full gate reimplementation)."""
from __future__ import annotations

from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]


def _board(glob_prefix: str) -> Path:
    docs = ROOT / "docs"
    matches = list(docs.glob(f"{glob_prefix}*.md"))
    assert matches, f"missing board {glob_prefix}"
    return matches[0]


def test_phase_boards_accepted():
    for prefix in ("Phase4", "Phase5", "Phase6"):
        path = _board(prefix)
        text = path.read_text(encoding="utf-8")
        assert "accepted" in text.lower(), f"{path.name} not accepted"
    p4 = _board("Phase4").read_text(encoding="utf-8")
    assert "P4.E" in p4 and "[x]" in p4
    # checklist row for acceptance must be checked
    assert "| P4.E |" in p4
    for line in p4.splitlines():
        if "| P4.E |" in line:
            assert "[x]" in line, line


def test_phase4_queue_surface_importable():
    from wtpy.apps.astock.research.platform import ResearchPlatform
    from wtpy.apps.astock.research.queue_backend import (
        MemoryQueueBackend,
        SqliteQueueBackend,
    )
    from wtpy.apps.astock.research.trial_store import TrialStore
    from wtpy.apps.astock.research.worker import ResearchWorker

    assert ResearchPlatform and MemoryQueueBackend and SqliteQueueBackend
    assert TrialStore and ResearchWorker


def test_phase5_evaluation_surface_regimes_yearly():
    from wtpy.apps.astock.research.evaluation import evaluate_trials
    from wtpy.apps.astock.research.scoring import rank_candidates

    dates = list(range(1, 21))
    equity = [1.0 + i * 0.01 for i in range(20)]
    out = evaluate_trials(
        [
            {
                "id": "t1",
                "total_return": 0.2,
                "max_drawdown": 0.1,
                "win_rate": 0.55,
                "n_trades": 20,
                "sharpe": 1.0,
                "stability": 0.5,
                "dates": dates,
                "equity_curve": equity,
                "yearly_metrics": [{"year": 2020, "total_return": 0.1}],
                "exit_weekday": 3,
                "sell_on": "open",
                "gua_key": "none",
            }
        ]
    )
    assert "regimes" in out and "t1" in out["regimes"]
    assert "yearly" in out and "t1" in out["yearly"]
    # composite ranking not total_return-only (smoke)
    ranked = rank_candidates(
        [
            {
                "id": "hi",
                "total_return": 1.0,
                "max_drawdown": 0.5,
                "win_rate": 0.4,
                "n_trades": 10,
                "sharpe": 0.2,
                "stability": 0.1,
                "out_of_sample_return": 0.05,
            },
            {
                "id": "bal",
                "total_return": 0.4,
                "max_drawdown": 0.08,
                "win_rate": 0.6,
                "n_trades": 40,
                "sharpe": 1.5,
                "stability": 0.8,
                "out_of_sample_return": 0.35,
            },
        ],
        mode="composite",
    )
    assert ranked[0]["id"] == "bal"


def test_phase6_search_schedule_drift_symbols():
    from wtpy.apps.astock.research.continuous import run_budgeted_search
    from wtpy.apps.astock.research.drift import detect_drift
    from wtpy.apps.astock.research.optimizer import grid_search, random_search
    from wtpy.apps.astock.research.schedules import list_schedules

    names = {s["name"] if isinstance(s, dict) else s for s in list_schedules()}
    # list_schedules may return dicts or names
    if names and isinstance(next(iter(names)), str) and "daily" not in str(names).lower():
        # try values
        raw = list_schedules()
        blob = str(raw).lower()
        assert "daily" in blob and "nightly" in blob and "weekend" in blob
    else:
        blob = str(list_schedules()).lower()
        assert "daily" in blob and "weekend" in blob

    g1 = grid_search({"a": [1, 2], "b": [3, 4]}, budget=3, seed=1)
    g2 = grid_search({"a": [1, 2], "b": [3, 4]}, budget=3, seed=1)
    assert g1 == g2
    r = random_search({"a": [1, 2, 3], "b": [4, 5]}, n=5, seed=7)
    assert len(r) <= 5
    d = detect_drift(
        {"total_return": 0.05, "max_drawdown": 0.3, "win_rate": 0.4},
        {"total_return": 0.25, "max_drawdown": 0.1, "win_rate": 0.55},
    )
    assert d.get("drift") is True or d.get("severity")
    trials = run_budgeted_search(
        {"x": [1, 2], "y": [3]},
        method="grid",
        budget=2,
        seed=1,
        evaluate_fn=lambda p: {"total_return": 0.1, "params": p},
    )
    assert trials
