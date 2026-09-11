# -*- coding: utf-8 -*-
"""方案 A 导出区布局回归：纯静态结构不变量（无 Node 依赖）。

覆盖：
- 既有导出/规则选择 id 全文件唯一；新增 bqExtraRuleWarn 唯一
- 「① 附带信号 sheet」「② 选择导出范围」步骤标记各 1 处
- <details class="bq-export-help"> 存在且承载原 #bqHint 长文
- #bqHint 保留 id/class 但内容清空
- 旧 class .bq-export-note / .bq-export-label 无残留
- .bq-export-bar 局部 div/details 配对平衡
- delta 修补：summary:focus-visible 且保留 outline:none；renderBqRuleSummary
  在 early return 前同步 count / 更新警告；initBqRulePick 关面板重置箭头；
  bqHasExtraRules 默认集合只取一次、统一 indexOf 判定
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
V3 = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"

TARGET_IDS = (
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

MOVED_HELP_KEYS = ("导出为周报格式", "日柱自动读", "信号规则：")


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


def test_bq_export_target_ids_unique(v3_html: str):
    for eid in TARGET_IDS:
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
    assert v3_html.count("① 附带信号 sheet") == 1
    assert v3_html.count("② 选择导出范围") == 1


def test_bq_export_help_details_holds_moved_copy(v3_html: str):
    m = re.search(r'<details class="bq-export-help">(.*?)</details>', v3_html, re.S)
    assert m, "missing details.bq-export-help"
    body = m.group(1)
    assert "<summary>" in body
    for key in MOVED_HELP_KEYS:
        assert key in body, key


def test_bq_hint_emptied(v3_html: str):
    m = re.search(r'<div[^>]*\bid="bqHint"[^>]*>(.*?)</div>', v3_html, re.S)
    assert m, "missing #bqHint"
    head, inner = m.group(0), m.group(1)
    assert 'class="muted"' in head
    assert inner.strip() == "", f"#bqHint should be empty, got {inner.strip()[:60]!r}"
    for key in ("指数/ETF 无复权口径",) + MOVED_HELP_KEYS:
        assert key not in inner, key


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
    start = v3_html.find('<div class="bq-export-bar">')
    assert start >= 0
    bar = _extract_element(v3_html, start, "div")
    assert len(re.findall(r"<div\b", bar)) == len(re.findall(r"</div>", bar))
    assert len(re.findall(r"<details\b", bar)) == len(re.findall(r"</details>", bar))
    for eid in (
        "bqRulePickBtn",
        "bqRulePickSummary",
        "bqRulePickPanel",
        "bqExportOnePeriodBtn",
        "bqExportBtn",
        "bqExtraRuleWarn",
    ):
        assert f'id="{eid}"' in bar, eid
