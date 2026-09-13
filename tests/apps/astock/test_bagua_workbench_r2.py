# -*- coding: utf-8 -*-
"""卦象工作台 R2 整改回归（docs/plans/bagua-ux-v1/review-r2-codex.md）。

覆盖 11 项复核问题的可验证层：

- R2-01 跨层：真实 submit_screen_job → _screen_run_job → run_screen，
  只替换数据面解析与 run_weekly_review 计算（不 mock run_screen 整体）。
- R2-08 共用清理：导出/同卦两个入口创建任务都不删 queued/running 记录。
- R2-02/03/04/05/06/07/09/10/11：前端 JS 函数级（node）与静态不变量。
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config

BACKEND_ROOT = Path(__file__).resolve().parents[3]
V3 = BACKEND_ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"


# ===========================================================================
# helpers
# ===========================================================================


def _extract_js_function(src: str, name: str) -> str:
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


@pytest.fixture(scope="module")
def v3_html() -> str:
    assert V3.is_file(), f"missing {V3}"
    return V3.read_text(encoding="utf-8")


class _FakeCtx:
    """筛选任务容器的最小上下文（不触碰真实任务容器）。"""

    def __init__(self, tmp_path: Path):
        self.cfg = get_default_config(storage_root=tmp_path)
        self.bq_screen_jobs: dict = {}
        self.bq_screen_lock = threading.Lock()
        self.bq_screen_queue: "queue.Queue" = queue.Queue(maxsize=5)
        self.bq_screen_worker_started = True


def _patch_screen_chain(monkeypatch, *, summary=None):
    """只替换数据面解析与规则计算；submit/worker/run_screen 走真实实现。"""
    from wtpy.apps.astock.service import indicator_review as ir
    from wtpy.apps.astock.service import screening as sc

    seen = {"calls": 0, "codes": "UNSET"}

    monkeypatch.setattr(
        sc, "resolve_screen_asof",
        lambda cfg, requested=None: (20260911, {"formal_l1_id": "L1_TEST", "max_date": 20260911}),
    )
    monkeypatch.setattr(sc, "_load_calendar", lambda cfg: None)

    def _review(cfg, *, asof=None, rule_ids=None, codes=None, persist=False, on_progress=None):
        seen["calls"] += 1
        seen["codes"] = codes
        return summary or {
            "status": "ok", "universe_size": 100, "missing_count": 0,
            "error_count": 0, "failed_codes": [], "rules": [], "duration_sec": 0.1,
        }

    monkeypatch.setattr(ir, "run_weekly_review", _review)
    return sc, seen


def _run_submitted(sc, ctx, job_id):
    """同步执行一次 worker 的作业体（跳过线程与队列时序）。"""
    params = ctx.bq_screen_jobs[job_id]["params"]
    sc._screen_run_job(ctx, job_id, params)
    return ctx.bq_screen_jobs[job_id]


# ===========================================================================
# R2-01 全市场 / 空范围 / 混合范围（跨层：submit → run_job → run_screen）
# ===========================================================================


def test_r2_01_all_scope_reaches_computation(tmp_path, monkeypatch):
    """scope=all（codes=None）必须原样保存 None 并真的进入计算，不能被当空范围拒绝。"""
    sc, seen = _patch_screen_chain(monkeypatch)
    ctx = _FakeCtx(tmp_path)
    rec = sc.submit_screen_job(
        ctx, rule_ids=["r1"], match_mode="any", asof=None, codes=None, scope="all"
    )
    assert ctx.bq_screen_jobs[rec["job_id"]]["params"]["codes"] is None, "全市场不得写成 []"
    job = _run_submitted(sc, ctx, rec["job_id"])
    assert job["status"] == "done", job.get("error")
    assert seen["calls"] == 1 and seen["codes"] is None
    assert job["scope_summary"] == "全部 A 股"
    assert job["result"]["scope_size"] == 100


def test_r2_01_empty_picked_scope_rejected_before_queue(tmp_path, monkeypatch):
    """指定范围为空：提交即 400，不建任务、不进队列、不触发计算。"""
    sc, seen = _patch_screen_chain(monkeypatch)
    ctx = _FakeCtx(tmp_path)
    for codes in ([], None):
        with pytest.raises(sc.ScreenError) as ei:
            sc.submit_screen_job(
                ctx, rule_ids=["r1"], match_mode="any", asof=None, codes=codes, scope="picked"
            )
        assert "空" in str(ei.value)
    assert ctx.bq_screen_jobs == {}, "被拒绝的提交不得留下任务记录"
    assert ctx.bq_screen_queue.empty(), "被拒绝的提交不得进队列"
    assert seen["calls"] == 0


def test_r2_01_etf_only_rejected_before_queue(tmp_path, monkeypatch):
    """指定范围全是指数/ETF：提交即拒绝，不占用工作线程。"""
    sc, seen = _patch_screen_chain(monkeypatch)
    ctx = _FakeCtx(tmp_path)
    with pytest.raises(sc.ScreenError) as ei:
        sc.submit_screen_job(
            ctx, rule_ids=["r1"], match_mode="any", asof=None,
            codes=["SSE.IDX.000001", "SSE.ETF.510300"], scope="picked",
        )
    assert "指数/ETF" in str(ei.value)
    assert ctx.bq_screen_jobs == {} and seen["calls"] == 0


def test_r2_01_mixed_scope_reports_exclusions(tmp_path, monkeypatch):
    """股票 + 指数 + 脏输入：原始输入原样入队，计算只拿股票，排除明细如实回报。"""
    sc, seen = _patch_screen_chain(monkeypatch)
    ctx = _FakeCtx(tmp_path)
    rec = sc.submit_screen_job(
        ctx, rule_ids=["r1"], match_mode="any", asof=None,
        codes=["SSE.STK.600000", "SSE.IDX.000001", "???"], scope="picked",
    )
    stored = ctx.bq_screen_jobs[rec["job_id"]]["params"]["codes"]
    assert stored == ["SSE.STK.600000", "SSE.IDX.000001", "???"], "原始输入必须保留给排除统计"
    job = _run_submitted(sc, ctx, rec["job_id"])
    r = job["result"]
    assert seen["codes"] == ["SSE.STK.600000"]
    assert r["excluded_count"] == 2
    assert "1 个标的为指数/ETF" in r["excluded_note"]
    assert "1 个输入无法识别" in r["excluded_note"]


def test_r2_01_duplicate_codes_dedup_no_false_exclusion(tmp_path, monkeypatch):
    """重复代码只算一次，不产生虚假的排除数。"""
    sc, seen = _patch_screen_chain(monkeypatch)
    ctx = _FakeCtx(tmp_path)
    rec = sc.submit_screen_job(
        ctx, rule_ids=["r1"], match_mode="any", asof=None,
        codes=["SSE.STK.600000", "SSE.STK.600000", "600000"], scope="picked",
    )
    job = _run_submitted(sc, ctx, rec["job_id"])
    assert seen["codes"] == ["SSE.STK.600000"]
    assert job["result"]["excluded_count"] == 0
    assert job["result"]["excluded_note"] == ""
    assert job["scope_summary"] == "指定 1 只股票"


# ===========================================================================
# R2-08 导出/同卦共用容器的清理策略
# ===========================================================================


def _mixed_container(n_running: int, n_done: int):
    """构造超过保留阈值的混合任务容器（不触碰真实运行任务）。"""
    jobs: dict = {"_seed": None}
    now = time.time()
    for i in range(n_running):
        jobs[f"run{i}"] = {"job_id": f"run{i}", "status": "running", "created_at": now - 1000 + i}
    for i in range(n_done):
        jobs[f"done{i}"] = {"job_id": f"done{i}", "status": "done", "created_at": now - 500 + i}
    return jobs


def test_r2_08_export_entry_keeps_running_jobs():
    from wtpy.apps.astock.api_routes import bagua as bq

    ctx = types.SimpleNamespace(bq_export_jobs=_mixed_container(4, 24))
    removed = bq._bq_trim_export_jobs_locked(ctx)
    assert removed > 0, "超过阈值应清理终态记录"
    left = ctx.bq_export_jobs
    for i in range(4):
        assert f"run{i}" in left, "运行中任务绝不能被清理"
    assert sum(1 for v in left.values() if isinstance(v, dict)) <= bq._BQ_JOBS_TRIM_TO


def test_r2_08_same_gua_entry_uses_same_policy(monkeypatch, tmp_path, v3_html):
    """同卦入口创建任务后，容器里所有 queued/running 记录都保留。"""
    from wtpy.apps.astock.api_routes import bagua as bq

    ctx = types.SimpleNamespace(
        cfg=types.SimpleNamespace(storage_root=tmp_path),
        bq_export_jobs=_mixed_container(3, 22),
        bq_export_lock=threading.Lock(),
    )
    # 不真的起线程跑全市场扫描：替换作业体
    monkeypatch.setattr(bq, "_bq_run_same_gua_job", lambda *a, **k: None)
    monkeypatch.setattr(bq._bq_threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: None))

    bq._bq_start_same_gua_job(
        ctx, code="SSE.STK.600000", date="2026-09-11", period="DAY", adjust="raw",
        scope=None, limit=None,
    )
    left = ctx.bq_export_jobs
    for i in range(3):
        assert f"run{i}" in left, "同卦入口不得清理运行中任务"
    running = [k for k, v in left.items() if isinstance(v, dict) and v.get("status") in ("queued", "running")]
    assert running, "新建的同卦任务应为 queued"


def test_r2_08_only_terminal_records_are_removable():
    from wtpy.apps.astock.api_routes import bagua as bq

    jobs = {"_seed": None}
    now = time.time()
    for i in range(40):
        jobs[f"queued{i}"] = {"status": "queued", "created_at": now - 900 + i}
    ctx = types.SimpleNamespace(bq_export_jobs=jobs)
    assert bq._bq_trim_export_jobs_locked(ctx) == 0, "无终态记录时不应清理任何东西"
    assert len(ctx.bq_export_jobs) == 41


# ===========================================================================
# 前端 JS：R2-02 类型归一化 / R2-03 ISO 周 / R2-04 旧链接 / R2-07 轮询归类
# ===========================================================================


def test_r2_02_wb_sym_type_normalizes_both_namings(v3_html: str):
    fn = _extract_js_function(v3_html, "wbSymType")
    idx = _extract_js_function(v3_html, "wbIsIdxEtf")
    out = _run_node(
        fn + "\n" + idx + "\n"
        + "const cases = [['stock','STK'],['STK','STK'],['index','IDX'],['IDX','IDX'],"
        + "['etf','ETF'],['ETF','ETF'],['',''],['Stock','STK']];"
        + "for (const [i,e] of cases) { const g = wbSymType(i);"
        + " if (g !== e) { console.log('FAIL', i, g, e); process.exit(1); } }"
        + "console.log('OK', wbIsIdxEtf('index'), wbIsIdxEtf('ETF'), wbIsIdxEtf('stock'));"
    )
    assert "OK true true false" in out


def test_r2_02_same_gua_adjust_uses_normalizer(v3_html: str):
    fn = _extract_js_function(v3_html, "wbOpenRelated")
    # 同卦口径必须走归一化判断，不能再按大写 STK 比较
    assert "wbSymType(d.symbol_type) === \"STK\"" in fn
    assert 'toUpperCase() === "STK"' not in fn


def test_r2_03_iso_week_anchors_on_jan4(v3_html: str):
    """2021-W01 必须是 2021-01-10（1 月 4 日锚定），并逐周回代自洽。"""
    f1 = _extract_js_function(v3_html, "wbIsoWeeksInYear")
    f2 = _extract_js_function(v3_html, "wbIsoWeekToSunday")
    script = f1 + "\n" + f2 + "\n" + """
function isoWeekOf(d) {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const ys = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return [t.getUTCFullYear(), Math.ceil((((t - ys) / 86400000) + 1) / 7)];
}
const expect = {"2021-W01":"20210110","2022-W01":"20220109","2023-W01":"20230108",
                "2026-W36":"20260906","2020-W53":"20210103"};
for (const k in expect) {
  const g = wbIsoWeekToSunday(k);
  if (g !== expect[k]) { console.log("FAIL", k, g, expect[k]); process.exit(1); }
}
if (wbIsoWeekToSunday("2021-W53") !== "") { console.log("FAIL 2021-W53 should be invalid"); process.exit(1); }
if (wbIsoWeekToSunday("2026-W99") !== "") { console.log("FAIL 2026-W99 should be invalid"); process.exit(1); }
let bad = 0;
for (let y = 2019; y <= 2026; y++) {
  for (let w = 1; w <= wbIsoWeeksInYear(y); w++) {
    const s = wbIsoWeekToSunday(y + "-W" + String(w).padStart(2, "0"));
    const d = new Date(Date.UTC(+s.slice(0,4), +s.slice(4,6)-1, +s.slice(6,8)));
    const [ry, rw] = isoWeekOf(d);
    if (d.getUTCDay() !== 0 || ry !== y || rw !== w) bad++;
  }
}
if (bad) { console.log("FAIL roundtrip", bad); process.exit(1); }
console.log("OK", wbIsoWeeksInYear(2020), wbIsoWeeksInYear(2021), wbIsoWeeksInYear(2026));
"""
    out = _run_node(script)
    assert "OK 53 52 53" in out


def test_r2_04_workbench_module_maps_to_bagua_query(v3_html: str):
    fn = _extract_js_function(v3_html, "getCurrentModule")
    script = (
        "globalThis.location = { search: '?module=workbench', pathname: '/' };\n"
        + fn + "\n"
        + "const a = getCurrentModule();\n"
        + "location.search = '?module=bagua-query'; const b = getCurrentModule();\n"
        + "location.search = '?module=nonsense'; const c = getCurrentModule();\n"
        + "console.log('OK', a, b, c);\n"
    )
    out = _run_node(script)
    assert "OK bagua-query bagua-query backtest" in out


def test_r2_04_view_exists_for_bagua_query(v3_html: str):
    assert v3_html.count('id="view-bagua-query"') == 1
    assert 'id="view-workbench"' not in v3_html, "旧 workbench 视图已移除，必须由映射兜住"


def test_r2_07_poll_failure_kind(v3_html: str):
    fn = _extract_js_function(v3_html, "wbScreenPollFailureKind")
    script = (
        fn + "\n"
        + "const stale = wbScreenPollFailureKind({ status: 404 });\n"
        + "const net = wbScreenPollFailureKind(Object.assign(new Error('网络请求失败'), { network: true }));\n"
        + "const s5 = wbScreenPollFailureKind({ status: 503 });\n"
        + "console.log('OK', stale, net, s5);\n"
    )
    out = _run_node(script)
    assert "OK stale transient transient" in out


def test_r2_07_poll_keeps_state_and_retries(v3_html: str):
    fn = _extract_js_function(v3_html, "wbPollScreenJob")
    # 404 才 stale；暂时性失败保留状态并安排重试；结果获取失败只重取结果
    assert 'wbScreenPollFailureKind(e) === "stale"' in fn
    assert "wbRenderScreenTransient" in fn
    assert "wbs.pollTimer = setTimeout" in fn
    assert "绝不重建计算任务" in fn


def test_r2_07_options_out_of_order_response_dropped(v3_html: str):
    """选项加载乱序：A 请求在途时切到 B，A 的迟来响应不得覆盖 B 的日期（复核建议 10）。"""
    fn = _extract_js_function(v3_html, "wbRefreshOptions")
    script = (
        "let applied = [];\n"
        "globalThis.wbq = { picked: [], optionsCache: {}, surfaceMax: 0, dateNote: '', price: 'raw' };\n"
        "globalThis.wbIsStk = (it) => it && it.type === 'STK';\n"
        "globalThis.wbApplyOptions = (o) => applied.push(o.tag);\n"
        "globalThis.wbUpdateDateNote = () => {};\n"
        "globalThis.wbRenderSummaries = () => {};\n"
        "const resolvers = {};\n"
        "globalThis.api = (path) => new Promise((res) => { resolvers[path] = res; });\n"
        + fn + "\n"
        + "(async () => {\n"
        + "  wbq.picked = [{ id: 'SSE.STK.600000', type: 'STK' }];\n"
        + "  const p1 = wbRefreshOptions();\n"
        + "  wbq.picked = [{ id: 'SSE.STK.000001', type: 'STK' }];\n"
        + "  const p2 = wbRefreshOptions();\n"
        + "  resolvers['/api/v1/bagua/options?code=SSE.STK.000001&adjust=raw']({ tag: 'B', surface_max_date: 20260911 });\n"
        + "  resolvers['/api/v1/bagua/options?code=SSE.STK.600000&adjust=raw']({ tag: 'A', surface_max_date: 20260101 });\n"
        + "  await p1; await p2;\n"
        + "  console.log('OK', JSON.stringify(applied), wbq.optionsCache['SSE.STK.600000|raw'].tag);\n"
        + "})();\n"
    )
    out = _run_node(script)
    # 只应用了 B；A 仍入缓存（不影响正确性）
    assert 'OK ["B"] A' in out


# ===========================================================================
# R3-01 条件与结果错配 / R3-02 重复提交 / R3-03 候选类型
# ===========================================================================


_FAKE_DOM_PRELUDE = """
const _mkEl = (id) => ({
  id, innerHTML: '', hidden: true, textContent: '', className: '', value: '', checked: false,
  disabled: false, dataset: {}, style: {},
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, dispatchEvent() {}, click() {}, remove() {}, focus() {},
});
globalThis.__els = {};
globalThis.$ = (id) => (globalThis.__els[id] = globalThis.__els[id] || _mkEl(id));
globalThis.esc = (s) => String(s == null ? '' : s);
globalThis.toast = () => {};
globalThis.wbFmtDate = (d) => String(d);
globalThis.WB_TYPE_LABEL = { STK: '股票', IDX: '指数', ETF: 'ETF' };
globalThis.wbSymType = (v) => {
  const s = String(v == null ? '' : v).trim().toUpperCase();
  if (s === 'STK' || s === 'STOCK') return 'STK';
  if (s === 'IDX' || s === 'INDEX') return 'IDX';
  if (s === 'ETF') return 'ETF';
  return s;
};
"""


def test_r3_01_scope_change_invalidates_completed_result(v3_html: str):
    """反例 A：已完成结果后更换范围（如「从查询结果导入」）→ 结果必须过期且不可导出。"""
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbScreenChanged", "wbScreenResultIsStale", "wbRenderScreenOutdated")
    )
    script = (
        _FAKE_DOM_PRELUDE
        + "globalThis.wbs = { condVersion: 1, submitted: { version: 1, summary: '735金叉及趋势 · 指定 2 只（手动输入）' },"
        + " snapshot: { codes: ['SSE.STK.600033'], date: '20260911' }, jobRunning: false };\n"
        + "globalThis.wbRenderScreenChips = () => {};\n"
        + "globalThis.wbRenderSummaries = () => {};\n"
        + fns + "\n"
        + "const box = $('wbSResult'); box.hidden = false;\n"
        + "wbScreenChanged();\n"
        + "console.log('OK', wbs.condVersion, wbs.snapshot === null,"
        + " /结果已过期/.test(box.innerHTML), /导出本次筛选结果|wbSExportBtn/.test(box.innerHTML), /735金叉及趋势/.test(box.innerHTML));\n"
    )
    out = _run_node(script)
    # 版本 +1、快照清空、显示过期、无导出入口、标注旧条件
    assert "OK 2 true true false true" in out


def test_r3_01_late_result_not_shown_as_current(v3_html: str):
    """反例 B：条件已变后在途结果晚到 → 只能显示过期，不能冒充本次结果、不能有导出入口。"""
    fns = "\n".join(
        _extract_js_function(v3_html, n)
        for n in ("wbScreenResultIsStale", "wbRenderScreenOutdated", "wbRenderScreenJob")
    )
    script = (
        _FAKE_DOM_PRELUDE
        + "globalThis.wbs = { condVersion: 2, submitted: { version: 1, summary: '735金叉及趋势 · 指定 1 只（手动输入）' },"
        + " snapshot: null, jobRunning: false };\n"
        + "globalThis.wbFmtDate = (d) => String(d);\n"
        + "globalThis.wbRenderScreenHits = () => 'HITS';\n"
        + "globalThis.wbBindScreenHitButtons = () => {};\n"
        + "globalThis.wbRenderSummaries = () => {};\n"
        + fns + "\n"
        + "const box = $('wbSResult');\n"
        + "wbRenderScreenJob({ status: 'done', result: { status: 'ok', matched_count: 1, asof: 20260911,"
        + " scope_size: 1, evaluated: 1, missing_count: 0, error_count: 0, hits: [{ code: 'SSE.STK.600033' }], rules: [] } });\n"
        + "const html = box.innerHTML;\n"
        + "console.log('OK', /结果已过期/.test(html), /HITS|筛选完成/.test(html), /wbSExportBtn/.test(html), wbs.snapshot === null);\n"
    )
    out = _run_node(script)
    assert "OK true false false true" in out


def test_r3_01_all_condition_changes_bump_version(v3_html: str):
    """条件变更入口（规则/范围/日期/组合/导入/手动/移除）都必须走失效逻辑。"""
    changed = _extract_js_function(v3_html, "wbScreenChanged")
    assert "wbs.condVersion += 1" in changed
    assert "wbs.snapshot = null" in changed
    # 导入范围必须调失效逻辑（R3-01 反例 A 的根因）
    imp = _extract_js_function(v3_html, "wbImportPickedFromQuery")
    assert "wbScreenChanged()" in imp
    bind = _extract_js_function(v3_html, "wbBind")
    for line in ("scopeSel.onchange", "sDate.onchange", "combine.onchange"):
        assert line in bind, line
    assert "wbScreenChanged" in bind


def test_r3_02_export_second_submit_is_blocked(v3_html: str):
    """导出：第一次提交未返回时再次提交不得发出第二个请求（独立 submitting 状态）。"""
    fn = _extract_js_function(v3_html, "wbRunExport")
    script = (
        _FAKE_DOM_PRELUDE
        + "let calls = 0, release;\n"
        + "globalThis.wbe = { submitting: false, scope: 'all' };\n"
        + "globalThis.wbs = { condVersion: 0 };\n"
        + "$('wbEDate').value = '2026-09-11';\n"
        + "globalThis.wbExportSelection = () => ({ valid: true, label: '全市场 A 股 + ETF', codes: null, all: true });\n"
        + "globalThis.wbExportRuleIds = () => [];\n"
        + "globalThis.wbYyyymmdd = () => '20260911';\n"
        + "globalThis.wbRenderSummaries = () => {};\n"
        + "globalThis.wbRememberExportJob = () => {};\n"
        + "globalThis.wbRefreshExportJobs = () => {};\n"
        + "globalThis.api = () => { calls++; return new Promise((r) => { release = () => r({ job_id: 'J1' }); }); };\n"
        + fn + "\n"
        + "(async () => {\n"
        + "  const p1 = wbRunExport();\n"
        + "  const inFlight = wbe.submitting;\n"
        + "  wbRenderSummaries();            // 模拟切页签/条件重绘\n"
        + "  const p2 = wbRunExport();       // 必须被 submitting 挡住\n"
        + "  release();\n"
        + "  await Promise.all([p1, p2]);\n"
        + "  console.log('OK', calls, inFlight, wbe.submitting);\n"
        + "})();\n"
    )
    out = _run_node(script)
    assert "OK 1 true false" in out


def test_r3_02_screen_second_submit_is_blocked(v3_html: str):
    """筛选：提交在途不能重入；submitting 与 jobRunning 分开。"""
    fn = _extract_js_function(v3_html, "wbRunScreen")
    script = (
        _FAKE_DOM_PRELUDE
        + "let calls = 0, release;\n"
        + "globalThis.wbs = { submitting: false, jobRunning: false, condVersion: 3, scope: 'all', pickedCodes: [], rules: [] };\n"
        + "$('wbSDate').value = '2026-09-11';\n"
        + "globalThis.wbCheckedRuleIds = () => ['r1'];\n"
        + "globalThis.wbScreenSummaryText = () => '735金叉及趋势 · 全部 A 股';\n"
        + "globalThis.wbYyyymmdd = () => '20260911';\n"
        + "globalThis.wbRenderSummaries = () => {};\n"
        + "globalThis.wbSaveActiveScreen = () => {};\n"
        + "globalThis.wbPollScreenJob = () => {};\n"
        + "globalThis.api = () => { calls++; return new Promise((r) => { release = () => r({ job_id: 'S1' }); }); };\n"
        + fn + "\n"
        + "(async () => {\n"
        + "  const p1 = wbRunScreen();\n"
        + "  const midSubmitting = wbs.submitting;\n"
        + "  const p2 = wbRunScreen();\n"
        + "  release();\n"
        + "  await Promise.all([p1, p2]);\n"
        + "  console.log('OK', calls, midSubmitting, wbs.submitting, wbs.jobRunning,"
        + " JSON.stringify(wbs.submitted));\n"
        + "})();\n"
    )
    out = _run_node(script)
    # 只发 1 次；提交在途标记为 true，响应后 submitting=false 且 jobRunning=true；
    # 提交快照记录的是提交那一刻的条件版本与摘要
    assert 'OK 1 true false true {"version":3,"summary":"735金叉及趋势 · 全部 A 股"}' in out


def test_r3_02_submit_state_respected_by_summaries(v3_html: str):
    """摘要重绘必须尊重 submitting（导出/筛选两个按钮）。"""
    summ = _extract_js_function(v3_html, "wbRenderSummaries")
    assert "wbe.submitting" in summ and "wbs.submitting" in summ
    e_dis = re.search(r"eBtn\.disabled = ([^;]+);", summ)
    assert e_dis and "wbe.submitting" in e_dis.group(1), "导出按钮 disabled 必须包含 submitting"
    s_dis = re.search(r"sBtn\.disabled = ([^;]+);", summ)
    assert s_dis and "wbs.submitting" in s_dis.group(1), "筛选按钮 disabled 必须包含 submitting"
    assert 'eBtn.textContent = wbe.submitting ? "提交中…"' in summ
    assert 'wbs.submitting ? "提交中…"' in summ


def test_r3_03_screen_candidates_exclude_index_etf(v3_html: str):
    """筛选候选：指数/ETF 不可选（渲染阶段剔除并说明），导出页仍允许。"""
    fn = _extract_js_function(v3_html, "wbManualAdd")
    script = (
        _FAKE_DOM_PRELUDE
        + "globalThis.wbs = { pickedCodes: [], pickedMeta: {}, pickedFrom: '', rules: [] };\n"
        + "globalThis.wbe = { pickedCodes: [], pickedMeta: [], from: '' };\n"
        + "globalThis.wbRenderPickedChips = () => {};\n"
        + "globalThis.wbScreenChanged = () => {};\n"
        + "globalThis.wbRenderSummaries = () => {};\n"
        + "globalThis.wbResolveInputTokens = async () => ({ ok: [], problems: [{ token: '000001', items: ["
        + "{ id: 'SZSE.STK.000001', code: '000001', name: '平安银行', type: 'STK', type_label: '股票' },"
        + "{ id: 'SSE.IDX.000001', code: '000001', name: '上证指数', type: 'IDX', type_label: '指数' }] }] });\n"
        + fn + "\n"
        + "(async () => {\n"
        + "  $('wbSManual').value = '000001';\n"
        + "  await wbManualAdd('screen');\n"
        + "  const cand = $('wbSPickedCand').innerHTML;\n"
        + "  const note = $('wbSPickedInputError').textContent;\n"
        + "  console.log('OK', /平安银行/.test(cand), /上证指数/.test(cand),"
        + " /上证指数（指数）不参与规则筛选/.test(note), wbs.pickedCodes.length);\n"
        + "})();\n"
    )
    out = _run_node(script)
    assert "OK true false true 0" in out
    # 点击分支也有同一道校验（防御，不依赖渲染阶段过滤）
    assert 'if (isScreen && wbSymType(it.type) !== "STK")' in fn
    # 导出页不受影响：非 isScreen 时不做股票过滤
    assert "if (isScreen) {" in fn


def test_r3_01_screened_export_marks_old_conditions(v3_html: str):
    """筛选交接后再改筛选条件：导出范围必须标注为「旧条件」，不静默当成本次筛选。"""
    fn = _extract_js_function(v3_html, "wbExportSelection")
    script = (
        _FAKE_DOM_PRELUDE
        + "globalThis.wbs = { condVersion: 5 };\n"
        + "globalThis.wbe = { scope: 'screened', from: '来自筛选结果',"
        + " screenedSnapshot: { codes: ['SSE.STK.600033'] }, screenedVersion: 4 };\n"
        + fn + "\n"
        + "const stale = wbExportSelection();\n"
        + "wbe.screenedVersion = 5;\n"
        + "const fresh = wbExportSelection();\n"
        + "console.log('OK', stale.label, '||', fresh.label);\n"
    )
    out = _run_node(script)
    assert "OK 来自筛选结果（旧条件） · 1 个标的 || 来自筛选结果 · 1 个标的" in out
    # 交接时记录条件版本
    assert "wbe.screenedVersion = wbs.condVersion" in _extract_js_function(v3_html, "wbRenderScreenJob")


# ===========================================================================
# 前端静态：R2-05 引导 / R2-06 导出规则 / R2-09 导出口径 / R2-10 口径继承 / R2-11 导航
# ===========================================================================


def test_r2_05_boot_no_longer_inits_legacy_page(v3_html: str):
    m = re.search(r"async function boot\(\)\s*\{", v3_html)
    assert m
    brace = v3_html.index("{", m.start())
    depth = 0
    for j in range(brace, len(v3_html)):
        if v3_html[j] == "{":
            depth += 1
        elif v3_html[j] == "}":
            depth -= 1
            if depth == 0:
                boot_src = v3_html[m.start(): j + 1]
                break
    assert "initBaguaQueryPage()" not in boot_src, "旧查询页不再初始化"
    assert "maybeShowBqTour" not in v3_html, "旧自动引导必须移除"
    assert "$(\"wbGuideBtn\")" in v3_html, "新引导由用户主动开启"


def test_r2_05_tour_steps_target_real_controls(v3_html: str):
    steps = re.search(r"const BQ_TOUR_STEPS = \[(.*?)\];", v3_html, re.S).group(1)
    ids = re.findall(r'id:\s*"([^"]+)"', steps)
    assert ids, "引导步骤必须存在"
    for eid in ids:
        assert v3_html.count(f'id="{eid}"') >= 1, f"引导目标不存在: {eid}"
    # 旧控件已退役，不得再出现在引导步骤里
    for retired in ("bqCode", "bqPeriod", "bqAdjust", "bqSearchBtn"):
        assert retired not in ids
    fn = _extract_js_function(v3_html, "startBqTour")
    assert 'e.key === "Escape"' in fn, "引导必须支持键盘退出"


def test_r2_06_export_rules_use_independent_set(v3_html: str):
    ens = _extract_js_function(v3_html, "wbEnsureExportRules")
    ren = _extract_js_function(v3_html, "wbRenderExportRules")
    ids_fn = _extract_js_function(v3_html, "wbExportRuleIds")
    # 勾选独立保存；目录刷新只剔除失效 ID；提交/摘要取同一来源
    assert "wbe.checkedRuleIds" in ens and "live.has(id)" in ens
    assert "wbe.checkedRuleIds.has(r.id)" in ren
    assert "wbe.checkedRuleIds" in ids_fn
    run = _extract_js_function(v3_html, "wbRunExport")
    summ = _extract_js_function(v3_html, "wbRenderSummaries")
    assert "wbExportRuleIds()" in run and "wbExportRuleIds()" in summ
    assert 'input[data-wb-export-rule]:checked' not in v3_html, "不得再从 DOM 读勾选状态"


def test_r2_06_export_rule_labels_show_source(v3_html: str):
    fn = _extract_js_function(v3_html, "wbRuleSourceText")
    assert "r.source" in fn and "r.id" in fn
    assert "wbRuleSourceText(r)" in _extract_js_function(v3_html, "wbRenderExportRules")
    # 规则目录接口提供 source/version
    from wtpy.apps.astock.service.screening import list_screen_rules  # noqa: F401

    src = (BACKEND_ROOT / "wtpy/apps/astock/service/screening.py").read_text(encoding="utf-8")
    assert '"source": r.get("source"' in src
    assert '"version": r.get("version"' in src


def test_r2_09_export_button_uses_successful_codes(v3_html: str):
    fn = _extract_js_function(v3_html, "wbRenderQueryResult")
    assert "snap.okCodes" in fn
    assert "wbq.snapshot.codes" not in fn, "导出范围不得再用全部请求代码"
    run = _extract_js_function(v3_html, "wbRunQuery")
    assert "snap.okCodes = okRows.map" in run
    assert "snap.failCount" in run
    imp = _extract_js_function(v3_html, "wbImportPickedFromQuery")
    assert "snap.okCodes" in imp


def test_r2_10_screen_to_export_carries_adjust(v3_html: str):
    fn = _extract_js_function(v3_html, "wbRenderScreenJob")
    assert '"tushare_qfq"' in fn
    assert 'wbe.sourceSnapshot' in fn
    assert 'ep.value = "tushare_qfq"' in fn
    div = _extract_js_function(v3_html, "wbExportDiverged")
    assert "src.adjust" in div and "src.date" in div


def test_r2_11_mobile_nav_gets_full_row(v3_html: str):
    css = v3_html.split("</style>", 1)[0]
    m = re.search(r"@media\(max-width:980px\)\{(.*?)\n    \}", css, re.S)
    assert m, "missing 980 media block"
    block = m.group(1)
    nav_rule = re.search(r"\.main-nav\{([^}]*)\}", block)
    assert nav_rule, "980 断点必须显式设置 .main-nav"
    rule = nav_rule.group(1)
    # flex:1 会把 flex-basis 归零，必须给 100% basis（或 flex:1 0 100%）
    assert "flex:1 0 100%" in rule or "flex-basis:100%" in rule, rule
    assert "100%" in rule


def test_manual_input_for_screen_and_export(v3_html: str):
    """UX：筛选与指定标的导出都能直接输入/粘贴，不必先查询。"""
    for eid in ("wbSManual", "wbSAddBtn", "wbEManual", "wbEAddBtn"):
        assert v3_html.count(f'id="{eid}"') == 1, eid
    fn = _extract_js_function(v3_html, "wbManualAdd")
    assert "wbResolveInputTokens" in fn
    res = _extract_js_function(v3_html, "wbResolveInputTokens")
    assert "/api/v1/bagua/instruments" in res
    # 筛选只能选 A 股；导出允许指数/ETF
    assert 'wbSymType(it.type) !== "STK"' in fn
    bind = _extract_js_function(v3_html, "wbBind")
    assert 'wbManualAdd("screen")' in bind and 'wbManualAdd("export")' in bind
