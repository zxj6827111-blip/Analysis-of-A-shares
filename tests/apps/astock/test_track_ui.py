# -*- coding: utf-8 -*-
"""跟踪页签（阶段 3 三级钻取 UI）的前端静态断言。

对应只读后端 `wtpy/apps/astock/api_routes/tracking.py`：
- L0 `GET /api/v1/bagua/track/rules`
- L1 `GET /api/v1/bagua/track/rules/{rule_id}/weeks`
- L2 `GET /api/v1/bagua/track/weeks/{entry_asof}`
- 导出 `GET /api/v1/bagua/track/export`（后端由他人实现，前端只调用+兜底）

只做源码级断言（与 test_screen_latest_ui.py / test_bagua_workbench_r2.py 同一
手法）：页签注册、三个渲染函数与三个 fetch、竞态防护、覆盖率黄色警示、
回填/子集免责全文（悬停说明，不占常驻横幅）、补算提交后收抽屉 + 任务浮标、导出调用。
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


def test_track_view_registered(v3_html: str):
    """① 跟踪是一级栏目：导航按钮 + 独立视图 + 面板，且已迁出工作台页签。

    2026-09-15 用户反馈「应独立成栏目」：结构从 wb-mode 页签改为
    section.view#view-track，导航位置固定在「回测」与「卦象」之间。
    """
    assert 'data-view="track"' in v3_html, "缺少跟踪一级导航按钮"
    assert 'id="view-track"' in v3_html, "缺少 view-track 独立视图"
    assert 'id="wb-track-panel"' in v3_html, "缺少跟踪面板"
    # 导航顺序：回测 → 跟踪 → 卦象
    i_bt = v3_html.index('data-view="backtest"')
    i_track = v3_html.index('data-view="track"')
    i_bagua = v3_html.index('data-view="bagua-query"')
    assert i_bt < i_track < i_bagua, "跟踪导航必须在回测与卦象之间"
    # 面板归属新容器：样式作用域由 #wbRoot 改为类选择器 .wb-root（两视图共用）
    assert 'id="wbTrackRoot" class="wb-root"' in v3_html, "跟踪容器缺少 .wb-root 作用域"
    # CSS 里不再有 #wbRoot 选择器（历史说明性注释里提到不算）
    assert not re.search(r"#wbRoot\s*[{,.:\[]", v3_html), \
        "CSS 作用域应统一为 .wb-root（不再是 id 选择器）"
    # 已迁出工作台：不再是 wb-mode 页签，页签只剩 3 个
    assert 'data-wb-mode="track"' not in v3_html, "跟踪不应再是工作台页签"
    assert v3_html.count('class="wb-mode" data-wb-mode=') == 3, "工作台页签应只剩 3 个"
    # 独立视图内不再靠 hidden 控制显隐（由 section.view.active 负责）
    assert not re.search(
        r'<section class="wb-body" id="wb-track-panel"[^>]*hidden', v3_html
    ), "跟踪面板不应再用 hidden（视图切换负责显隐）"


def test_switch_module_registers_track(v3_html: str):
    """② switchModule 登记 track：绑定按钮 + 懒加载 L0 + ?module= 白名单。"""
    fn = _extract_js_function(v3_html, "switchModule")
    m = re.search(r'if\s*\(name\s*===\s*"track"\)\s*\{', fn)
    assert m, "switchModule 缺少 track 分支"
    branch = fn[m.start():m.start() + 500]
    assert "wbtEnterTrack()" in branch, "进入跟踪栏目必须（懒）加载 L0"
    assert "wbtBind()" in branch, "首屏直达 ?module=track 时按钮必须先绑定"
    # ?module=track 直达白名单（getCurrentModule 与 boot 的 mod 白名单）
    assert '"track"' in _extract_js_function(v3_html, "getCurrentModule"), \
        "getCurrentModule 白名单缺少 track"
    assert re.search(r'\[\s*"rules",\s*"experiment"[^\]]*"track"\s*\]', v3_html), \
        "boot 的 ?module= 白名单缺少 track"
    # wbSetMode 不再管 track（已迁出工作台）
    assert "track" not in _extract_js_function(v3_html, "wbSetMode"), \
        "wbSetMode 不应再引用 track"


def test_track_bind_is_independent_and_idempotent(v3_html: str):
    """②b 跟踪面板绑定独立于工作台（首屏直达 ?module=track 也能用）。

    原绑定挂在 wbBind（wbInited 守卫，只有进过卦象才执行）；升为一级
    栏目后首屏直达不经过工作台，若不独立绑定，搜索/导出/返回全失效。
    """
    fn = _extract_js_function(v3_html, "wbtBind")
    for el in ("wbTrackSearch", "wbTrackExport", "wbTrackL1Back", "wbTrackL2Back"):
        assert f'$("{el}")' in fn, f"wbtBind 缺少 {el} 绑定"
    assert "wbTrackRoot" in fn, "口径按钮应在跟踪容器内查找（迁移后 wbRoot 不含它）"
    assert "dataset.wbtBound" in fn, "必须幂等（重复进入不重复绑定）"
    # 工作台的 wbBind 仍调用一次（进卦象时保持行为不变）
    assert "wbtBind()" in _extract_js_function(v3_html, "wbBind")


def test_three_level_render_functions(v3_html: str):
    """③ L0/L1/L2 三个渲染函数齐备，且各自对应一个只读端点。"""
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    weeks = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "/api/v1/bagua/track/rules" not in overview  # fetch 在加载函数里，渲染只画
    assert "wbTrackL0List" in overview
    assert "wbTrackL1List" in weeks
    assert "wbTrackL2List" in detail
    # 三个 fetch 分别落在三个加载函数里
    load_l0 = _extract_js_function(v3_html, "wbtLoadOverview")
    assert "/api/v1/bagua/track/rules" in load_l0
    load_l1 = _extract_js_function(v3_html, "wbtOpenRuleWeeks")
    assert "/api/v1/bagua/track/rules/" in load_l1 and "/weeks?weeks=26" in load_l1
    load_l2 = _extract_js_function(v3_html, "wbtOpenWeek")
    assert "/api/v1/bagua/track/weeks/" in load_l2 and "rule_id=" in load_l2


def test_track_loads_have_race_guard(v3_html: str):
    """④ 竞态防护：请求前自增 token + 捕获 condVersion，响应后双重校验。"""
    for name in ("wbtLoadOverview", "wbtOpenRuleWeeks", "wbtOpenWeek"):
        fn = _extract_js_function(v3_html, name)
        assert "++wbt.token" in fn, f"{name} 请求前必须自增 token（防乱序覆盖）"
        assert "const ver = wbt.condVersion" in fn, f"{name} 请求前必须捕获条件版本"
        assert re.search(r"if\s*\(\s*token\s*!==\s*wbt\.token\s*\|\|\s*ver\s*!==\s*wbt\.condVersion\s*\)\s*return", fn), (
            f"{name} 响应回来后必须比较 token 与 condVersion 并丢弃迟到响应"
        )
        assert "catch" in fn, f"{name} 必须 try/catch，失败不得让页签白屏"


def test_coverage_warning_logic(v3_html: str):
    """⑤ 覆盖率分口径渲染：null→「—」，<0.9 黄色警示，绝不渲染绿色。"""
    fn = _extract_js_function(v3_html, "wbtCoverageHtml")
    assert "coverage" not in fn  # 传参是 coverage 对象，函数体不硬编码端点字段名
    for key in ("signal_close", "week_first_open", "excess"):
        assert key in fn, f"覆盖率必须分口径渲染 {key}"
    assert "wb-warn" in fn, "低覆盖率必须黄色警示"
    assert "< 0.9" in fn, "覆盖率阈值判断缺失"
    assert "wb-ok" not in fn and "已结算" not in fn, "覆盖率达标也不得渲染成绿色「已结算」"
    assert '"—"' in fn or "—" in fn, "分母为 0（null）必须显示占位符"
    # 覆盖率警示函数确实被 L2 明细使用
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "wbtCoverageHtml(" in detail, "L2 必须展示覆盖率"


def test_backfill_notice_is_hover_only(v3_html: str):
    """⑥ 回填免责改成悬停说明，不再占常驻黄色横幅。

    2026-09-17 用户反馈「这两条黄提示有点碍眼」：文案本身没问题，问题是常驻占版面。
    契约 §9 要求 UI 能读到全文，因此改成挂在「回填」标签的 title 上（正文仍直接取
    后端 `j.backfill_notice`，前端不另写措辞），导出 meta 里同样带全文。
    """
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "backfill_notice" in detail, "L2 必须引用 backfill_notice"
    assert "esc(j.backfill_notice)" in detail, "回填提示必须转义后全文渲染"
    assert "backfillTip" in detail and "title=" in detail, "免责全文必须挂在标签的悬停说明上"
    assert 'wb-warn">回填数据提示' not in detail, "回填免责不得再作为常驻警示框渲染"
    assert "回填数据提示" not in v3_html, "常驻回填警示框文案应已移除"


def test_export_button_calls_track_export(v3_html: str):
    """⑦ 导出按钮存在并调用跟踪导出端点，失败有兜底提示。"""
    assert 'id="wbTrackExport"' in v3_html, "缺少导出跟踪结果按钮"
    fn = _extract_js_function(v3_html, "wbtExportTrack")
    assert "/api/v1/bagua/track/export" in fn, "必须调用跟踪导出端点"
    assert "download_url" in fn, "必须使用响应里的 download_url"
    assert "跟踪导出暂不可用" in fn, "ok:false/404 必须给出明确兜底提示"
    assert "catch" in fn, "导出必须 try/catch"
    bind = _extract_js_function(v3_html, "wbtBind")
    assert '$("wbTrackExport")' in bind, "导出按钮必须绑定事件"


def test_l0_null_semantics_and_sample_warning(v3_html: str):
    """null 语义：胜率/均值 null 显示「—」；总票次<30 标注「样本不足」。"""
    assert "function wbtPct(" in v3_html, "缺少百分数/null 格式化助手"
    fn = _extract_js_function(v3_html, "wbtPct")
    assert 'return "—"' in fn, "null/NaN 必须显示「—」而不是 0"
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "insufficient_sample" in overview and "样本不足" in overview
    assert "latest_week" in overview and "data-wbt-week" in overview, "本周入选应可直达 L2"
    assert "fingerprint" in overview, "多版本按指纹前缀标注"
    # 双口径列
    assert "weekly_equal_mean_ret_exec" in overview and "weekly_equal_mean_ret_sig" in overview
    # 排序与搜索
    assert "data-wbt-sort" in overview and "wbtSortValue" in overview
    assert '$("wbTrackSearch")' in _extract_js_function(v3_html, "wbtBind")


def test_empty_states_are_guidance(v3_html: str):
    """空态：无发布周给引导文案，不当错误。"""
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "暂无跟踪数据" in overview
    assert "周五链首次运行后自动生成" in overview


# ---------------------------------------------------------------------------
# L2 列表页（2026-09-15 用户反馈改造）：精简列 + 名称 + 展开详情 + 排序
# ---------------------------------------------------------------------------


def test_l2_slim_columns_with_name_and_week_end_close(v3_html: str):
    """L2 默认列 = 用户要求的精简列（代码/名称/首日开盘/期末收盘/最高收益/
    本周收益/超额收益/成交状态等），诊断列收进详情。"""
    m = re.search(r"const WBT_L2_COLS = \[(.*?)\n  \];", v3_html, flags=re.S)
    assert m, "缺少 L2 列定义"
    cols = m.group(1)
    for need in ("代码", "名称", "周卦", "首日开盘", "期末收盘", "最高收益", "本周收益", "成交状态"):
        assert need in cols, f"L2 默认列缺少「{need}」"
    assert "week_gua" in cols, "L2 列配置必须包含 week_gua 字段键"
    # 诊断列不再进默认列（收进详情行）
    for gone in ("周一", "周二", "峰谷回撤"):
        assert ('label: "%s"' % gone) not in cols, f"「{gone}」应从默认列收进详情"
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "week_gua" in detail, "L2 表格渲染必须包含 week_gua"
    # 名称列：产物 name 字段（缺名显示「—」，绝不拿代码冒充）
    assert "r.name" in detail, "L2 必须渲染产物里的股票名称"
    # 期末收盘价用产物的显式字段，不从 daily 反推
    assert "close_week_end" in detail, "L2 必须用 close_week_end（期末收盘价）"
    # 见顶「周几」：日期 + 中文星期
    assert "wbtWeekdayCn(" in detail, "最高收益必须标注见顶在周几"
    assert ("最高收益" in detail or "最高涨幅" in detail) and ("max_gain_exec" in detail or "max_gain_sig" in detail)
    # V1.1 exec 口径核心字段断言
    assert "entry_open_week" in detail, "L2 必须包含 entry_open_week"
    assert "ret_close_exec" in detail, "L2 必须包含 ret_close_exec"
    assert "excess_exec" in detail, "L2 必须包含 excess_exec"
    row_detail = _extract_js_function(v3_html, "wbtRowDetailHtml")
    assert "ret_vs_week_open" in row_detail, "详情行逐日收益必须按首日开盘口径"
    assert "wbt-l2-bagua-wrap" in row_detail, "详情行必须包含周卦与月卦共识面板"
    assert "周卦：" in row_detail and "月卦：" in row_detail, "面板必须包含周卦与月卦"
    assert "wbt-btn-query" not in row_detail, "左侧头部查卦象按钮已依要求移除"



def test_l2_row_expand_sort_and_unbuyable_mark(v3_html: str):
    """展开详情、表头排序、一字涨停标注、进行中标注。"""
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert 'data-wbt-expand=' in detail, "缺少展开按钮"
    assert 'data-wbt-l2sort=' in detail, "缺少表头排序钩子"
    assert "wbtSortL2Rows(" in detail, "渲染前必须排序"
    assert "limit_up_unbuyable" in detail, "一字涨停买不进必须显著标注"
    assert "进行中" in detail, "窗口未结束要标「进行中」而不是「已结算」"
    # 详情行按真实交易日渲染（不写死周一~周五）
    row_detail = _extract_js_function(v3_html, "wbtRowDetailHtml")
    assert "weekDates" in row_detail, "详情必须按跟踪周实际交易日渲染"
    assert "track_week_dates" in _extract_js_function(v3_html, "wbtRenderWeekDetail")
    # 绑定：展开/排序都在 wbtBindWeekDetail 里（本地重绘，不重发请求）
    bind = _extract_js_function(v3_html, "wbtBindWeekDetail")
    assert "data-wbt-expand" in bind and "data-wbt-l2sort" in bind
    assert "fetch(" not in bind and "api(" not in bind, "展开/排序必须本地重绘，不重发请求"
    # 排序值：本周涨幅随口径切换（exec/sig）
    sortv = _extract_js_function(v3_html, "wbtL2SortValue")
    assert "ret_close_exec" in sortv and "ret_close_sig" in sortv
    assert "wbtCaliberIsExec()" in sortv, "本周涨幅排序必须随口径"
    # 缺值排序沉底（不允许把无数据票排到最前）
    sorter = _extract_js_function(v3_html, "wbtSortL2Rows")
    assert "return 1" in sorter and "return -1" in sorter


def test_l1_columns_match_exec_values(v3_html: str):
    """L1 列头与取值必须同口径（原先列头随口径切、取值固定用信号收盘字段）。

    口径固定首日开盘后：列头写「平均收益(首日开盘)」，取值必须是
    mean_ret_close_exec / win_rate_exec。
    """
    fn = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    assert "平均收益(首日开盘)" in fn and "胜率(首日开盘)" in fn
    assert "mean_ret_close_exec" in fn, "收益取值必须是首日开盘口径"
    assert "win_rate_exec" in fn, "胜率取值必须是首日开盘口径"
    assert "mean_ret_close_sig" not in fn and "win_rate_sig" not in fn, \
        "L1 不应再引用信号收盘口径字段（会与列头矛盾）"
    # 收益类数字带涨跌色
    assert "wbtPctSigned(" in fn


def test_short_code_display(v3_html: str):
    """代码去掉 SSE.STK./SZSE.STK. 前缀（用户要求），但查卦象仍用完整码。"""
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "r.code_disp || r.code" in detail, "L2 代码列必须优先用后端短码"
    # 查卦象按钮仍传完整 std_code（否则解析不到标的）
    assert 'data-wbt-q="' + "' + esc(r.code) + '" in detail, "查卦象必须用完整代码"


def test_l0_prefers_backend_rule_name(v3_html: str):
    """L0 规则名优先用后端直出的 rule_name（消除 wbs.rules 未加载的时序坑）。"""
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "r.rule_name" in overview, "L0 必须优先用后端 rule_name"
    assert "wbtRuleName(r.rule_id)" in overview, "仍保留本地映射与裸 id 的回落链"


def test_caliber_fixed_to_exec_and_explained(v3_html: str):
    """口径固定「首日开盘」：切换按钮与切换函数已移除（用户要求隐藏信号收盘）。

    产物里的 sig 口径数据仍保留（只是不展示），将来要恢复只需加回按钮。
    """
    m = re.search(r'caliber:\s*"(\w+)"', v3_html)
    assert m and m.group(1) == "exec", "口径必须固定首日开盘（可执行口径）"
    assert "data-wbt-caliber" not in v3_html, "口径切换按钮应已移除"
    assert "function wbtSetCaliber" not in v3_html, "口径切换函数应已移除"
    # 口径说明段按用户要求删除（2026-09-16）：口径信息由列头标注与 L2 tooltip 承载
    assert "平均收益(首日开盘)" in v3_html
    assert "本周涨幅(首日开盘)" in v3_html
    # 涨跌配色（A 股习惯：红涨绿跌）
    assert ".wb-root .wb-pos" in v3_html and "var(--red)" in v3_html
    assert ".wb-root .wb-neg" in v3_html and "var(--green)" in v3_html
    signed = _extract_js_function(v3_html, "wbtPctSigned")
    assert "wb-pos" in signed and "wb-neg" in signed, "收益类数字必须带涨跌色"


def test_backfill_drawer_structure_and_behavior(v3_html: str):
    """历史补算升级为右侧抽屉（V1.1 契约）：默认 hidden 收起，L0 按钮呼出。

    包含遮罩、抽屉容器、关闭按钮、单周/批量 Tab 切换、Escape 键盘支持，
    且包含原有控件 ID（保证功能与测试兼容）。
    """
    assert 'id="wbTrackBfDrawer"' in v3_html, "缺少补算抽屉容器"
    assert 'id="wbTrackBfMask"' in v3_html, "缺少抽屉遮罩"
    assert 'id="wbTrackBfOpen"' in v3_html, "缺少打开补算抽屉按钮"
    assert 'id="wbTrackBfClose"' in v3_html, "缺少关闭抽屉按钮"
    assert 'id="wbTrackBfCancel"' in v3_html, "缺少取消按钮"
    assert 'id="wbTrackBfTabSingle"' in v3_html, "缺少指定历史周 Tab"
    assert 'id="wbTrackBfTabBatch"' in v3_html, "缺少批量最近 N 周 Tab"
    assert 'role="dialog"' in v3_html and 'aria-modal="true"' in v3_html
    # 默认隐藏
    m_drawer = re.search(r'<aside id="wbTrackBfDrawer"[^>]*hidden', v3_html)
    assert m_drawer, "抽屉默认必须 hidden"
    m_mask = re.search(r'<div id="wbTrackBfMask"[^>]*hidden', v3_html)
    assert m_mask, "遮罩默认必须 hidden"
    # 抽屉标题与说明
    assert "历史补算" in v3_html
    assert "补算说明" in v3_html
    # 控件与轮询逻辑都在（收起不影响功能）
    for el in ("wbTrackBfWeek", "wbTrackBfRun", "wbTrackBfJobs"):
        assert 'id="%s"' % el in v3_html
    # ESC 键关闭与滚动锁定
    bind = _extract_js_function(v3_html, "wbtBind")
    assert "Escape" in bind and "wbtCloseBackfillDrawer" in bind
    assert "wbt-scroll-locked" in v3_html


def test_pending_picks_show_name_and_how_to_settle(v3_html: str):
    """待结算 chips 带名称 + 告知如何结算（原来只有一坨代码，用户看不懂）。"""
    fn = _extract_js_function(v3_html, "wbtPendingHtml")
    assert "p.name" in fn, "待结算票必须显示名称"
    assert "每周五晚自动结算" in fn, "必须告诉用户如何结算"
    assert "补算指定周" in fn, "必须指向补算入口"


# ---------------------------------------------------------------------------
# 补算指定历史周（2026-09-15 用户要求：能指定 2026-07-31 这类历史周测试）
# ---------------------------------------------------------------------------


def test_backfill_entry_submits_and_polls(v3_html: str):
    """补算入口：信号日/最近 N 周 → 提交 → 轮询 → 完成自动刷新 L0。"""
    for el in ("wbTrackBfWeek", "wbTrackBfRun", "wbTrackBfWeeks",
               "wbTrackBfRunN", "wbTrackBfJobs"):
        assert 'id="%s"' % el in v3_html, "缺少补算控件 %s" % el
    submit = _extract_js_function(v3_html, "wbtRunBackfill")
    assert "/api/v1/bagua/track/backfill" in submit
    assert 'method: "POST"' in submit, "补算是写操作，必须 POST"
    poll = _extract_js_function(v3_html, "wbtPollBackfill")
    assert "/api/v1/bagua/track/backfill/status" in poll
    assert "setTimeout" in poll, "必须轮询（分钟级任务）而不是只查一次"
    assert "wbtLoadOverview()" in poll, "补算结束后必须刷新 L0（结果可能刚出现）"
    bind = _extract_js_function(v3_html, "wbtBind")
    assert '"wbTrackBfRun"' in bind and '"wbTrackBfRunN"' in bind, "补算按钮必须绑定"
    # 进入栏目即同步一次状态（可能有在跑的任务）
    assert "wbtPollBackfill()" in _extract_js_function(v3_html, "wbtEnterTrack")
    # 免责：补算=按当前规则重建，页面上必须写明
    assert "不等于当时真实发布的名单" in v3_html
    # retryable（退出码 3）的语义必须如实分开说：指定规则补算**不会**被服务端
    # 自动重试（重任务待办键含规则清单、_heavy_job_command 不猜命令），
    # 整周补算才进自动重试队列。2026-09-17 用户实测踩到过一次误导文案。
    jobs = _extract_js_function(v3_html, "wbtRenderBackfillJobs")
    assert "wbtBackfillRetryableNote(" in jobs, "退出码 3 的说明必须走共用文案函数"
    note = _extract_js_function(v3_html, "wbtBackfillRetryableNote")
    assert "不会被服务端自动重试" in note, "指定规则补算必须说明不会自动重试"
    assert "稍后会自动重试" in note, "整周补算仍应说明会进自动重试队列"
    assert "rule_ids" in note, "两种模式的说明必须按有无 rule_ids 分开"
    # 退出码 3 的真正原因在 CLI 输出末尾：抽屉里要能就地看到，不用翻日志
    assert "j.output" in jobs, "必须用任务输出展示原因"
    assert '"running"' in jobs and '"queued"' in jobs, "只对已结束的任务展示输出"
    assert "<details" in jobs and "服务端输出" in jobs, "失败/未通过原因必须可展开查看"
    assert "esc(out.join" in jobs, "服务端输出必须转义后渲染"


def test_headers_carry_caliber_label(v3_html: str):
    """L0/L1/L2 的收益/胜率列头必须标口径，切口径列头跟着变。

    用户反馈「看不懂信号收盘/首日开盘」：只在按钮上写不够，表格里两张
    口径的数字会看起来互相矛盾——列头必须带口径名。
    """
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "caliberCn" in overview, "L0 列头未标口径"
    assert "周胜率(" in overview and "周平均收益(" in overview
    weeks = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    # 口径固定首日开盘（信号收盘已隐藏）：L1 列头只需标这一个口径
    assert "首日开盘" in weeks, "L1 列头未标口径"
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "retLabel" in detail and "本周涨幅" in detail, "L2 本周涨幅列头未标口径"


def test_l2_expand_state_reset_between_weeks(v3_html: str):
    """换周必须清展开集合（/review 修正）：键是 code|rule_id，跨周同票
    同规则会被误当成「用户展开过」。"""
    fn = _extract_js_function(v3_html, "wbtOpenWeek")
    assert "wbt.l2Expanded.clear()" in fn, "进新周前必须清空展开集合"
    # 展开行有独立样式（与主行视觉分层）
    assert ".wb-l2-detail" in v3_html and re.search(r"\.wb-l2-detail\s*>\s*td", v3_html)


def test_backfill_poll_stops_when_leaving_track_view(v3_html: str):
    """离开跟踪栏目即停止补算轮询（/review 修正）：后台任务继续跑，
    但页面不再每 5s 发请求；回来 wbtEnterTrack 会续上。"""
    fn = _extract_js_function(v3_html, "switchModule")
    assert 'name !== "track"' in fn and "wbt.bfTimer" in fn and "clearTimeout" in fn
    # 轮询续接点在 wbtEnterTrack（回栏目自动恢复）
    assert "wbtPollBackfill()" in _extract_js_function(v3_html, "wbtEnterTrack")
    # 死代码清理：L2 重构后 wbtDailyMap 不再被引用，必须删掉
    assert "function wbtDailyMap" not in v3_html


def test_task_center_shows_track_backfill(v3_html: str):
    """任务中心聚合补算任务（与回测/实验并列），点开跳跟踪栏目。"""
    unify = _extract_js_function(v3_html, "unifyTasks")
    assert "trackJobs || []" in unify, "任务中心未聚合补算任务"
    assert "track_backfill" in unify
    load = _extract_js_function(v3_html, "loadTasks")
    assert "/api/v1/bagua/track/backfill/status" in load, "loadTasks 必须拉补算状态"
    assert "unifyTasks(runs, exps, liveJobs, trackJobs)" in load
    # 点开详情：补算没有 run 详情页 → 跳跟踪栏目（不走 run 详情路径）
    assert 'task.kind === "track_backfill"' in v3_html, "点击处理缺少补算分支"
    assert 'switchModule("track")' in v3_html
    # 任务类型筛选与标签
    assert '<option value="track_backfill">跟踪补算</option>' in v3_html
    assert 'r.kind === "track_backfill" ? "跟踪补算"' in v3_html


# ---------------------------------------------------------------------------
# 指定规则补算（2026-09-16）：规则多选 + 子集周标注
# ---------------------------------------------------------------------------


def test_backfill_rule_picker_present(v3_html: str):
    """规则选择器：抽屉面板 + 单选范围切换 + 检索 + 复选列表 + chips。

    2026-09-16 V1.1 UI：整周回填 vs 最多 5 条指定规则切换，
    单周与批量分 Tab 隔离，已选 chips 与计数同步。
    """
    assert 'id="wbTrackBfSinglePanel"' in v3_html, "缺少单周补算面板"
    assert 'id="wbTrackBfBatchPanel"' in v3_html, "缺少批量补算面板"
    assert 'id="wbTrackBfRuleSummary"' in v3_html
    assert "规则：全部规则" in v3_html
    for el in ("wbTrackBfRuleSearch", "wbTrackBfRuleList", "wbTrackBfRuleChips",
               "wbTrackBfRuleCount", "wbTrackBfRuleClear"):
        assert 'id="%s"' % el in v3_html, "缺少规则选择器控件 %s" % el
    # 上限必须前置可见（别等勾选到第 6 条才被 400 顶回来）
    assert "最多 5 条" in v3_html
    # 两个操作必须分块（「补某一周」与「批量」分 Tab）
    assert "批量补最近 N 周" in v3_html
    assert "不支持指定规则" in v3_html
    # 「不选规则=补全部规则」的语义必须在默认视图中可见
    assert "不选规则=补全部规则" in v3_html


def test_backfill_rules_lazy_load_and_executable_only(v3_html: str):
    """规则列表懒加载（打开抽屉才拉）且只列可执行规则。"""
    loader = _extract_js_function(v3_html, "wbtLoadBackfillRules")
    assert "/api/v1/bagua/screen/rules" in loader, "规则来源必须是筛选规则目录（同一口径）"
    assert "r.executable" in loader, "不可执行规则提交必被后端 400 拒绝，不能列出误导"
    assert "bfRulesLoaded" in loader, "必须做一次性加载缓存（不重复打接口）"
    # 加载后剔除失效勾选（规则被删后保持勾选会在提交时 400）
    assert "wbt.bfChecked" in loader and "live" in loader
    # 懒加载挂在抽屉打开函数上
    open_fn = _extract_js_function(v3_html, "wbtOpenBackfillDrawer")
    assert "wbtLoadBackfillRules()" in open_fn, "规则列表必须在打开抽屉时懒加载"


def test_backfill_rule_list_uses_set_state_and_max_guard(v3_html: str):
    """复选列表：状态独立于 DOM 过滤；勾选第 6 条当场拦住并提示。"""
    render = _extract_js_function(v3_html, "wbtRenderBackfillRules")
    assert "data-wb-bf-rule" in render, "规则行必须是复选框"
    assert "wbt.bfChecked" in render, "选中状态必须以 Set 为准（检索过滤不丢已选）"
    assert "wbTrackBfRuleSearch" in render, "必须支持检索过滤"
    assert "WBT_BF_MAX_RULES" in render and "toast" in render, \
        "勾选超出上限必须当场拦截并提示，不能等提交时 400"
    assert "x.checked = false" in render


def test_backfill_rule_state_visible_in_three_places(v3_html: str):
    """已选状态三处同步（chips/计数/折叠标题）：任何时候都看得懂是「全部」还是「指定」。"""
    state = _extract_js_function(v3_html, "wbtRenderBackfillRuleState")
    for el in ("wbTrackBfRuleCount", "wbTrackBfRuleChips", "wbTrackBfRuleSummary"):
        assert el in state, "已选状态必须同步到 %s" % el
    assert "已选 " in state and "规则：" in state
    # 清空按钮要把三处一起复位
    bind = _extract_js_function(v3_html, "wbtBind")
    assert "wbTrackBfRuleClear" in bind and "wbt.bfChecked.clear" in bind


def test_backfill_submit_sends_rule_ids_only_when_picked(v3_html: str):
    """选中规则 → payload 带 rule_ids；未选 → 保持整周回填（不带该字段）。"""
    picker = _extract_js_function(v3_html, "wbtBackfillPickedRules")
    assert "wbt.bfChecked" in picker, "已选必须取自 Set 状态"
    bind = _extract_js_function(v3_html, "wbtBind")
    assert "rule_ids: picked" in bind, "选中规则必须随 payload 提交"
    assert "WBT_BF_MAX_RULES" in bind, "前端必须先挡超出条数（后端上限 5）"
    # 批量补算（锚点是最近发布周）不支持指定规则：前端要说清而不是白提交
    assert "批量补算不支持指定规则" in bind
    assert "WBT_BF_MAX_RULES = 5" in v3_html, "前端上限必须与后端 MAX_SUBSET_RULES 一致"


def test_backfill_submit_closes_drawer_and_shows_pill(v3_html: str):
    """提交成功后抽屉自动收起，任务进度改由右下角浮标常驻显示。

    2026-09-17 用户反馈：点「开始补算」后抽屉不关，toast（z-index 300）被抽屉
    （z-index 1001）盖住，用户"不知道到底有没有进行这个任务"。
    """
    run = _extract_js_function(v3_html, "wbtRunBackfill")
    assert "wbtCloseBackfillDrawer()" in run, "提交成功后必须自动收起抽屉"
    assert run.index("wbtCloseBackfillDrawer()") < run.index("toast("), \
        "先收抽屉再弹 toast，否则提示仍被抽屉盖住"
    assert "wbtPollBackfill()" in run, "提交后必须继续轮询任务状态"
    # 失败路径必须留着抽屉：错误原因写在抽屉底部（别让用户去翻日志）
    assert "wbtSetBackfillError" in run
    assert "wbtCloseBackfillDrawer()" not in run.split("catch", 1)[1], \
        "提交失败不得关抽屉——抽屉里才有失败原因"

    # 浮标：DOM + 高于抽屉的层级 + 由轮询驱动 + 点击回看
    assert 'id="wbTrackBfPill"' in v3_html, "缺少任务浮标元素"
    css = re.search(r"#wbTrackBfPill \{(.*?)\}", v3_html, re.S)
    assert css, "缺少浮标样式"
    z = re.search(r"z-index:\s*(\d+)", css.group(1))
    drawer_z = re.search(r"#wbTrackBfDrawer \{.*?z-index:\s*(\d+)", v3_html, re.S)
    assert z and drawer_z and int(z.group(1)) > int(drawer_z.group(1)), \
        "浮标层级必须高于抽屉，否则收起后被遮挡"
    jobs = _extract_js_function(v3_html, "wbtRenderBackfillJobs")
    assert "wbtUpdateBackfillPill(" in jobs, "浮标必须由任务状态轮询驱动"
    pill = _extract_js_function(v3_html, "wbtUpdateBackfillPill")
    assert "bfDrawerOpen" in pill, "抽屉打开时浮标必须让位（否则压住底部按钮）"
    assert "queued" in pill and "running" in pill, "运行中状态判定缺失"
    assert "bfPillRunningJob" in pill, "只对亲眼看着跑完的任务报完成"
    bind = _extract_js_function(v3_html, "wbtBind")
    assert "wbTrackBfPill" in bind and "wbtOpenBackfillDrawer" in bind, \
        "点浮标必须能回到抽屉看明细"
    close_fn = _extract_js_function(v3_html, "wbtCloseBackfillDrawer")
    assert "wbtUpdateBackfillPill(" in close_fn, "关抽屉后浮标要接棒显示进度"


def test_subset_scope_is_labeled_in_all_three_levels(v3_html: str):
    """子集周（部分名单）在 L0/L1/L2 都必须标注——否则被读成当周全量结果。

    L2 的标注自 2026-09-17 起从常驻警示框改为「指定规则补算」标签 + 悬停全文
    （用户嫌黄框碍眼）；标注本身仍必须存在，不能变成"看不出来"。
    """
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "subset_weeks" in overview and "指定规则补算" in overview, "L0 缺少子集周标记"
    weeks = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    assert 'rules_scope === "subset"' in weeks, "L1 周行未标注指定规则补算"
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "isSubsetScope" in detail and "scope_notice" in detail, \
        "L2 必须渲染后端下发的 scope_notice 全文"
    assert 'wb-warn">指定规则补算' not in detail, "子集提示不得再作为常驻警示框渲染"
    assert 'title="\' + subsetTip' in detail, "子集口径全文必须挂在标签悬停说明上"
    # 任务列表里也要能看出是"指定规则"任务（措辞收在共用目标文案里）
    jobs = _extract_js_function(v3_html, "wbtRenderBackfillJobs")
    assert "wbtBackfillJobTarget(" in jobs, "任务行必须用共用的目标文案"
    target_fn = _extract_js_function(v3_html, "wbtBackfillJobTarget")
    assert "rule_ids" in target_fn and "指定" in target_fn, "任务文案必须区分指定规则/全部规则"


def test_subset_notice_text_from_backend_only(v3_html: str):
    """提示文案单一来源在后端：前端只渲染 scope_notice，不另写一套措辞。"""
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "esc(j.scope_notice)" in detail, "必须直接渲染后端 scope_notice"
    assert "不代表这些规则当周没有入选" not in v3_html, \
        "前端不得复制后端文案（两处措辞会漂移）"


def test_l0_v11_redesign_kpis_sparkline_and_exec_excess(v3_html: str):
    """L0 首页 V1.1 重构：4 KPI 容器、Sparkline SVG 函数、exec 超额展示。"""
    assert 'id="wbTrackL0Kpis"' in v3_html, "缺少 L0 KPI 容器"
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "wbtRenderL0Kpis(" in overview, "渲染总览必须调用 KPI 渲染"
    assert "weekly_equal_mean_excess_exec" in overview, "L0 必须展示 exec 超额"
    assert "wbtSparklineSvg(r.trend_weeks)" in overview, "L0 表格必须调用原生 Sparkline"
    spark = _extract_js_function(v3_html, "wbtSparklineSvg")
    assert "<svg" in spark and "polyline" in spark, "Sparkline 必须输出原生 SVG"
    kpi_fn = _extract_js_function(v3_html, "wbtRenderL0Kpis")
    for need in ("跟踪策略", "最新信号周", "累计入选", "已结算"):
        assert need in kpi_fn, f"L0 4 卡缺少「{need}」"


def test_l1_v11_kpis_and_pagination(v3_html: str):
    """L1 规则周级历史页 V1.1 重构：5 KPI 汇总卡、分页与口径。"""
    assert 'id="wbTrackL1Summary"' in v3_html, "缺少 L1 汇总卡容器"
    assert 'id="wbTrackL1Pagination"' in v3_html, "缺少 L1 分页容器"
    kpi_fn = _extract_js_function(v3_html, "wbtRenderL1Summary")
    for need in ("跟踪周", "已结算", "胜率(首日开盘)", "平均收益(首日开盘)", "平均超额(首日开盘)"):
        assert need in kpi_fn, f"L1 汇总卡缺少「{need}」"
    weeks = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    assert "mean_excess_exec" in weeks, "L1 表格必须包含平均超额(首日开盘)"
    assert "wbt.l1Page" in weeks or "wbtTrackState.l1Page" in weeks, "L1 表格必须支持分页"


def test_l2_v11_kpis_and_coverage_warning(v3_html: str):
    """L2 周级个股明细 V1.1 重构：5 KPI 汇总卡、完整性警示横幅与逐日收益。"""
    assert 'id="wbTrackL2Kpis"' in v3_html, "缺少 L2 KPI 容器"
    assert 'id="wbTrackL2Banners"' in v3_html, "缺少 L2 警示横幅容器"
    kpi_fn = _extract_js_function(v3_html, "wbtRenderL2Kpis")
    for need in ("入选股票", "有效样本", "胜率(首日开盘)", "平均收益(首日开盘)", "平均超额(首日开盘)"):
        assert need in kpi_fn, f"L2 汇总卡缺少「{need}」"
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "wbtRenderL2Kpis(" in detail, "渲染周明细必须调用 KPI 渲染"
    assert "0.90" in detail, "必须检查 90% 覆盖率门槛"
    assert "数据完整性警示" in detail, "低覆盖率必须展示数据完整性警示横幅"






# ---------------------------------------------------------------------------
# 周卦异步补齐（2026-09-16 性能整改 + 用户复核回归）
# ---------------------------------------------------------------------------


def test_week_gua_sort_uses_same_source_as_display(v3_html: str):
    """排序与显示必须同源：异步补齐的结果在 wbt.baguaByCode 里，直接读
    r.week_gua（首屏占位是空串）会得到「显示有卦、排序值为空」。"""
    sort_fn = _extract_js_function(v3_html, "wbtL2SortValue")
    assert "wbtRowBaguaPart" in sort_fn or "wbtRowBagua" in sort_fn, (
        "周卦排序必须走与显示同一个取值函数"
    )
    assert "r.week_gua" not in sort_fn, "不得直接读原始行的 week_gua（首屏为空）"
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "wbtRowBaguaPart(r, \"week\")" in detail, "周卦列的取值来源必须与排序一致"


def test_bagua_retry_covers_month_failure(v3_html: str):
    """月卦失败也要能被重试：重试集合必须包含 month_state=error 的票。"""
    retry_fn = _extract_js_function(v3_html, "wbtBaguaRetryCodes")
    assert "month_state" in retry_fn, "月卦失败的票必须纳入重试范围"
    fill = _extract_js_function(v3_html, "wbtLoadWeekBagua")
    assert "month_state" in fill, "补齐结果必须记录月卦状态"
    assert "fingerprint" in fill, "补齐请求必须带上与明细一致的版本指纹"


def test_leave_l2_keeps_filled_results(v3_html: str):
    """离开 L2 只中断在途请求、保留已补结果；换周才清空（否则"续传"名不副实）。"""
    level_fn = _extract_js_function(v3_html, "wbtShowLevel")
    assert "wbtAbortBagua()" in level_fn, "离开 L2 应中断而不清空"
    assert "wbtResetBagua()" not in level_fn, "离开 L2 不得清空已补结果"
    abort_fn = _extract_js_function(v3_html, "wbtAbortBagua")
    assert "baguaByCode" not in abort_fn, "中断函数不得清结果"
    reset_fn = _extract_js_function(v3_html, "wbtResetBagua")
    assert "baguaByCode" in reset_fn, "换周必须清结果（结果按代码存，跨周会串）"
    resume_fn = _extract_js_function(v3_html, "wbtResumeBagua")
    assert "wbtBaguaRetryCodes()" in resume_fn, "回到 L2 只续传缺的那部分"


def test_week_identity_passed_to_l2(v3_html: str):
    """规则版本身份：L0/L1 都要把 fingerprint（必要时用本周 recorded_rule_id）
    传给明细接口，否则 canonical id 在本周快照缺失时会「有名单却显示没数据」。"""
    open_week = _extract_js_function(v3_html, "wbtOpenWeek")
    assert "weekFingerprint" in open_week and "fingerprint=" in open_week
    assert "j.fingerprint" in open_week, "后端解析出的指纹要回写，供补齐复用"
    l1 = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    assert "recorded_rule_id" in l1, "L1 周行应优先用该周实际记录的 rule_id"
    assert "data-wbt-week-fp" in l1
    overview = _extract_js_function(v3_html, "wbtRenderOverview")
    assert "data-wbt-week-fp" in overview, "L0 直达也要带指纹"


def test_coverage_scopes_are_labeled(v3_html: str):
    """覆盖率有两种范围，页面必须标清楚（用户 2026-09-16 复核：391/391=100% 是
    本组口径，而 99.95% 来自整周产物的 coverage 字段，不标会混读）。"""
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "整周全部规则" in detail, "整周产物的覆盖率必须标出范围"
    assert "当前规则名单" in detail, "本组口径的覆盖率/横幅必须标出范围"
    assert "j.coverage" in detail and "ui_summary" in detail


def test_version_conflict_and_identity_are_surfaced(v3_html: str):
    """版本不符 / 版本无匹配 / 同公式兄弟不一致都要在页面上说清楚。"""
    l1 = _extract_js_function(v3_html, "wbtRenderRuleWeeks")
    assert "version_conflict" in l1, "L1 版本不符周必须标注"
    assert "版本不符" in l1
    detail = _extract_js_function(v3_html, "wbtRenderWeekDetail")
    assert "param_unmatched" in detail, "L2 版本无匹配必须给出提示而不是空态"
    assert "规则版本不匹配" in detail
    assert "sibling_divergence" in detail, "同公式产物不一致必须暴露"
    assert "同公式产物不一致" in detail


def test_l2_expanded_layout_order_and_trend_chart(v3_html: str):
    """验证个股详情展开布局顺序与累计收益走势图规范：
    1. 顺序：每日收益卡片 -> 覆盖率说明 -> 走势图 -> 周卦/月卦
    2. 走势图包含 0% 零线、真实数据点直线连线、末点标注、隔离 Tooltip
    3. 标题明确标明计算口径基准
    """
    row_fn = _extract_js_function(v3_html, "wbtRowDetailHtml")
    assert "wbt-daily-grid" in row_fn, "必须包含每日收益卡片区域"
    assert "wbt-l2-cov-footer" in row_fn, "必须包含覆盖率说明"
    assert "trendChartHtml" in row_fn, "必须包含走势图 HTML"
    assert "baguaHtml" in row_fn, "必须包含周卦月卦卡片"

    # 验证最终拼接渲染顺序：每日收益 -> 覆盖率 -> 走势图 -> 卦象
    ret_block = row_fn[row_fn.index("return '<div class=\"wbt-l2-expand-wrap\">"):]
    pos_cards = ret_block.index("wbt-daily-grid")
    pos_cov = ret_block.index("wbt-l2-cov-footer")
    pos_chart = ret_block.index("+ trendChartHtml")
    pos_bagua = ret_block.index("+ baguaHtml")
    assert pos_cards < pos_cov < pos_chart < pos_bagua, (
        f"拼接顺序错误: 期望 cards({pos_cards}) < cov({pos_cov}) < chart({pos_chart}) < bagua({pos_bagua})"
    )

    # 验证走势图实现函数规范
    chart_fn = _extract_js_function(v3_html, "wbtRowTrendChartHtml")
    assert "wbt-l2-chart-wrap" in chart_fn, "走势图必须有专属样式外层"
    assert "wbt-trend-svg" in chart_fn, "走势图必须使用 SVG 渲染"
    assert "0.00%" in chart_fn, "走势图必须标明 0% 零线参考"
    assert "stroke-dasharray" in chart_fn, "零线或辅助线必须有虚线样式"
    assert "polyline" in chart_fn, "数据点必须使用直线 polyline 连接（避免平滑假极值）"
    assert "lastPt" in chart_fn or "lastLabelHtml" in chart_fn, "必须包含末点收益率标注"
    assert "wbt-trend-tooltip" in chart_fn, "必须包含隔离的 Tooltip 骨架"
    assert "segments" in chart_fn, "必须拆分连续交易日段（缺失交易日断开）"

    # CSS 样式验证
    assert ".wbt-l2-chart-wrap" in v3_html
    assert ".wbt-trend-tooltip" in v3_html



def test_backfill_submit_error_is_shown_inline(v3_html: str):
    """补算提交失败的原因必须留在抽屉里（不只是一闪而过的 toast）。

    用户 2026-09-16 反馈：选了一周提交只看到 400，原因得去翻服务端日志。
    """
    assert 'id="wbTrackBfSubmitError"' in v3_html, "抽屉里缺少失败原因展示位"
    fn = _extract_js_function(v3_html, "wbtRunBackfill")
    assert "wbtSetBackfillError(" in fn, "提交失败必须写入内联展示位"
    setter = _extract_js_function(v3_html, "wbtSetBackfillError")
    assert "wbTrackBfSubmitError" in setter
    assert "box.hidden" in setter, "无错误时要收起（不常驻占位）"


def test_backfill_drawer_shows_published_weeks_upfront(v3_html: str):
    """补算抽屉必须提前显示"哪些周已有发布名单"，并在输入已发布周时立刻给原因。

    用户 2026-09-16：提交后才吃 400，且看不懂报错里提到的规则名（那是占住该周的
    规则，不是他选的规则）。护栏是按周判定的，必须让用户输入前就看得见。
    """
    assert 'id="wbTrackBfPublishedHint"' in v3_html, "缺少已发布周清单展示位"
    assert 'id="wbTrackBfWeekWarn"' in v3_html, "缺少输入已发布周时的即时说明位"
    loader = _extract_js_function(v3_html, "wbtLoadPublishedWeeks")
    assert "/api/v1/bagua/track/published-weeks" in loader
    warn = _extract_js_function(v3_html, "wbtUpdateBackfillWeekWarn")
    assert "scoped_rule_ids" in warn, "要说明该周名单里已有哪些规则"
    # 口径：补算单位是周 × 规则 → 已有名单的周应提示"可追加"，而不是"不能用"
    assert "追加进这一周已有的名单" in warn, "必须说明会追加而不是替换整周"
    assert "无需补算" in warn, "规则已在名单里时要提示无需补算"
    opener = _extract_js_function(v3_html, "wbtOpenBackfillDrawer")
    assert "wbtLoadPublishedWeeks()" in opener, "打开抽屉就应加载清单"
