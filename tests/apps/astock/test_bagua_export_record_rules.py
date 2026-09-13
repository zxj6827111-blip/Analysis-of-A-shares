# -*- coding: utf-8 -*-
"""导出记录里的「附加规则 N 条」必须带上规则名。

背景：批量导出创建任务后，导出记录只显示「附加规则 4 条」，用户无法从记录
本身核对究竟附加了哪几条规则。本文件锁定改造后的展示规则：

- 规则目录已加载：条数后面跟规则名，如「附加规则 4 条（A、B、C、D）」。
- 规则目录尚未加载（它是异步接口）：只显示条数，**不得**把规则误标成已删除。
- 规则已被规则中心删除/归档：按 ID 标注「（已删除）」，不借用同名规则顶替。
- 同名规则（如两条「735金叉及趋势」）：补 ID 区分，不做按名去重。
- 规则条数很多：最多列 6 条，其余用「等 N 条」收尾，避免记录行被撑爆。

同时覆盖两个相邻的健壮性点：规则目录到位后导出记录要重绘（否则名字永远不出现），
断连态下的既有记录按钮必须仍然可点。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
V3 = BACKEND_ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"


# ---------------------------------------------------------------------------
# 与 test_bagua_workbench_r2.py 同口径的 node 脚手架
# ---------------------------------------------------------------------------


def _extract_js_function(src: str, name: str) -> str:
    import re

    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\(", src)
    assert m, f"function not found: {name}"
    paren = src.index("(", m.start())
    depth = 0
    i = paren
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    brace = src.index("{", i)
    depth = 0
    for j in range(brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
    raise AssertionError(f"unbalanced {name}")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    assert node, "node required"
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, cwd=str(BACKEND_ROOT)
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    return proc.stdout


_PRELUDE = """
const _mkEl = (id) => ({
  id, innerHTML: '', hidden: true, textContent: '', className: '', value: '', checked: false,
  disabled: false, dataset: {}, style: {},
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, dispatchEvent() {}, click() {}, remove() {}, focus() {},
});
globalThis.__els = {};
globalThis.$ = (id) => (globalThis.__els[id] = globalThis.__els[id] || _mkEl(id));
globalThis.esc = (s) => String(s == null ? '' : s);
globalThis.wbFmtDate = (d) => String(d);
globalThis.wbPriceLabel = (v) => (v === 'raw' ? '未复权' : String(v));
"""

JOB = (
    "{ job_id: 'bqexp_1', status: 'done', scope_summary: '全市场 A 股 + ETF',"
    " date: 20260911, periods: ['DAY', 'WEEK', 'MONTH'], adjust: 'raw',"
    " created_hm: '11:03:28', query_date: 20260911, review_asof_used: 20260911,"
    " review_rules_mode: 'picked', review_rules: %s }"
)

FOUR = "['txt_735金叉及趋势', 'txt_先跌后涨新版5日外', 'rule_a', 'rule_b']"

FOUR_RULES = (
    "globalThis.wbs = { rules: ["
    "{ id: 'txt_735金叉及趋势', name: '735金叉及趋势' },"
    "{ id: 'txt_先跌后涨新版5日外', name: '先跌后涨新版5日外' },"
    "{ id: 'rule_a', name: '规则甲' },"
    "{ id: 'rule_b', name: '规则乙' }"
    "] };\n"
)


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


def _row_html(v3_html: str, rules_js: str, job_js: str) -> str:
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbExportRuleNamesText", "wbExportJobRow")
    )
    script = (
        _PRELUDE
        + rules_js
        + fns
        + "\nconsole.log('HTML<<' + wbExportJobRow("
        + job_js
        + ", false) + '>>HTML');\n"
    )
    out = _run_node(script)
    start, end = out.index("HTML<<") + 6, out.index(">>HTML")
    return out[start:end]


# ---------------------------------------------------------------------------
# 展示规则
# ---------------------------------------------------------------------------


def test_picked_rules_show_names_after_count(v3_html: str):
    """核心诉求：条数后面直接列出这 4 条规则的名称。"""
    html = _row_html(v3_html, FOUR_RULES, JOB % FOUR)
    assert "附加规则 4 条（735金叉及趋势、先跌后涨新版5日外、规则甲、规则乙）" in html


def test_catalog_not_loaded_shows_count_only(v3_html: str):
    """目录异步未到位：只显示条数，不能把规则误标成「已删除」。"""
    html = _row_html(v3_html, "globalThis.wbs = { rules: [] };\n", JOB % FOUR)
    assert "附加规则 4 条 · 实际数据日" in html
    assert "已删除" not in html


def test_deleted_rule_falls_back_to_id(v3_html: str):
    """规则中心删除/归档后名字不可解析：按 ID 标注，不拿同名规则顶替。"""
    rules_js = (
        "globalThis.wbs = { rules: [{ id: 'rule_a', name: '规则甲' }] };\n"
    )
    html = _row_html(v3_html, rules_js, JOB % "['rule_a', 'rule_gone']")
    assert "附加规则 2 条（规则甲、rule_gone（已删除））" in html


def test_duplicate_names_disambiguated_by_id(v3_html: str):
    """同名规则不按名去重：重名时补 ID，避免记录里出现两个无法分辨的名字。"""
    rules_js = (
        "globalThis.wbs = { rules: ["
        "{ id: 'a_1', name: '735金叉及趋势' },"
        "{ id: 'a_2', name: '735金叉及趋势' }"
        "] };\n"
    )
    html = _row_html(v3_html, rules_js, JOB % "['a_1', 'a_2']")
    assert (
        "附加规则 2 条（735金叉及趋势[ID a_1]、735金叉及趋势[ID a_2]）" in html
    )


def test_duplicate_rule_ids_collapsed(v3_html: str):
    """同一规则 ID 重复提交：条数与名称都按去重后口径（导出侧也会合并成一张 sheet）。"""
    html = _row_html(v3_html, FOUR_RULES, JOB % "['rule_a', 'rule_a', 'rule_b']")
    assert "附加规则 2 条（规则甲、规则乙）" in html


def test_long_rule_list_capped(v3_html: str):
    """规则很多时最多列 6 条 + 「等 N 条」，记录行不被撑成一片。"""
    ids = [f"r{i}" for i in range(8)]
    rules_js = "globalThis.wbs = { rules: [" + ",".join(
        f"{{ id: 'r{i}', name: '规则{i}' }}" for i in range(8)
    ) + "] };\n"
    html = _row_html(v3_html, rules_js, JOB % ("['" + "', '".join(ids) + "']"))
    assert (
        "附加规则 8 条（规则0、规则1、规则2、规则3、规则4、规则5 等 8 条）" in html
    )


@pytest.mark.parametrize(
    "mode,expect",
    [("default", "信号 sheet=默认（周五链复核）"), ("none", "不带信号 sheet")],
)
def test_default_and_none_modes_unchanged(v3_html: str, mode: str, expect: str):
    """默认/不附带两种模式的行为不回归（不能被新分支改动）。"""
    job = (
        "{ job_id: 'j', status: 'done', scope_summary: '全市场 A 股 + ETF',"
        f" date: 20260911, periods: ['DAY'], adjust: 'raw', review_rules_mode: '{mode}' }}"
    )
    html = _row_html(v3_html, FOUR_RULES, job)
    assert expect in html


# ---------------------------------------------------------------------------
# 相邻健壮性：重绘时机 + 断连态按钮
# ---------------------------------------------------------------------------


def test_rules_load_rerenders_export_jobs(v3_html: str):
    """两个规则加载入口都要在目录到位后重绘导出记录（否则规则名永不出现）。"""
    for name in ("wbEnsureScreenRules", "wbEnsureExportRules"):
        body = _extract_js_function(v3_html, name)
        assert "wbRerenderExportJobs()" in body, name


def test_rerender_skipped_while_disconnected(v3_html: str):
    """断连态下记录上方还有「暂时断连」横幅：重绘会抹掉它，必须跳过。"""
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbExportJobsHtml", "wbRerenderExportJobs")
    )
    stub = (
        "let wbExportJobsDisconnected = true;\n"
        "let wbExportJobsCache = [{ job_id: 'j1', status: 'done' }];\n"
        "let wbExportJobsStale = [];\n"
        "let rowCalls = 0, bindCalls = 0;\n"
        "globalThis.wbExportJobRow = () => { rowCalls += 1; return 'ROW'; };\n"
        "globalThis.wbBindExportJobActions = () => { bindCalls += 1; };\n"
    )
    script = _PRELUDE + stub + fns + "\n" + (
        "wbRerenderExportJobs();\n"
        "const skipped = rowCalls === 0 && bindCalls === 0 && $('wbEJobs').innerHTML === '';\n"
        "wbExportJobsDisconnected = false;\n"
        "wbRerenderExportJobs();\n"
        "const redrawn = rowCalls === 1 && bindCalls === 1 && /ROW/.test($('wbEJobs').innerHTML);\n"
        "console.log('OK', skipped, redrawn);\n"
    )
    assert "OK true true" in _run_node(script)


def test_rerender_keeps_stale_local_records(v3_html: str):
    """重绘不能只画服务端列表：本地记着但服务端已丢失的记录（失效行）要一起保留。"""
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbExportJobsHtml", "wbRerenderExportJobs")
    )
    stub = (
        "let wbExportJobsDisconnected = false;\n"
        "let wbExportJobsCache = [{ job_id: 'j1', status: 'done' }];\n"
        "let wbExportJobsStale = ['gone_1', 'gone_2'];\n"
        "globalThis.wbExportJobRow = (x, isStale) => (isStale ? 'STALE:' + x.job_id : 'LIVE:' + x.job_id);\n"
        "globalThis.wbBindExportJobActions = () => {};\n"
    )
    script = _PRELUDE + stub + fns + "\n" + (
        "wbRerenderExportJobs();\n"
        "const html = $('wbEJobs').innerHTML;\n"
        "console.log('OK', /LIVE:j1/.test(html), /STALE:gone_1/.test(html), /STALE:gone_2/.test(html));\n"
    )
    assert "OK true true true" in _run_node(script)


def test_disconnected_refresh_keeps_actions_clickable(v3_html: str):
    """断连时保留的既有记录此前只渲染不绑事件（死按钮）：必须绑定下载/重试。"""
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbExportJobsHtml", "wbRefreshExportJobs")
    )
    stub = (
        "let wbExportJobsRefreshing = false;\n"
        "let wbExportJobsCache = [{ job_id: 'j1', status: 'done' }];\n"
        "let wbExportJobsStale = ['gone_1'];\n"
        "let wbExportJobsDisconnected = false;\n"
        "let bindCalls = 0;\n"
        "globalThis.wbBindExportJobActions = () => { bindCalls += 1; };\n"
        "globalThis.wbExportJobRow = () => 'ROW';\n"
        "globalThis.api = async () => { throw new Error('offline'); };\n"
        "globalThis.wbe = { pollTimer: null };\n"
        "globalThis.setTimeout = () => 0;\n"
        "globalThis.clearTimeout = () => {};\n"
    )
    script = _PRELUDE + stub + fns + "\n" + (
        "(async () => {\n"
        "  await wbRefreshExportJobs();\n"
        "  const box = $('wbEJobs');\n"
        "  console.log('OK', wbExportJobsDisconnected, bindCalls,\n"
        "    /暂时断连/.test(box.innerHTML), /ROW/.test(box.innerHTML));\n"
        "})();\n"
    )
    assert "OK true 1 true true" in _run_node(script)
