# -*- coding: utf-8 -*-
"""Stage C: bagua metrics, signal preview, versioned import, combined AND/OR."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.bagua.filter_rules import (
    GuaFilter,
    compute_bagua_metrics,
    event_matches_gua_filter,
    filter_events_by_gua_filter,
)
from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service.gua import preview_filter, run_bagua_metrics_for_run


class _Ev:
    def __init__(self, bagua):
        self.bagua = bagua


def test_compute_bagua_metrics_before_after_and_sample_flag():
    events = [
        _Ev({"state_id": "01-1", "gua_order": 1, "main_hexagram_name": "乾", "action_signal": "新开仓"}),
        _Ev({"state_id": "01-2", "gua_order": 1, "main_hexagram_name": "乾", "action_signal": "持有"}),
        _Ev({"state_id": "02-1", "gua_order": 2, "main_hexagram_name": "坤", "action_signal": "新开仓"}),
        _Ev({"state_id": "02-2", "gua_order": 2, "main_hexagram_name": "坤", "action_signal": "加仓"}),
        _Ev({"state_id": "03-1", "gua_order": 3, "main_hexagram_name": "屯", "action_signal": "减仓"}),
    ]
    gf = GuaFilter(
        enabled=True,
        selection_mode="action_signal",
        selected_action_signals=["新开仓", "加仓"],
    )
    m = compute_bagua_metrics(events, gf, min_sample=30)
    assert m["n_signals_before"] == 5
    assert m["n_signals_after"] == 3
    assert abs(m["retention_rate"] - 0.6) < 1e-9
    assert m["filter_active"] is True
    assert m["sample_sufficient"] is False
    assert any("不稳健" in w or "偏少" in w or "≥" in w for w in m["warnings"])
    after_actions = {x["key"]: x["count"] for x in m["after"]["by_action_signal"]}
    assert after_actions.get("新开仓") == 2
    assert after_actions.get("加仓") == 1
    assert "持有" not in after_actions


def test_combined_and_or_matrix():
    """Within category OR, across categories AND (combined mode)."""
    events = [
        _Ev({"state_id": "01-2", "gua_order": 1, "action_signal": "新开仓"}),
        _Ev({"state_id": "01-1", "gua_order": 1, "action_signal": "持有"}),
        _Ev({"state_id": "02-2", "gua_order": 2, "action_signal": "新开仓"}),
        _Ev({"state_id": "01-4", "gua_order": 1, "action_signal": "加仓"}),
        _Ev({"state_id": "03-2", "gua_order": 3, "action_signal": "加仓"}),
    ]
    # main in {1} AND action in {新开仓, 加仓}
    gf = GuaFilter(
        enabled=True,
        selection_mode="combined",
        selected_main_hexagram_ids=[1],
        selected_action_signals=["新开仓", "加仓"],
    )
    out = filter_events_by_gua_filter(events, gf)
    sids = {e.bagua["state_id"] for e in out}
    assert sids == {"01-2", "01-4"}

    # exact line OR within states, AND with action
    gf2 = GuaFilter(
        enabled=True,
        selection_mode="combined",
        selected_state_ids=["01-2", "02-2"],
        selected_action_signals=["新开仓"],
    )
    out2 = filter_events_by_gua_filter(events, gf2)
    assert {e.bagua["state_id"] for e in out2} == {"01-2", "02-2"}

    # action-only (OR within actions)
    gf3 = GuaFilter(
        enabled=True,
        selection_mode="action_signal",
        selected_action_signals=["持有", "减仓"],
    )
    # no 减仓 in set -> only 持有
    out3 = filter_events_by_gua_filter(events, gf3)
    assert [e.bagua["state_id"] for e in out3] == ["01-1"]


def test_preview_filter_rule_table_and_optional_signal_flag():
    d = preview_filter(
        {
            "enabled": True,
            "selection_mode": "exact_line",
            "selected_state_ids": ["01-5", "02-5", "11-1"],
        }
    )
    assert d["matched_state_count"] == 3
    assert d["signal_preview"] is None
    assert "规则表" in (d.get("note") or "") or d.get("total_state_count") == 384

    # signal_preview without rule_ids should not crash
    d2 = preview_filter(
        {
            "enabled": True,
            "selection_mode": "action_signal",
            "selected_action_signals": ["新开仓"],
        },
        signal_preview=True,
        rule_ids=None,
    )
    assert d2["signal_preview"] is not None
    assert d2["signal_preview"].get("ok") is False


def test_run_bagua_metrics_for_run_from_signals_csv(tmp_path: Path):
    cfg = AStockConfig()
    cfg.output_root = tmp_path / "out"
    rid = "bt_gua_m1"
    d = cfg.output_root / rid
    d.mkdir(parents=True)
    meta = {
        "run_id": rid,
        "status": "ok",
        "n_signals_before_bagua": 4,
        "n_signals_after_bagua": 2,
        "gua_filter": {
            "enabled": True,
            "selection_mode": "action_signal",
            "selected_action_signals": ["新开仓"],
            "rule_version": "test_v1",
        },
    }
    (d / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    # write signals.csv with bagua_* columns (shipped writer schema)
    csv_text = (
        "std_code,date,period,indicator_id,value,is_dwm,"
        "bagua_full_name,bagua_yao_name,bagua_gua_order,bagua_judgement,"
        "bagua_core_gang,bagua_state_id,bagua_action_signal,bagua_biangua,bagua_rule_version\n"
        "SSE.STK.600000,20240102,DAY,735,,0,乾为天,初九,1,,,01-1,新开仓,,test_v1\n"
        "SSE.STK.600000,20240103,DAY,735,,0,乾为天,九二,1,,,01-2,持有,,test_v1\n"
        "SSE.STK.600000,20240104,DAY,735,,0,坤为地,初六,2,,,02-1,新开仓,,test_v1\n"
        "SSE.STK.600000,20240105,DAY,735,,0,坤为地,六二,2,,,02-2,加仓,,test_v1\n"
    )
    (d / "signals.csv").write_text(csv_text, encoding="utf-8-sig")

    m = run_bagua_metrics_for_run(cfg, rid)
    assert m["run_id"] == rid
    assert m["source"] == "signals.csv"
    assert m["n_signals_before"] == 4
    assert m["n_signals_after"] == 2  # only 新开仓
    assert m["filter_active"] is True
    acts = {x["key"]: x["count"] for x in (m.get("after") or {}).get("by_action_signal") or []}
    assert acts.get("新开仓") == 2


def test_run_bagua_metrics_meta_fallback(tmp_path: Path):
    cfg = AStockConfig()
    cfg.output_root = tmp_path / "out"
    rid = "bt_gua_meta"
    d = cfg.output_root / rid
    d.mkdir(parents=True)
    (d / "run_meta.json").write_text(
        json.dumps(
            {
                "run_id": rid,
                "n_signals_before_bagua": 100,
                "n_signals_after_bagua": 12,
                "gua_filter": {
                    "enabled": True,
                    "selection_mode": "exact_line",
                    "selected_state_ids": ["24-1", "46-1", "11-1"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    m = run_bagua_metrics_for_run(cfg, rid)
    assert m["source"] == "meta_only"
    assert m["n_signals_before"] == 100
    assert m["n_signals_after"] == 12
    assert m["sample_sufficient"] is False


def test_reimport_archives_previous(tmp_path: Path):
    from wtpy.apps.astock.service.gua import reimport_excel
    from wtpy.apps.astock.bagua.rebuild_from_excel import default_excel_path

    try:
        xlsx = default_excel_path()
    except FileNotFoundError:
        pytest.skip("excel not present")

    cfg = AStockConfig()
    active = tmp_path / "bagua_384.json"
    cfg.bagua_json = active
    # seed active with a dummy previous version
    active.write_text(
        json.dumps(
            {
                "rule_version": "old_ver_test",
                "entries": [],
                "count_yao": 0,
                "count_gua": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = reimport_excel(xlsx, cfg=cfg, archive_previous=True)
    assert active.exists()
    assert report.get("previous_rule_version") == "old_ver_test"
    assert report.get("archived_previous")
    arch = Path(report["archived_previous"])
    assert arch.exists()
    prev = json.loads(arch.read_text(encoding="utf-8"))
    assert prev.get("rule_version") == "old_ver_test"
    # active should be new full knowledge
    kb = json.loads(active.read_text(encoding="utf-8"))
    assert kb.get("count_yao") == 384
    assert kb.get("rule_version") != "old_ver_test"


def test_ui_stage_c_hooks():
    html = (
        Path(__file__).resolve().parents[3]
        / "wtpy"
        / "apps"
        / "astock"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert "signal_preview" in html
    assert "bagua-metrics" in html
    assert "guaAnalysisBlock" in html
    assert "真实信号抽样" in html or "真实信号" in html
    assert 'data-pane="gua"' in html or 'id="pane-gua"' in html
