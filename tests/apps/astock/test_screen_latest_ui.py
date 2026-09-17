# -*- coding: utf-8 -*-
"""筛选页签接入预筛快照缓存端点（阶段 1d）的前端静态断言。

对应 GET /api/v1/bagua/screen/latest：进筛选页签自动加载最近预筛结果。
关键机制：condVersion 竞态防护 + 强制重算复选框 + 来源徽标，全部只做
源码级断言（与 test_bagua_workbench_r2.py 的 v3_html 静态断言同一手法）。
"""

from __future__ import annotations

import re

import pytest

import tests.apps.astock.conftest  # noqa: F401

BACKEND_ROOT = tests.apps.astock.conftest.ROOT
V3 = BACKEND_ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"


def _extract_js_function(src: str, name: str) -> str:
    """按花括号配平提取整个 JS 函数体（与 r2 测试同一实现）。"""
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


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


def test_setmode_screen_calls_load_latest(v3_html: str):
    """① wbSetMode 的 screen 分支必须触发自动加载。"""
    fn = _extract_js_function(v3_html, "wbSetMode")
    m = re.search(r'if\s*\(next\s*===\s*"screen"\)\s*\{', fn)
    assert m, "wbSetMode 缺少 screen 分支"
    branch = fn[m.start():]
    assert "wbLoadLatestScreen()" in branch, "进筛选页签必须自动加载最近预筛结果"
    # 与既有顺序约定一致：先保证规则目录，再加载快照视图
    assert "wbEnsureScreenRules()" in branch


def test_load_latest_has_race_guard(v3_html: str):
    """② 竞态防护：请求前记 condVersion，响应后版本不等即丢弃。"""
    fn = _extract_js_function(v3_html, "wbLoadLatestScreen")
    assert "const ver = wbs.condVersion" in fn, "请求前必须捕获条件版本"
    # 响应回来后版本不等（含提交在途）→ 迟到响应绝不覆盖
    assert re.search(r"if\s*\(\s*wbs\.condVersion\s*!==\s*ver", fn), (
        "响应回来后必须比较 wbs.condVersion 与捕获版本"
    )
    assert "wbs.jobRunning" in fn, "手动提交在跑时自动加载必须让位"
    assert "/api/v1/bagua/screen/latest" in fn, "必须请求新快照端点"
    # 自动加载不得伪装成提交任务（不置 jobRunning/submitting）
    assert "wbs.jobRunning = true" not in fn


def test_load_latest_no_snapshot_is_guidance_not_error(v3_html: str):
    """available:false 只渲染引导文案，不弹错误。"""
    fn = _extract_js_function(v3_html, "wbLoadLatestScreen")
    assert 'j.available !== true' in fn or "j.available !== true" in fn
    assert "wb-error" not in fn, "暂无快照是常态，不得用错误样式"
    assert "暂无预筛快照" in fn, "必须有简短引导文案"


def test_force_recompute_checkbox_and_body(v3_html: str):
    """③ 复选框存在、提交 body 带 force_recompute。"""
    assert v3_html.count('id="wbSForceRecompute"') == 1, "强制重算复选框缺失或重复"
    fn = _extract_js_function(v3_html, "wbRunScreen")
    assert "force_recompute" in fn, "POST body 必须带 force_recompute"
    assert "$(\"wbSForceRecompute\")" in fn, "取值必须来自复选框"
    # 复选框变更按条件变更处理（结果失效走既有 wbScreenChanged 机制）
    bind = _extract_js_function(v3_html, "wbBind")
    m = re.search(r"sForce\.onchange\s*=\s*([^;]+);", bind)
    assert m and "wbScreenChanged" in m.group(1), "复选框变更必须走条件失效逻辑"


def test_latest_endpoint_fetch_present(v3_html: str):
    """④ 前端确实请求了 /api/v1/bagua/screen/latest。"""
    assert v3_html.count("/api/v1/bagua/screen/latest") >= 1


def test_source_badge_exists_and_used(v3_html: str):
    """⑤ wbScreenSourceBadge 存在且被现算/自动加载两条渲染路径共用。"""
    fn = _extract_js_function(v3_html, "wbScreenSourceBadge")
    assert fn.count("cache") >= 1
    assert "预筛快照" in fn and "实时计算" in fn
    job = _extract_js_function(v3_html, "wbRenderScreenJob")
    latest = _extract_js_function(v3_html, "wbRenderLatestScreen")
    assert "wbScreenSourceBadge(" in job, "现算结果渲染必须带来源徽标"
    assert "wbScreenSourceBadge(" in latest, "自动加载渲染必须带来源徽标"


def test_latest_render_reuses_hits_and_snapshot(v3_html: str):
    """自动加载复用既有命中渲染/按钮绑定，并填充 wbs.snapshot 供导出交接。"""
    latest = _extract_js_function(v3_html, "wbRenderLatestScreen")
    assert "wbScreenHitsHtml" in latest, "必须复用命中行渲染"
    assert "wbBindScreenHitButtons" in latest, "「查卦象」按钮绑定必须保留"
    assert "wbs.snapshot = {" in latest, "自动加载结果必须写入 wbs.snapshot 供导出"
    # submitted 标记与当前条件一致（版本相同=未过期），由 wbLoadLatestScreen 置位
    loader = _extract_js_function(v3_html, "wbLoadLatestScreen")
    assert "wbs.submitted" in loader
