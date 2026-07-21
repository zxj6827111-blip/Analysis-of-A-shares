# -*- coding: utf-8 -*-
"""Phase 5 gate: research evaluation center (pure helpers + thin API)."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wtpy.apps.astock.api import create_app
from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.research.evaluation import evaluate_trials
from wtpy.apps.astock.research.gua_gain import pair_gua_gain
from wtpy.apps.astock.research.heatmap import build_heatmap
from wtpy.apps.astock.research.scoring import (
    composite_score,
    hard_filter,
    neighborhood_stability,
    pareto_front,
    rank_candidates,
)
from wtpy.apps.astock.research.validation import score_in_out, walk_forward_folds


def test_walk_forward_folds_multiple():
    folds = walk_forward_folds(2010, 2020, train_years=3, test_years=1, step_years=1)
    assert len(folds) >= 2
    for f in folds:
        assert f["train_end"] >= f["train_start"]
        assert f["test_start"] == f["train_end"] + 1
        assert f["test_end"] >= f["test_start"]
        assert f["test_end"] <= 2020


def test_score_in_out_prefers_oos_when_configured():
    in_m = {"total_return": 1.0, "sharpe": 2.0, "win_rate": 0.7, "max_drawdown": 0.1, "n_trades": 50}
    out_strong = {
        "total_return": 0.8,
        "sharpe": 1.5,
        "win_rate": 0.6,
        "max_drawdown": 0.12,
        "n_trades": 40,
        "out_windows_profit_frac": 0.7,
    }
    out_weak = {
        "total_return": -0.2,
        "sharpe": -0.5,
        "win_rate": 0.3,
        "max_drawdown": 0.4,
        "n_trades": 40,
        "out_windows_profit_frac": 0.2,
    }
    s_strong = score_in_out(in_m, out_strong, prefer_oos=True, oos_weight=0.8)
    s_weak = score_in_out(in_m, out_weak, prefer_oos=True, oos_weight=0.8)
    assert s_strong["out_score"] > s_weak["out_score"]
    assert s_strong["combined_score"] > s_weak["combined_score"]
    # same in-sample; OOS drives ranking preference
    assert s_strong["in_score"] == pytest.approx(s_weak["in_score"])


def test_rank_candidates_composite_differs_from_total_return():
    # A: high return but terrible DD + overfit
    # B: moderate return, excellent risk metrics + OOS
    candidates = [
        {
            "id": "A",
            "total_return": 2.0,
            "max_drawdown": 0.6,
            "sharpe_like": 0.1,
            "win_rate": 0.4,
            "out_score": 0.1,
            "decay": 1.5,
            "stability": 0.1,
        },
        {
            "id": "B",
            "total_return": 0.5,
            "max_drawdown": 0.08,
            "sharpe_like": 1.8,
            "win_rate": 0.62,
            "out_score": 0.55,
            "decay": 0.05,
            "stability": 0.9,
        },
        {
            "id": "C",
            "total_return": 1.2,
            "max_drawdown": 0.25,
            "sharpe_like": 0.9,
            "win_rate": 0.5,
            "out_score": 0.3,
            "decay": 0.4,
            "stability": 0.4,
        },
    ]
    by_ret = sorted(candidates, key=lambda x: -x["total_return"])
    ranked = rank_candidates(candidates, mode="composite")
    assert [c["id"] for c in by_ret] == ["A", "C", "B"]
    ranked_ids = [c["id"] for c in ranked]
    # composite must not equal pure total_return order
    assert ranked_ids != [c["id"] for c in by_ret]
    # B should beat A under composite (risk / OOS)
    assert ranked_ids.index("B") < ranked_ids.index("A")
    assert composite_score(candidates[1]) > composite_score(candidates[0])


def test_pareto_front_multiple_nondominated():
    cands = [
        {"id": "r1", "total_return": 1.0, "max_drawdown": 0.3, "stability": 0.5},
        {"id": "r2", "total_return": 0.6, "max_drawdown": 0.1, "stability": 0.8},  # better DD/stability
        {"id": "r3", "total_return": 0.2, "max_drawdown": 0.5, "stability": 0.2},  # dominated
        {"id": "r4", "total_return": 0.9, "max_drawdown": 0.15, "stability": 0.6},
    ]
    front = pareto_front(cands)
    ids = {c["id"] for c in front}
    assert "r3" not in ids
    assert len(front) >= 2
    assert "r1" in ids or "r2" in ids


def test_hard_filter_rejects_few_trades_and_drawdown():
    ok, reasons = hard_filter(
        {"n_trades": 2, "max_drawdown": 0.1, "total_return": 0.5},
        {"min_trades": 10, "max_drawdown": 0.35},
    )
    assert ok is False
    assert any("too_few_trades" in r for r in reasons)

    ok2, reasons2 = hard_filter(
        {"n_trades": 50, "max_drawdown": 0.55, "total_return": 0.5},
        {"min_trades": 10, "max_drawdown": 0.35},
    )
    assert ok2 is False
    assert any("excessive_drawdown" in r for r in reasons2)

    ok3, _ = hard_filter(
        {"n_trades": 50, "max_drawdown": 0.1, "total_return": 0.5},
        {"min_trades": 10, "max_drawdown": 0.35},
    )
    assert ok3 is True


def test_gua_gain_pairing_deltas():
    rows = [
        {
            "exit_weekday": 5,
            "sell_on": "open",
            "hold": 1,
            "gua_key": "none",
            "total_return": 0.10,
            "max_drawdown": 0.20,
            "win_rate": 0.50,
            "trade_count": 40,
        },
        {
            "exit_weekday": 5,
            "sell_on": "open",
            "hold": 1,
            "gua_key": "best3",
            "total_return": 0.25,
            "max_drawdown": 0.15,
            "win_rate": 0.60,
            "trade_count": 30,
        },
        {
            "exit_weekday": 3,
            "sell_on": "close",
            "hold": 1,
            "gua_key": "none",
            "total_return": 0.20,
            "max_drawdown": 0.10,
            "win_rate": 0.55,
            "trade_count": 20,
        },
        {
            "exit_weekday": 3,
            "sell_on": "close",
            "hold": 1,
            "gua_key": "best3",
            "total_return": 0.12,
            "max_drawdown": 0.18,
            "win_rate": 0.48,
            "trade_count": 18,
        },
    ]
    gains = pair_gua_gain(rows)
    assert len(gains) == 2
    by_exit = {g["tech_key"][0]: g for g in gains}
    assert by_exit[5]["delta_return"] == pytest.approx(0.15)
    assert by_exit[5]["delta_drawdown"] == pytest.approx(-0.05)
    assert by_exit[5]["delta_win_rate"] == pytest.approx(0.10)
    assert by_exit[5]["delta_trade_count"] == pytest.approx(-10)
    assert by_exit[3]["delta_return"] < 0


def test_heatmap_shape():
    rows = [
        {"exit_weekday": 1, "sell_on": "open", "total_return": 0.1},
        {"exit_weekday": 1, "sell_on": "close", "total_return": 0.2},
        {"exit_weekday": 5, "sell_on": "open", "total_return": 0.3},
        {"exit_weekday": 5, "sell_on": "close", "total_return": 0.4},
    ]
    hm = build_heatmap(rows, "exit_weekday", "sell_on", "total_return")
    assert hm["shape"] == (2, 2)  # y=sell_on (2), x=exit_weekday (2)
    assert len(hm["matrix"]) == 2
    assert len(hm["matrix"][0]) == 2
    assert hm["x_labels"] == [1, 5]
    assert set(hm["y_labels"]) == {"open", "close"}


def test_neighborhood_stability_flags_isolated_spike():
    # plateau neighborhood around low returns, one isolated spike
    grid = [
        {"params": {"a": 1, "b": 1}, "total_return": 0.10},
        {"params": {"a": 1, "b": 2}, "total_return": 0.11},
        {"params": {"a": 2, "b": 1}, "total_return": 0.09},
        {"params": {"a": 2, "b": 2}, "total_return": 0.10},
        {"params": {"a": 5, "b": 5}, "total_return": 0.90},  # isolated high
    ]
    res = neighborhood_stability(grid, metric_key="total_return", spike_ratio=1.5)
    assert res["is_spike"] is True
    assert "isolated_spike" in res["flags"]


def test_evaluate_trials_end_to_end():
    trials = [
        {
            "id": "t1",
            "exit_weekday": 5,
            "sell_on": "open",
            "gua_key": "none",
            "total_return": 0.2,
            "max_drawdown": 0.15,
            "win_rate": 0.55,
            "n_trades": 30,
            "stability": 0.6,
            "sharpe_like": 1.0,
        },
        {
            "id": "t2",
            "exit_weekday": 5,
            "sell_on": "open",
            "gua_key": "best3",
            "total_return": 0.35,
            "max_drawdown": 0.12,
            "win_rate": 0.6,
            "n_trades": 25,
            "stability": 0.7,
            "sharpe_like": 1.2,
        },
        {
            "id": "t3",
            "exit_weekday": 3,
            "sell_on": "close",
            "gua_key": "none",
            "total_return": 0.05,
            "max_drawdown": 0.4,
            "win_rate": 0.45,
            "n_trades": 12,
            "stability": 0.3,
            "sharpe_like": 0.2,
        },
        {
            "id": "t4",
            "exit_weekday": 3,
            "sell_on": "close",
            "gua_key": "best3",
            "total_return": 0.08,
            "max_drawdown": 0.35,
            "win_rate": 0.48,
            "n_trades": 10,
            "stability": 0.35,
            "sharpe_like": 0.3,
        },
    ]
    out = evaluate_trials(trials)
    assert out["n_trials"] == 4
    assert isinstance(out["ranking"], list) and len(out["ranking"]) == 4
    assert isinstance(out["pareto"], list) and len(out["pareto"]) >= 1
    assert isinstance(out["gua_gains"], list) and len(out["gua_gains"]) >= 1
    assert "primary" in out["heatmaps"]
    assert out["heatmaps"]["primary"]["shape"][0] >= 1
    assert "flags" in out


def test_api_evaluate_returns_ranking(tmp_path: Path):
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "st"
    cfg.output_root = tmp_path / "out"
    cfg.ensure_dirs()
    app = create_app(cfg)
    client = TestClient(app)
    trials = [
        {
            "id": "a",
            "exit_weekday": 1,
            "sell_on": "open",
            "gua_key": "none",
            "total_return": 0.1,
            "max_drawdown": 0.1,
            "n_trades": 20,
            "win_rate": 0.5,
            "stability": 0.5,
        },
        {
            "id": "b",
            "exit_weekday": 1,
            "sell_on": "open",
            "gua_key": "best3",
            "total_return": 0.2,
            "max_drawdown": 0.08,
            "n_trades": 18,
            "win_rate": 0.55,
            "stability": 0.6,
        },
    ]
    r = client.post("/api/v1/research/evaluate", json={"trials": trials})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert isinstance(body.get("ranking"), list)
    assert len(body["ranking"]) == 2


def test_assign_regime_bull_bear_sideways():
    from wtpy.apps.astock.research.regimes import assign_regime, slice_metrics_by_regime

    dates = list(range(1, 61))
    equity = []
    for i in range(60):
        if i < 20:
            equity.append(1.0 + i * 0.02)
        elif i < 40:
            equity.append(equity[-1] * 0.97)
        else:
            equity.append(equity[-1] * 1.0)
    series = assign_regime(
        dates, equity, method="simple", window=5, bull_threshold=0.03, bear_threshold=-0.03
    )
    assert len(series) == 60
    labels = {x["regime"] for x in series}
    assert "bull" in labels
    assert "bear" in labels
    rows = [{"date": x["date"], "ret": x["rolling_return"]} for x in series]
    by = slice_metrics_by_regime(rows, series, metric_keys=["ret"])
    assert by
    assert any(k in by for k in ("bull", "bear", "sideways"))
    for _reg, agg in by.items():
        assert agg["count"] >= 1
        assert "ret_mean" in agg


def test_evaluate_trials_includes_regimes_and_yearly():
    from wtpy.apps.astock.research.evaluation import evaluate_trials

    dates = list(range(1, 31))
    equity = [1.0 + i * 0.01 for i in range(30)]
    trials = [
        {
            "id": "t_reg",
            "total_return": 0.2,
            "max_drawdown": 0.1,
            "win_rate": 0.55,
            "n_trades": 20,
            "sharpe": 1.0,
            "stability": 0.5,
            "dates": dates,
            "equity_curve": equity,
            "yearly_metrics": [
                {"year": 2020, "total_return": 0.1},
                {"year": 2021, "total_return": 0.05},
            ],
            "exit_weekday": 3,
            "sell_on": "open",
            "gua_key": "none",
        },
        {
            "id": "t2",
            "total_return": 0.15,
            "max_drawdown": 0.08,
            "win_rate": 0.5,
            "n_trades": 18,
            "sharpe": 0.9,
            "stability": 0.6,
            "exit_weekday": 4,
            "sell_on": "close",
            "gua_key": "best3",
        },
    ]
    out = evaluate_trials(trials)
    assert "regimes" in out
    assert "t_reg" in out["regimes"]
    assert "series" in out["regimes"]["t_reg"]
    assert "by_regime" in out["regimes"]["t_reg"]
    assert out["yearly"]["t_reg"]["2020"]["total_return"] == 0.1
    assert out["ranking"]

