# -*- coding: utf-8 -*-
"""Stage D SQLite dual-write + Stage E experiment grid/runner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service import db as exp_db
from wtpy.apps.astock.service.experiments import (
    expand_param_grid,
    estimate_grid_size,
    create_experiment_from_grid,
    GUA_PRESETS,
    WEEKDAY_TEMPLATES,
)
from wtpy.apps.astock.service.runs import append_run_index, list_runs, delete_run


@pytest.fixture()
def cfg(tmp_path: Path) -> AStockConfig:
    c = AStockConfig()
    c.output_root = tmp_path / "outputs"
    c.storage_root = tmp_path / "storage"
    c.output_root.mkdir(parents=True, exist_ok=True)
    c.storage_root.mkdir(parents=True, exist_ok=True)
    return c


def test_init_db_and_dual_write_append(cfg: AStockConfig):
    exp_db.init_db(cfg)
    assert exp_db.db_path(cfg).exists()
    rid = "bt_dual_1"
    (Path(cfg.output_root) / rid).mkdir(parents=True)
    (Path(cfg.output_root) / rid / "metrics.json").write_text(
        json.dumps({"total_return": 0.12, "win_rate": 0.5, "n_round_trips": 3}),
        encoding="utf-8",
    )
    append_run_index(
        cfg,
        {
            "run_id": rid,
            "title": "demo + 卦象·最佳3爻",
            "status": "ok",
            "indicator_ids": ["txt_x"],
            "indicator_names": ["demo"],
            "period": "DAY",
            "hold": 1,
            "entry_lag": 1,
            "buy_weekday": 1,
            "exit_weekday": 4,
            "buy_on": "open",
            "sell_on": "open",
            "signal_weekdays": [5],
            "account_mode": "portfolio",
            "with_bagua": True,
            "gua_filter": {
                "enabled": True,
                "selection_mode": "exact_line",
                "selected_state_ids": ["24-1", "46-1", "11-1"],
            },
            "metrics": {"total_return": 0.12, "win_rate": 0.5, "n_round_trips": 3},
            "start": 20240101,
            "end": 20240601,
        },
    )
    assert exp_db.count_runs_db(cfg) == 1
    rows = exp_db.list_runs_db(cfg, limit=10)
    assert rows[0]["run_id"] == rid
    assert rows[0]["title"].startswith("demo")
    assert rows[0]["gua_filter"]["enabled"] is True
    assert rows[0]["metrics"]["total_return"] == 0.12
    # list_runs prefers sqlite
    listed = list_runs(cfg, limit=10)
    assert any(r.get("run_id") == rid for r in listed)


def test_migrate_runs_index_json(cfg: AStockConfig):
    idx = Path(cfg.output_root) / "runs_index.json"
    idx.write_text(
        json.dumps(
            [
                {
                    "run_id": "bt_mig_a",
                    "title": "A",
                    "status": "ok",
                    "created_at": 100,
                    "period": "DAY",
                    "metrics": {"total_return": 0.01},
                },
                {
                    "run_id": "bt_mig_b",
                    "title": "B",
                    "status": "ok",
                    "created_at": 200,
                    "period": "WEEK",
                    "metrics": {"total_return": 0.02},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = exp_db.migrate_runs_index_to_sqlite(cfg)
    assert report["ok"] is True
    assert report["imported"] == 2
    assert exp_db.count_runs_db(cfg) == 2
    # idempotent
    report2 = exp_db.migrate_runs_index_to_sqlite(cfg)
    assert report2["imported"] == 2
    assert exp_db.count_runs_db(cfg) == 2


def test_delete_run_clears_db(cfg: AStockConfig):
    rid = "bt_del_1"
    (Path(cfg.output_root) / rid).mkdir(parents=True)
    append_run_index(cfg, {"run_id": rid, "title": "x", "status": "ok", "metrics": {}})
    assert exp_db.count_runs_db(cfg) == 1
    delete_run(cfg, rid, remove_files=True)
    assert exp_db.count_runs_db(cfg) == 0


def test_estimate_and_expand_grid():
    n = estimate_grid_size(
        ["r1", "r2"],
        ["none", "best3", "bull"],
        ["fri_signal_mon_buy_thu_exit", "all_signal_tn12"],
        [None, 0.03],
    )
    # 2 * 3 * 2 * 2 = 24
    assert n == 24
    variants = expand_param_grid(
        rule_ids=["r1"],
        gua_keys=["none", "best3"],
        weekday_keys=["all_signal_tn12"],
        stop_loss_list=[None],
        period="DAY",
        codes=["sh600000"],
        start=20240101,
        end=20240201,
    )
    assert len(variants) == 2
    assert variants[0]["rule_ids"] == ["r1"]
    assert variants[1]["gua_filter"]["enabled"] is True
    assert set(variants[1]["gua_filter"]["selected_state_ids"]) == {"24-1", "46-1", "11-1"}
    assert "best3" in GUA_PRESETS and "bull" in GUA_PRESETS
    assert "fri_signal_mon_buy_thu_exit" in WEEKDAY_TEMPLATES


def test_create_experiment_soft_cap(cfg: AStockConfig):
    with pytest.raises(ValueError, match="超过上限"):
        create_experiment_from_grid(
            cfg,
            name="too-big",
            rule_ids=["a", "b", "c"],
            gua_keys=["none", "best3", "bull"],
            weekday_keys=list(WEEKDAY_TEMPLATES.keys()),
            stop_loss_list=[None, 0.02, 0.03],
            max_variants=5,
            force=False,
        )
    exp = create_experiment_from_grid(
        cfg,
        name="ok",
        rule_ids=["a"],
        gua_keys=["none", "best3"],
        weekday_keys=["all_signal_tn12"],
        stop_loss_list=[None],
        max_variants=50,
        codes=["sh600000"],
        start=20240101,
        end=20240131,
    )
    assert exp["experiment_id"].startswith("exp_")
    assert exp["estimated_variants"] == 2
    assert len(exp["variants"]) == 2
    assert exp["variants"][0]["status"] == "pending"
    # results table empty metrics ok
    table = exp_db.experiment_results_table(cfg, exp["experiment_id"])
    assert table["n"] == 2


def test_experiment_findings_batch_matches_results_table(cfg: AStockConfig):
    """Batch dashboard loader returns exactly the experiment_results_table rows."""
    exp = create_experiment_from_grid(
        cfg,
        name="batch-eq",
        rule_ids=["a"],
        gua_keys=["none", "best3"],
        weekday_keys=["all_signal_tn12"],
        stop_loss_list=[None],
        max_variants=50,
        codes=["sh600000"],
        start=20240101,
        end=20240131,
    )
    exp_id = exp["experiment_id"]
    variants = exp["variants"]
    assert len(variants) == 2
    for i, v in enumerate(variants):
        rid = f"bt_batch_{i}"
        append_run_index(
            cfg,
            {
                "run_id": rid,
                "title": f"batch run {i}",
                "status": "ok",
                "param_hash": v["param_hash"],
                "metrics": {
                    "total_return": 0.1 + i,
                    "win_rate": 0.55 + i * 0.1,
                    "max_drawdown": -0.05 - i * 0.01,
                    "n_round_trips": 3 + i,
                },
            },
        )
        exp_db.update_variant(cfg, v["variant_id"], run_id=rid, status="ok")
    single = exp_db.experiment_results_table(cfg, exp_id)
    batch = exp_db.experiment_findings_batch(cfg, [exp_id])
    assert exp_id in batch
    assert batch[exp_id] == single
    # missing ids are simply absent, never an error
    assert exp_db.experiment_findings_batch(cfg, ["exp_missing_1"]) == {}


def test_param_hash_dedup_lookup(cfg: AStockConfig):
    params = {"rule_ids": ["x"], "period": "DAY", "hold": 1}
    ph = exp_db.param_hash(params)
    append_run_index(
        cfg,
        {
            "run_id": "bt_hash_1",
            "title": "h",
            "status": "ok",
            "param_hash": ph,
            "metrics": {"total_return": 0.1},
        },
    )
    # ensure hash stored
    found = exp_db.find_run_id_by_param_hash(cfg, ph)
    assert found == "bt_hash_1"


def test_ui_has_experiment_panel():
    html = (
        Path(__file__).resolve().parents[3]
        / "wtpy"
        / "apps"
        / "astock"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="pageExperiment"' in html
    assert 'data-bt-page="experiment"' in html
    assert "/api/v1/experiments" in html
    assert "btnExpCreate" in html
    assert "实验中心" in html
    assert "exp-hero" in html
    assert "将运行" in html or "expComboCount" in html
    # no longer buried only under history as chaotic MVP strip
    assert "用同一条选股规则" in html or "同一条选股规则" in html
