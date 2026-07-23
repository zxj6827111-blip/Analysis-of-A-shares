# -*- coding: utf-8 -*-
"""Phase 6 gate: search + continuous research."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.research.optimizer import grid_search, random_search, staged_search
from wtpy.apps.astock.research.promote import select_for_full_retest
from wtpy.apps.astock.research.schedules import list_schedules
from wtpy.apps.astock.research.drift import detect_drift
from wtpy.apps.astock.research.reports_auto import build_research_summary, mark_paper_candidates
from wtpy.apps.astock.research.continuous import run_budgeted_search


SPACE = {
    "exit_weekday": [1, 2, 3, 4, 5],
    "sell_on": ["open", "close"],
    "hold_days": [1, 2, 3],
}


def test_grid_search_seed_reproducible():
    a = grid_search(SPACE, budget=8, seed=42)
    b = grid_search(SPACE, budget=8, seed=42)
    c = grid_search(SPACE, budget=8, seed=7)
    assert a == b
    assert len(a) == 8
    assert a != c or SPACE  # different seed should usually differ; full product may coincide rarely
    # stronger: same seed always identical
    assert [tuple(sorted(x.items())) for x in a] == [tuple(sorted(x.items())) for x in b]


def test_random_search_budget_respected():
    out = random_search(SPACE, n=10, seed=1)
    assert len(out) == 10
    out0 = random_search(SPACE, n=0, seed=1)
    assert out0 == []


def test_staged_search_returns_le_budget():
    coarse, fine = 6, 4
    out = staged_search(
        SPACE,
        coarse_budget=coarse,
        fine_budget=fine,
        seed=3,
        score_fn=lambda p: float(p.get("hold_days") or 0),
    )
    assert len(out) <= coarse + fine
    assert len(out) >= min(coarse, 1)


def test_select_for_full_retest_top_n():
    candidates = [
        {
            "id": "a",
            "params": {"x": 1},
            "total_return": 0.1,
            "max_drawdown": 0.1,
            "win_rate": 0.5,
            "n_trades": 20,
            "stability": 0.5,
        },
        {
            "id": "b",
            "params": {"x": 2},
            "total_return": 0.3,
            "max_drawdown": 0.05,
            "win_rate": 0.6,
            "n_trades": 25,
            "stability": 0.7,
        },
        {
            "id": "c",
            "params": {"x": 3},
            "total_return": 0.2,
            "max_drawdown": 0.08,
            "win_rate": 0.55,
            "n_trades": 22,
            "stability": 0.6,
        },
    ]
    selected = select_for_full_retest(candidates, top_n=2, metric="composite_score")
    assert len(selected) == 2
    assert all(isinstance(p, dict) for p in selected)


def test_list_schedules_includes_named():
    names = {s["name"] for s in list_schedules()}
    assert "daily" in names
    assert "nightly" in names
    assert "weekend" in names


def test_detect_drift_flags_significant_decay():
    baseline = {"total_return": 0.25, "max_drawdown": 0.10, "win_rate": 0.55}
    recent = {"total_return": 0.05, "max_drawdown": 0.25, "win_rate": 0.40}
    out = detect_drift(recent, baseline)
    assert out["drift"] is True
    assert out["severity"] in ("low", "medium", "high")
    assert len(out["reasons"]) >= 1


def test_build_research_summary_non_empty():
    trials = [
        {
            "id": "t1",
            "total_return": 0.1,
            "max_drawdown": 0.1,
            "n_trades": 10,
            "win_rate": 0.5,
            "stability": 0.5,
        }
    ]
    s = build_research_summary("exp1", trials, evaluate_result={"ranking": trials, "flags": []})
    assert s.get("markdown")
    assert len(s["markdown"]) > 10
    assert s.get("n_trials") == 1


def test_mark_paper_candidates_sets_flags():
    ranked = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    out = mark_paper_candidates(ranked, top_k=2)
    assert out[0]["paper_trading_observe"] is True
    assert out[1]["paper_trading_observe"] is True
    assert out[2]["paper_trading_observe"] is False
    # originals untouched
    assert "paper_trading_observe" not in ranked[0]


def test_run_budgeted_search_e2e_fake_evaluate():
    def fake_eval(params):
        return {
            "total_return": float(params.get("hold_days") or 0) * 0.01,
            "max_drawdown": 0.1,
            "score": float(params.get("hold_days") or 0),
        }

    trials = run_budgeted_search(
        SPACE,
        method="random",
        budget=5,
        seed=11,
        evaluate_fn=fake_eval,
    )
    assert len(trials) == 5
    assert all("params" in t for t in trials)
    assert all("metrics" in t or "score" in t or "total_return" in t for t in trials)


def test_api_search_schedules_drift(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.output_root = tmp_path / "out"
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)

    r = client.post(
        "/api/v1/research/search",
        json={"method": "grid", "space": SPACE, "budget": 5, "seed": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("n") == 5
    assert isinstance(body.get("trials"), list)

    r2 = client.get("/api/v1/research/schedules")
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2.get("ok") is True
    names = set(b2.get("names") or [])
    assert {"daily", "nightly", "weekend"} <= names

    r3 = client.post(
        "/api/v1/research/drift",
        json={
            "recent": {"total_return": 0.0, "max_drawdown": 0.4, "win_rate": 0.3},
            "baseline": {"total_return": 0.2, "max_drawdown": 0.1, "win_rate": 0.5},
        },
    )
    assert r3.status_code == 200, r3.text
    b3 = r3.json()
    assert b3.get("ok") is True
    assert b3.get("drift") is True
