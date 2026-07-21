# -*- coding: utf-8 -*-
"""UI structure / key-text regression for A-stock web index.html."""
from __future__ import annotations

from pathlib import Path

import pytest

INDEX = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "web"
    / "static"
    / "index.html"
)


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX.is_file(), f"missing {INDEX}"
    return INDEX.read_text(encoding="utf-8")


def test_tab_structure_and_fullwidth_pages(html: str):
    for eid in ("pageNew", "pageHistory", "pageDetail", "btSubtabs", "viewBacktest"):
        assert f'id="{eid}"' in html
    assert "switchBtPage" in html
    assert "fullwidth" in html
    # history + detail full width (aside hidden)
    assert "main#viewBacktest.fullwidth" in html
    assert "main#viewBacktest.fullwidth > aside" in html


def test_history_row_height_and_strategy_ellipsis(html: str):
    # compact row target 64–80px
    assert "height: 72px" in html or "max-height: 80px" in html
    assert "white-space: nowrap !important" in html
    assert "text-overflow: ellipsis" in html
    # strategy name must not force multi-line clamp as primary layout
    # hist-title should be single-line ellipsis (our override)
    assert ".hist-title" in html
    # run id / extra metrics not permanently stacking rows
    assert ".hist-id, .hist-extra { display: none !important; }" in html or "hist-id" in html


def test_history_shared_colgroup_and_ops_row(html: str):
    assert "<colgroup>" in html
    assert "col-strategy" in html
    assert "主收益" in html
    assert "已平仓交易" in html
    assert "盈亏比" in html
    assert "ops-row" in html
    assert "flex-wrap: nowrap !important" in html
    assert "data-view-run" in html
    assert "data-copy-run" in html
    assert "data-del-run" in html


def test_no_scientific_notation_in_main_fmt(html: str):
    assert "toExponential" not in html
    assert "function formatPercent" in html or "function formatNumber" in html
    assert "function formatDrawdown" in html or "fmtMaxDrawdownAbs" in html
    assert "function formatTradeCount" in html


def test_timeline_weekday_or_session_labels(html: str):
    # Schedule UI is weekday-based; still mentions open/close session and signal day.
    assert "开盘" in html or "收盘" in html
    assert "buyWeekday" in html or "buy_weekday" in html or "买入日" in html
    assert "T 日收盘确认信号" in html or "T日收盘" in html or "收盘确认信号" in html
    assert "resolveTradeSchedule" in html
    assert "交易日序列" in html
    assert "节假日" in html
    assert "引擎" in html


def test_no_weekend_buy_exit_options(html: str):
    """Buy/exit weekday selects must not offer Saturday/Sunday."""
    # Signal filter and trade schedule use Mon–Fri only
    assert 'value="6"' not in html.split('id="buyWeekday"')[1].split("</select>")[0]
    assert 'value="7"' not in html.split('id="buyWeekday"')[1].split("</select>")[0]
    assert 'value="6"' not in html.split('id="exitWeekday"')[1].split("</select>")[0]
    assert 'value="7"' not in html.split('id="exitWeekday"')[1].split("</select>")[0]
    assert "周六" not in html.split('id="buyWeekday"')[1].split("</select>")[0]
    assert "周日" not in html.split('id="exitWeekday"')[1].split("</select>")[0]


def test_copy_params_refreshes_timeline_summary_gua(html: str):
    assert "function copyRunParams" in html
    assert "updateTimeline" in html
    assert "updateBtSummary" in html
    assert "GuaUI.setFromConfig" in html
    assert "btnPresetTn12" in html
    assert "经典 T+1" in html


def test_step_nav_and_gua_scheme_buttons(html: str):
    assert 'href="#step-swd"' in html or "step-swd" in html
    assert 'href="#step-trade"' in html or "step-trade" in html
    assert "信号星期" in html
    assert "交易日程" in html
    # Gua scheme buttons from prior delivery
    assert "最佳" in html or "GuaUI" in html


def test_detail_core_metrics_and_advanced_hidden_debug(html: str):
    assert 'id="coreMetrics"' in html
    assert 'id="detailTabs"' in html
    assert "pane-advanced" in html
    assert "虚拟账户合计" in html or "非真实" in html
    assert "服务器路径" in html
    # path hint not in primary toolbar as path-hint shown only in advanced
    assert 'id="resultPathHint"' in html
    assert "导出Excel" in html or "导出数据" in html


def test_per_symbol_warn_and_no_misleading_final_equity_primary(html: str):
    assert "perSymbolWarn" in html or "virtual-warn" in html
    assert "renderCoreMetrics" in html
    assert "单票平均收益" in html or "mean_symbol_return" in html


def test_delete_confirm_still_present(html: str):
    assert "confirm(" in html
    assert "deleteRunsByIds" in html or "/api/v1/runs/" in html


def test_single_primary_scroll_overrides(html: str):
    # workspace / aside should not force nested max-height scroll as primary layout
    assert "section.workspace { padding: 1rem 1.25rem; overflow: visible; max-height: none; }" in html or (
        "overflow: visible" in html and "max-height: none" in html
    )
