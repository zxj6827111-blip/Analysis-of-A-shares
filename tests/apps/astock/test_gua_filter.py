# -*- coding: utf-8 -*-
"""Tests for gua filter: excel import, filter logic, API surface."""
from __future__ import annotations

import tests.apps.astock.conftest  # noqa: F401

from pathlib import Path

import pytest

from wtpy.apps.astock.bagua.filter_rules import (
    GuaFilter,
    event_matches_gua_filter,
    filter_events_by_gua_filter,
    gua_filter_natural_language,
)
from wtpy.apps.astock.bagua.rebuild_from_excel import (
    default_excel_path,
    rebuild_knowledge_from_excel,
    validate_knowledge,
)
from wtpy.apps.astock.service.gua import list_hexagrams, list_states, preview_filter


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


class _Ev:
    def __init__(self, bagua):
        self.bagua = bagua


def test_excel_import_64_6_384():
    try:
        xlsx = default_excel_path()
    except FileNotFoundError:
        pytest.skip("excel not present")
    out = Path(__file__).resolve().parents[3] / "wtpy" / "apps" / "astock" / "bagua" / "_test_bagua_tmp.json"
    try:
        kb = rebuild_knowledge_from_excel(xlsx, out)
        report = validate_knowledge(kb)
        assert report["ok"], report["issues"]
        assert report["count_entries"] == 384
        assert report["count_gua"] == 64
        assert report["unique_state_ids"] == 384
        assert report["missing_action_signal"] == 0
    finally:
        if out.exists():
            out.unlink()


def test_json_has_action_and_state_id():
    import json

    kb = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert kb["count_yao"] == 384
    e0 = kb["entries"][0]
    assert e0.get("state_id") == "01-1"
    assert e0.get("action_signal") in ("新开仓", "加仓", "持有", "减仓", "清仓")
    sids = [e["state_id"] for e in kb["entries"]]
    assert len(sids) == len(set(sids))


def test_filter_disabled_passes_all():
    events = [_Ev({"state_id": "01-5", "gua_order": 1, "action_signal": "持有"})]
    gf = GuaFilter(enabled=False, selection_mode="none")
    assert len(filter_events_by_gua_filter(events, gf)) == 1


def test_filter_main_hexagram():
    events = [
        _Ev({"state_id": "01-5", "gua_order": 1, "main_hexagram_id": 1, "action_signal": "持有"}),
        _Ev({"state_id": "02-5", "gua_order": 2, "main_hexagram_id": 2, "action_signal": "新开仓"}),
    ]
    gf = GuaFilter(
        enabled=True,
        selection_mode="main_hexagram",
        selected_main_hexagram_ids=[1],
    )
    out = filter_events_by_gua_filter(events, gf)
    assert len(out) == 1
    assert out[0].bagua["state_id"] == "01-5"


def test_filter_exact_line():
    events = [
        _Ev({"state_id": "01-5", "gua_order": 1, "yao_order": 5, "action_signal": "持有"}),
        _Ev({"state_id": "02-5", "gua_order": 2, "yao_order": 5, "action_signal": "新开仓"}),
    ]
    gf = GuaFilter(enabled=True, selection_mode="exact_line", selected_state_ids=["02-5"])
    out = filter_events_by_gua_filter(events, gf)
    assert len(out) == 1
    assert out[0].bagua["state_id"] == "02-5"


def test_filter_action_signal():
    events = [
        _Ev({"state_id": "01-1", "gua_order": 1, "action_signal": "持有"}),
        _Ev({"state_id": "01-2", "gua_order": 1, "action_signal": "新开仓"}),
        _Ev({"state_id": "01-4", "gua_order": 1, "action_signal": "加仓"}),
    ]
    gf = GuaFilter(
        enabled=True,
        selection_mode="action_signal",
        selected_action_signals=["新开仓", "加仓"],
    )
    out = filter_events_by_gua_filter(events, gf)
    assert {e.bagua["action_signal"] for e in out} == {"新开仓", "加仓"}


def test_filter_combined_and_or():
    events = [
        _Ev({"state_id": "01-2", "gua_order": 1, "action_signal": "新开仓"}),
        _Ev({"state_id": "01-1", "gua_order": 1, "action_signal": "持有"}),
        _Ev({"state_id": "02-2", "gua_order": 2, "action_signal": "新开仓"}),
    ]
    gf = GuaFilter(
        enabled=True,
        selection_mode="combined",
        selected_main_hexagram_ids=[1],
        selected_action_signals=["新开仓", "加仓"],
    )
    out = filter_events_by_gua_filter(events, gf)
    assert len(out) == 1
    assert out[0].bagua["state_id"] == "01-2"


def test_empty_biangua_compatible():
    ev = _Ev(
        {
            "state_id": "64-6",
            "gua_order": 64,
            "yao_order": 6,
            "action_signal": "减仓",
            "biangua": "",
        }
    )
    gf = GuaFilter(enabled=True, selection_mode="exact_line", selected_state_ids=["64-6"])
    assert event_matches_gua_filter(ev, gf) is True


def test_natural_language():
    gf = GuaFilter(
        enabled=True,
        selection_mode="action_signal",
        selected_action_signals=["新开仓", "加仓"],
    )
    s = gua_filter_natural_language(gf)
    assert "新开仓" in s and "加仓" in s


def test_list_hexagrams_and_states():
    items = list_hexagrams()
    assert len(items) == 64
    assert len(items[0]["lines"]) == 6
    res = list_states(search="乾", page_size=20)
    assert res["total"] >= 1
    assert res["items"][0].get("state_id")


def test_preview_exact():
    d = preview_filter(
        {
            "enabled": True,
            "selection_mode": "exact_line",
            "selected_state_ids": ["01-5", "02-5"],
        }
    )
    assert d["matched_state_count"] == 2
    assert d["selected_exact_line_count"] == 2


def test_ui_contains_gua_drawer():
    html = (
        Path(__file__).resolve().parents[3]
        / "wtpy"
        / "apps"
        / "astock"
        / "web"
        / "static"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert "卦象过滤" in html
    assert "guaDrawer" in html
    assert "GuaUI" in html
    assert "gua_filter" in html
    assert "step-gua" in html
    assert "btnSwdWeekdays" in html
    assert "btnGuaPresetBest3" in html
    # signal colors distinct from stock red/green for open/add
    assert "gua-act-新开仓" in html
    assert ("选择卦象" in html or "打开完整目录" in html)
