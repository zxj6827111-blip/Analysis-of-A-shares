# -*- coding: utf-8 -*-
"""Stage B: multi-run compare + equity curve loaders (shipped service path)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.runs import (
    compare_runs,
    load_equity_curve,
    load_run_summary,
)


def _write_run(
    root: Path,
    run_id: str,
    *,
    metrics: dict,
    repro: dict,
    equity_rows: list | None = None,
    title: str | None = None,
) -> None:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "status": "ok",
        "title": title or run_id,
        "metrics": metrics,
        "repro": repro,
        "indicator_ids": repro.get("indicator_ids"),
        "period": repro.get("period"),
        "hold": repro.get("hold"),
        "entry_lag": repro.get("entry_lag"),
        "buy_weekday": repro.get("buy_weekday"),
        "exit_weekday": repro.get("exit_weekday"),
        "buy_on": repro.get("buy_on"),
        "sell_on": repro.get("sell_on"),
        "signal_weekdays": repro.get("signal_weekdays"),
        "schedule_mode": repro.get("schedule_mode"),
        "account_mode": repro.get("account_mode"),
        "gua_filter": repro.get("gua_filter"),
        "with_bagua": repro.get("with_bagua"),
    }
    (d / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if equity_rows is not None:
        with open(d / "equity.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(
                f, fieldnames=["date", "cash", "market_value", "equity"]
            )
            w.writeheader()
            for row in equity_rows:
                w.writerow(row)


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "outputs"
    c.output_root.mkdir(parents=True, exist_ok=True)
    return c


def test_load_equity_curve_from_csv(cfg: AStockConfig):
    _write_run(
        Path(cfg.output_root),
        "bt_eq_1",
        metrics={"total_return": 0.1},
        repro={"period": "DAY", "entry_lag": 1, "hold": 1},
        equity_rows=[
            {"date": 20240102, "cash": 100, "market_value": 0, "equity": 100},
            {"date": 20240103, "cash": 50, "market_value": 60, "equity": 110},
            {"date": 20240104, "cash": 40, "market_value": 50, "equity": 90},
        ],
    )
    pts = load_equity_curve(cfg, "bt_eq_1")
    assert len(pts) == 3
    assert pts[0]["date"] == 20240102
    assert pts[1]["equity"] == 110.0
    assert pts[2]["equity"] == 90.0


def test_compare_runs_param_diff_and_metrics(cfg: AStockConfig):
    root = Path(cfg.output_root)
    _write_run(
        root,
        "bt_a",
        title="策略A · 周五信号 · 周一买",
        metrics={
            "total_return": 0.12,
            "annual_return": 0.2,
            "max_drawdown": -0.05,
            "win_rate": 0.55,
            "payoff_ratio": 1.4,
            "n_round_trips": 10,
            "account_mode": "portfolio",
        },
        repro={
            "indicator_ids": ["735"],
            "period": "DAY",
            "account_mode": "portfolio",
            "schedule_mode": "weekday",
            "signal_weekdays": [5],
            "buy_weekday": 1,
            "exit_weekday": 4,
            "buy_on": "open",
            "sell_on": "open",
            "entry_lag": 1,
            "hold": 1,
            "start": 20200101,
            "end": 20201231,
            "gua_filter": {"enabled": False},
            "with_bagua": False,
        },
    )
    _write_run(
        root,
        "bt_b",
        title="策略A · 周五信号 · 周一买 + 最佳3爻",
        metrics={
            "total_return": 0.18,
            "annual_return": 0.28,
            "max_drawdown": -0.07,
            "win_rate": 0.6,
            "payoff_ratio": 1.6,
            "n_round_trips": 6,
            "account_mode": "portfolio",
        },
        repro={
            "indicator_ids": ["735"],
            "period": "DAY",
            "account_mode": "portfolio",
            "schedule_mode": "weekday",
            "signal_weekdays": [5],
            "buy_weekday": 1,
            "exit_weekday": 4,
            "buy_on": "open",
            "sell_on": "open",
            "entry_lag": 1,
            "hold": 1,
            "start": 20200101,
            "end": 20201231,
            "gua_filter": {
                "enabled": True,
                "selection_mode": "exact_line",
                "history_summary": {"short": "卦象3项"},
                "natural_language": "最佳3爻",
            },
            "with_bagua": True,
        },
    )

    # sanity: load_run_summary (real entry) sees weekday + gua
    sa = load_run_summary(cfg, "bt_a")
    sb = load_run_summary(cfg, "bt_b")
    assert sa["buy_weekday"] == 1
    assert sb["gua_filter"]["enabled"] is True

    out = compare_runs(cfg, ["bt_a", "bt_b"])
    assert out["n_runs"] == 2
    assert out["run_ids"] == ["bt_a", "bt_b"]
    assert len(out["runs"]) == 2

    # metrics table uses values from metrics.json via load_run_summary
    by_key = {row["key"]: row["values"] for row in out["metrics_table"]}
    assert by_key["total_return"] == [0.12, 0.18]
    assert by_key["n_round_trips"] == [10, 6]

    diff_keys = {d["key"] for d in out["param_diffs"]}
    # gua should differ; weekdays same so not required in diffs
    assert "gua_short" in diff_keys
    # identical weekday fields should not appear as diffs
    assert "buy_weekday" not in diff_keys
    assert "signal_weekdays" not in diff_keys

    # params snapshot includes weekday + session
    p0 = out["runs"][0]["params"]
    assert p0["buy_weekday"] == 1
    assert p0["exit_weekday"] == 4
    assert p0["buy_on"] == "open"
    assert p0["schedule_mode"] == "weekday"
    p1 = out["runs"][1]["params"]
    assert "卦象" in p1["gua_short"] or p1["gua_short"] != p0["gua_short"]


def test_compare_runs_rejects_count(cfg: AStockConfig):
    root = Path(cfg.output_root)
    for i in range(3):
        _write_run(
            root,
            f"bt_c{i}",
            metrics={"total_return": 0.01 * i},
            repro={"period": "DAY", "entry_lag": 1, "hold": 1, "schedule_mode": "tn"},
        )
    with pytest.raises(ValueError, match="at least 2"):
        compare_runs(cfg, ["bt_c0"])
    ids = [f"bt_x{i}" for i in range(11)]
    for rid in ids:
        _write_run(
            root,
            rid,
            metrics={"total_return": 0.01},
            repro={"period": "DAY", "schedule_mode": "tn"},
        )
    with pytest.raises(ValueError, match="at most 10"):
        compare_runs(cfg, ids)


def test_compare_tn_vs_weekday_entry_lag_in_diff(cfg: AStockConfig):
    root = Path(cfg.output_root)
    _write_run(
        root,
        "bt_tn",
        metrics={"total_return": 0.05},
        repro={
            "period": "DAY",
            "schedule_mode": "tn",
            "entry_lag": 1,
            "hold": 1,
            "buy_on": "open",
            "sell_on": "close",
            "buy_weekday": None,
            "exit_weekday": None,
            "signal_weekdays": [],
            "indicator_ids": ["735"],
            "account_mode": "portfolio",
        },
    )
    _write_run(
        root,
        "bt_wd",
        metrics={"total_return": 0.06},
        repro={
            "period": "DAY",
            "schedule_mode": "weekday",
            "entry_lag": 1,
            "hold": 1,
            "buy_on": "open",
            "sell_on": "open",
            "buy_weekday": 1,
            "exit_weekday": 4,
            "signal_weekdays": [5],
            "indicator_ids": ["735"],
            "account_mode": "portfolio",
        },
    )
    out = compare_runs(cfg, ["bt_tn", "bt_wd"])
    keys = {d["key"] for d in out["param_diffs"]}
    assert "schedule_mode" in keys
    assert "buy_weekday" in keys
    assert "exit_weekday" in keys
    assert "signal_weekdays" in keys
    assert "sell_on" in keys
