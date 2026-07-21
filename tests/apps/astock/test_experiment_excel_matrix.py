# -*- coding: utf-8 -*-
"""Unit tests for experiment Excel matrix helpers (Task A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wtpy.apps.astock.research.matrix import build_result_matrix
from wtpy.apps.astock.service.experiments import (
    build_experiment_matrix_rows,
    write_experiment_excel,
)


def _fake_row(
    *,
    variant_id: str,
    exit_weekday: int,
    sell_on: str,
    gua_key: str,
    total_return: float,
    max_drawdown: float = 0.1,
    status: str = "succeeded",
):
    return {
        "variant_id": variant_id,
        "status": status,
        "run_id": "run_%s" % variant_id,
        "param_hash": "h_%s" % variant_id,
        "params": {
            "exit_weekday": exit_weekday,
            "sell_on": sell_on,
            "_meta": {
                "gua_key": gua_key,
                "gua_label": gua_key,
                "rule_id": "r1",
            },
        },
        "metrics": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "win_rate": 0.5,
            "n_round_trips": 20,
        },
        "error": None,
    }


def test_build_experiment_matrix_rows_extracts_axes():
    rows = [
        _fake_row(
            variant_id="a",
            exit_weekday=4,
            sell_on="open",
            gua_key="none",
            total_return=0.12,
        ),
        _fake_row(
            variant_id="b",
            exit_weekday=4,
            sell_on="open",
            gua_key="best3",
            total_return=0.18,
        ),
        # missing axes → skipped
        {
            "variant_id": "skip",
            "status": "succeeded",
            "params": {"_meta": {"gua_key": "none"}},
            "metrics": {"total_return": 0.01},
        },
    ]
    flat = build_experiment_matrix_rows(rows)
    assert len(flat) == 2
    assert flat[0]["exit_weekday"] == 4
    assert flat[0]["sell_on"] == "open"
    assert flat[0]["gua_key"] == "none"
    assert flat[0]["total_return"] == 0.12
    assert flat[1]["gua_key"] == "best3"
    assert flat[1]["total_return"] == 0.18


def test_build_experiment_matrix_rows_feeds_result_matrix():
    rows = [
        _fake_row(variant_id="n4o", exit_weekday=4, sell_on="open", gua_key="none", total_return=0.10),
        _fake_row(variant_id="b4o", exit_weekday=4, sell_on="open", gua_key="best3", total_return=0.20),
        _fake_row(variant_id="n5c", exit_weekday=5, sell_on="close", gua_key="none", total_return=0.05),
        _fake_row(variant_id="b5c", exit_weekday=5, sell_on="close", gua_key="best3", total_return=0.15),
    ]
    flat = build_experiment_matrix_rows(rows)
    matrix = build_result_matrix(flat, metric_key="total_return")
    assert "none" in matrix["columns"]
    assert "best3" in matrix["columns"]
    table = matrix["table"]
    assert len(table) == 2
    # find open/4
    by_key = {(r["exit_weekday"], r["sell_on"]): r for r in table}
    assert by_key[(4, "open")]["none"] == 0.10
    assert by_key[(4, "open")]["best3"] == 0.20
    assert by_key[(5, "close")]["none"] == 0.05
    assert by_key[(5, "close")]["best3"] == 0.15


def test_write_experiment_excel_matrix_sheets(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")

    fake_table = {
        "rows": [
            _fake_row(variant_id="n4o", exit_weekday=4, sell_on="open", gua_key="none", total_return=0.10, max_drawdown=0.08),
            _fake_row(variant_id="b4o", exit_weekday=4, sell_on="open", gua_key="best3", total_return=0.20, max_drawdown=0.12),
            _fake_row(variant_id="n5c", exit_weekday=5, sell_on="close", gua_key="none", total_return=0.05, max_drawdown=0.09),
            _fake_row(variant_id="b5c", exit_weekday=5, sell_on="close", gua_key="best3", total_return=0.15, max_drawdown=0.11),
        ]
    }

    class _Cfg:
        output_root = str(tmp_path)

    import wtpy.apps.astock.service.experiments as exp_mod

    monkeypatch.setattr(
        exp_mod.exp_db,
        "experiment_results_table",
        lambda cfg, experiment_id: fake_table,
    )

    out = write_experiment_excel(_Cfg(), "exp_test", path=tmp_path / "summary.xlsx")
    assert Path(out).is_file()
    wb = openpyxl.load_workbook(out)
    names = wb.sheetnames
    assert "实验结果" in names
    assert "matrix" in names
    assert "matrix_max_drawdown" in names

    ws = wb["matrix"]
    headers = [c.value for c in ws[1]]
    assert headers[0] == "exit_weekday"
    assert headers[1] == "sell_on"
    assert "none" in headers
    assert "best3" in headers

    # evaluate optional (may be empty if hard_filter rejects all; still ok if present)
    if "evaluate" in names:
        ev = wb["evaluate"]
        assert ev["A1"].value == "rank"
        assert ev["B1"].value == "id"
        assert ev["D1"].value == "total_return"
        assert ev["E1"].value == "max_drawdown"
