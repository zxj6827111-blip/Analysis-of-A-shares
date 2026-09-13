# -*- coding: utf-8 -*-
"""导出区布局回归：纯静态结构不变量（无 Node 依赖）。

PLAN-BAGUA-UX-V1.1 整改后旧查询页导出区（.bq-export-bar）已被「查询卦象」视图内
工作台的批量导出面板（#wb-export-panel）替代：旧 bq* 布局 id 从 HTML 退役，
其 JS 函数（renderBqRuleSummary / initBqRulePick / bqHasExtraRules /
bqExportReviewSuffix）保留为代码级保护。本文件断言：
- 旧布局 id 已全部移除（不残留死 UI）；工作台关键 id 全文件唯一
- 工作台导出面板的结构等价物：范围选择、附加明细、摘要、主按钮、记录区
- 通用静态 id 无重复（原保护保留）
- delta 修补类函数断言（bqHasExtraRules 等）保留——函数仍在源码中
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"

# 旧查询页导出区 id（方案 A）：UI 已退役，全文件不得残留
RETIRED_IDS = (
    "bqRulePickBtn",
    "bqRulePickCount",
    "bqRulePickArrow",
    "bqRulePickSummary",
    "bqRulePickPanel",
    "bqExportOnePeriodBtn",
    "bqExportBtn",
    "bqHint",
    "bqExtraRuleWarn",
)

# 工作台导出面板（替代者）关键 id：各出现且全文件唯一
WORKBENCH_IDS = (
    "wbRoot",
    "wbQSearch",
    "wbQRun",
    "wbSRuleList",
    "wbSRun",
    "wbEScope",
    "wbEAttach",
    "wbERuleList",
    "wbESummary",
    "wbERun",
    "wbEJobs",
)

# 导出面板的模板说明必须与文件真实结构一致：既有样本是 meta / stock-all /
# etf-all（+ 可选规则 sheet）。原文案「日/周/月三个工作表组」与实际不符，已更正。
WORKBENCH_HELP_KEYS = ("报表结构：工作表", "stock-all", "index-all", "etf-all")


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


def test_bq_export_target_ids_unique(v3_html: str):
    for eid in RETIRED_IDS:
        assert v3_html.count(f'id="{eid}"') == 0, f"retired id must be removed: {eid}"
    for eid in WORKBENCH_IDS:
        assert v3_html.count(f'id="{eid}"') == 1, eid


def test_bq_export_static_markup_has_no_duplicate_ids(v3_html: str):
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", v3_html, flags=re.S | re.I)
    markup = re.sub(r"<style\b[^>]*>.*?</style>", "", markup, flags=re.S | re.I)
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)

    class _Ids(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.seen: dict[str, int] = {}

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k == "id" and v:
                    self.seen[v] = self.seen.get(v, 0) + 1

        handle_startendtag = handle_starttag

    p = _Ids()
    p.feed(markup)
    dups = {k: v for k, v in p.seen.items() if v > 1}
    assert not dups, f"duplicate static ids: {dups}"


def test_bq_export_step_markers(v3_html: str):
    # 工作台导出面板的三段结构：范围 / 报表内容 / 附加明细（对应旧 ①②步骤标记）
    assert v3_html.count("选择导出范围") >= 1
    assert v3_html.count("确认报表日期、口径和模板内容") >= 1
    assert v3_html.count("可选附加信号明细") >= 1
    # 旧查询页步骤文案不残留
    assert "① 附带信号 sheet" not in v3_html
    assert "② 选择导出范围" not in v3_html


def test_bq_export_help_details_holds_moved_copy(v3_html: str):
    # 旧帮助 details 已退役；周报模板说明迁入工作台导出面板
    assert '<details class="bq-export-help">' not in v3_html
    for key in WORKBENCH_HELP_KEYS:
        assert key in v3_html, key
    # 附加明细语义：增加工作表、不改变主表范围
    assert "不改变主表的股票范围" in v3_html


def test_bq_hint_emptied(v3_html: str):
    # 旧 #bqHint 长提示随旧 UI 移除；工作台用 wbQHelp/wbQDateNote 等短提示承接
    assert v3_html.count('id="bqHint"') == 0
    for key in ("指数/ETF 无复权口径",) + WORKBENCH_HELP_KEYS:
        assert key not in v3_html or key in WORKBENCH_HELP_KEYS


def test_bq_export_legacy_classes_removed(v3_html: str):
    assert "bq-export-note" not in v3_html
    assert "bq-export-label" not in v3_html


def _extract_function(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", src)
    assert m, f"function not found: {name}"
    brace = src.index("{", m.start())
    depth = 0
    for j in range(brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
    raise AssertionError(f"unbalanced {name}")


def test_bq_export_help_summary_focus_visible(v3_html: str):
    css = v3_html.split("</style>", 1)[0]
    base = re.search(r"\.bq-export-help summary\{([^}]*)\}", css)
    assert base, "missing base .bq-export-help summary rule"
    assert "outline:none" in base.group(1)
    focus = re.search(r"\.bq-export-help summary:focus-visible\{([^}]*)\}", css)
    assert focus, "missing .bq-export-help summary:focus-visible"
    body = focus.group(1)
    assert "outline:2px solid var(--blue)" in body
    assert "outline-offset:2px" in body


def test_render_bq_rule_summary_syncs_count_before_early_return(v3_html: str):
    fn = _extract_function(v3_html, "renderBqRuleSummary")
    i_cnt = fn.find('$("bqRulePickCount")')
    i_warn = fn.find("updateBqExtraRuleWarn()")
    i_early = fn.find("if (!el) return")
    assert i_early > 0
    assert 0 <= i_cnt < i_early, "count sync must precede the summary early return"
    assert 0 <= i_warn < i_early, "warn update must precede the summary early return"
    assert "cnt.textContent = String(ids.length)" in fn


def test_init_bq_rule_pick_resets_arrow_on_close(v3_html: str):
    fn = _extract_function(v3_html, "initBqRulePick")
    assert re.search(
        r'if \(panel\) \{\s*panel\.style\.display = "none";\s*'
        r'const arrow = \$\("bqRulePickArrow"\);\s*'
        r'if \(arrow\) arrow\.textContent = "▾";\s*\}',
        fn,
    ), "close-panel branch must reset arrow to ▾"
    assert 'willShow ? "▴" : "▾"' in fn


def test_bq_has_extra_rules_source_cleanup(v3_html: str):
    fn = _extract_function(v3_html, "bqHasExtraRules")
    assert fn.count("const defaults = bqDefaultRuleIds();") == 1
    assert fn.count("defaults.indexOf(id) < 0") == 2
    assert ".includes(id)" not in fn
    assert "|| []" not in fn


def _extract_element(src: str, start: int, tag: str) -> str:
    open_re = re.compile(rf"<{tag}\b", re.I)
    close_re = re.compile(rf"</{tag}>", re.I)
    assert open_re.match(src, start), f"start {start} is not <{tag}>"
    depth = 0
    pos = start
    while True:
        mo = open_re.search(src, pos)
        mc = close_re.search(src, pos)
        assert mc is not None, f"</{tag}> not found from {pos}"
        if mo is not None and mo.start() < mc.start():
            depth += 1
            pos = mo.end()
        else:
            depth -= 1
            pos = mc.end()
            if depth == 0:
                return src[start:pos]


def test_bq_export_bar_locally_balanced(v3_html: str):
    # 旧 .bq-export-bar 已随旧查询页退役；替代结构 = 工作台导出面板
    assert '<div class="bq-export-bar">' not in v3_html
    start = v3_html.find('<section class="wb-body" id="wb-export-panel"')
    assert start >= 0, "missing workbench export panel"
    panel = _extract_element(v3_html, start, "section")
    assert len(re.findall(r"<div\b", panel)) == len(re.findall(r"</div>", panel))
    for eid in ("wbEScope", "wbEAttach", "wbERuleList", "wbESummary", "wbERun", "wbEJobs"):
        assert f'id="{eid}"' in panel, eid
    # 查询页签不再包含大块导出设置（视觉验收：导出设置独立成页签）
    qpanel = _extract_element(
        v3_html, v3_html.find('<section class="wb-body" id="wb-query-panel"'), "section"
    )
    assert "wb-export-bar" not in qpanel
    assert "创建导出任务" not in qpanel


# ---------------------------------------------------------------------------
# 主导航（UX 精简）：隐藏「预测」，栏目名统一为 回测/卦象/规则/实验/任务/数据
# ---------------------------------------------------------------------------

NAV_VIEWS = ("backtest", "bagua-query", "rules", "experiment", "tasks", "datastore")
NAV_LABELS = ("回测", "卦象", "规则", "实验", "任务", "数据")


def test_main_nav_hides_forecast_and_unifies_labels(v3_html: str):
    m = re.search(
        r'<nav class="main-nav" id="mainNav">(.*?)</nav>', v3_html, re.S
    )
    assert m, "main-nav 未找到"
    items = re.findall(
        r'data-view="([^"]+)"[^>]*><span class="nav-icon">[^<]*</span>([^<]+)</button>',
        m.group(1),
    )
    assert [v for v, _l in items] == list(NAV_VIEWS), items
    assert [lbl.strip() for _v, lbl in items] == list(NAV_LABELS), items
    # 预测不再出现在导航（view 区块与 ?module=forecast 深链保留，可随时恢复）
    assert "forecast" not in [v for v, _l in items]
    assert 'id="view-forecast"' in v3_html, "预测视图区块不应被删除，仅从导航隐藏"
