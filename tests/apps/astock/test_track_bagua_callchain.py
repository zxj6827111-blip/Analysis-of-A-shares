# -*- coding: utf-8 -*-
"""执行调用链测试：L2 周卦补齐的「重进只续传缺的那部分」。

静态断言只能证明单个函数内部没清空结果，证明不了 `wbtOpenWeek → wbtLoadWeekBagua`
这条链上后面又把它清掉（用户 2026-09-16 复核正是这么发现问题的）。本测试把
index_v3.html 里的相关函数抽出来，在 node 里用最小桩件（$ / api / 渲染函数）
**真正执行**这条链，观察实际发出的补齐请求。

环境缺 node 时跳过（pytest.skip），不影响其他环境。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

BACKEND_ROOT = tests.apps.astock.conftest.ROOT
V3 = BACKEND_ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index_v3.html"

#: 需要抽出来真实执行的函数（其余用桩件替换：渲染与 DOM 不是本测试的对象）
FUNCS = (
    "wbtResetBagua",
    "wbtAbortBagua",
    "wbtBaguaCodes",
    "wbtBaguaRetryCodes",
    "wbtRowBagua",
    "wbtRowBaguaPart",
    "wbtBaguaNoteHtml",
    "wbtBaguaBatchMatches",
    "wbtLoadWeekBagua",
    "wbtResumeBagua",
    "wbtSortL2Rows",
    "wbtL2SortValue",
    "wbtShowLevel",
    "wbtOpenWeek",
)

HARNESS = r"""
// ---- 最小桩件：DOM/渲染不是本测试对象 ----
const WBT_BAGUA_CHUNK = __CHUNK__;
const calls = { detail: [], bagua: [] };
let baguaMode = "normal"; // normal | slow
const wbt = {
  loaded: true, token: 0, condVersion: 0, error: "",
  rules: [], windowWeeks: 12,
  sortKey: "win_rate", sortDesc: true, caliber: "exec", search: "",
  level: "l1", ruleId: null, ruleRow: null, weeks: [], week: null,
  weekId: null, weekRule: "", l2From: "l1", ruleFingerprint: "", weekFingerprint: "",
  l2SortKey: "week_ret", l2SortDesc: true, l2Expanded: new Set(),
  baguaToken: 0, baguaAbort: null, baguaByCode: {}, baguaOrder: [],
  baguaTotal: 0, baguaDone: 0, baguaFailed: 0, baguaRunning: false, baguaError: "",
  baguaRevision: "", bfTimer: null, bfHadRunning: false,
  bfDrawerOpen: false, bfMode: "single", l1Page: 1, l1PageSize: 10,
};
const $ = () => null;
const esc = (s) => String(s == null ? "" : s);
const wbtNum = (v) => (v == null || v === "" || isNaN(Number(v)) ? null : Number(v));
const wbtCaliberIsExec = () => true;
function wbtRenderWeekDetail() { calls.render = (calls.render || 0) + 1; }
function wbtShowLevelStub() {}

const DETAIL = () => ({
  ok: true, week_id: "20260911", snapshot_id: "snap1", tracking_revision_id: "rev1",
  bagua_mode: "defer", bagua_total: 2, fingerprint: "fp1", rule_id: "txt_X",
  rows: [
    { code: "A", rule_id: "txt_X", code_disp: "A", name: "甲", week_gua: "", bagua: null, bagua_state: "pending", bagua_month_state: "pending" },
    { code: "B", rule_id: "txt_X", code_disp: "B", name: "乙", week_gua: "", bagua: null, bagua_state: "pending", bagua_month_state: "pending" },
  ],
  pending_picks: [], ui_summary: {}, coverage: null, track_week_dates: [],
});

function item(c) {
  return { code: c, state: "ok", month_state: "ok", week_gua: "乾为天", bagua: { week: { combo: "乾为天" }, month: { combo: "坤为地" } } };
}

// serverReady：服务端这次能返回哪些票；用于制造「A 已补、B 未补」
let serverReady = new Set(["A"]);
async function api(url, opts) {
  if (url.indexOf("/bagua?") >= 0) {
    const codes = decodeURIComponent(url.split("codes=")[1].split("&")[0]).split(",");
    calls.bagua.push({ codes: codes.slice(), snapshot_id: "snap1", fingerprint: (url.match(/fingerprint=([^&]*)/) || [])[1] || null });
    if (baguaMode === "slow") {
      // 模拟在途请求：只有被 abort 才结束（用于验证离开 L2 时的中断）
      await new Promise((resolve, reject) => {
        if (opts && opts.signal) {
          if (opts.signal.aborted) return reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          opts.signal.addEventListener("abort", () =>
            reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
        }
      });
    }
    return {
      ok: true, week_id: "20260911", snapshot_id: "snap1", tracking_revision_id: "rev1",
      fingerprint: "fp1",
      items: codes.filter((c) => serverReady.has(c)).map(item),
    };
  }
  calls.detail.push(url);
  return DETAIL();
}

// ---- 被抽出来的真实实现 ----
__FUNCS__

(async () => {
  const out = {};
  // 1) 首次进入：请求整周（A、B）
  await wbtOpenWeek("20260911", "txt_X", "fp1");
  out.firstRequest = calls.bagua.map((c) => c.codes);
  out.afterFirst = { done: Object.keys(wbt.baguaByCode).length, order: wbt.baguaOrder.slice(), revision: wbt.baguaRevision };

  // 2) 服务端只给出了 A：B 仍缺
  out.missingAfterFirst = wbtBaguaRetryCodes();

  // 3) 离开 L2（在途请求场景）：中断但保留已补结果
  baguaMode = "slow";
  serverReady = new Set(["B"]);
  const pending = wbtLoadWeekBagua(wbtBaguaRetryCodes()); // 不 await：模拟在途
  await new Promise((r) => setTimeout(r, 10));
  out.runningBeforeLeave = wbt.baguaRunning;
  wbtShowLevel("l1");
  await pending.catch(() => {});
  out.afterLeave = {
    running: wbt.baguaRunning,
    kept: Object.keys(wbt.baguaByCode).slice().sort(),
    orderKept: wbt.baguaOrder.slice(),
  };

  // 4) 同周重进：只补缺的 B（不得清空 A、不得重发 A）
  baguaMode = "normal";
  calls.bagua.length = 0;
  await wbtOpenWeek("20260911", "txt_X", "fp1");
  out.reenterRequest = calls.bagua.map((c) => c.codes);
  out.afterReenter = {
    done: Object.keys(wbt.baguaByCode).length,
    allLoaded: Object.keys(wbt.baguaByCode).sort(),
    guaA: (wbtRowBagua({ code: "A" }) || {}).week_gua,
  };

  // 5) 全部补齐后再次重进：不应发任何请求，也不应清空
  calls.bagua.length = 0;
  await wbtOpenWeek("20260911", "txt_X", "fp1");
  out.reenterWhenComplete = calls.bagua.map((c) => c.codes);
  out.doneStillThere = Object.keys(wbt.baguaByCode).length;
  out.baguaError = wbt.baguaError;

  console.log(JSON.stringify(out));
})().catch((e) => {
  console.log(JSON.stringify({ error: String(e && e.stack ? e.stack : e) }));
});
"""


def _extract_js_function(src: str, name: str) -> str:
    """按花括号配平提取整个 JS 函数体（与 test_track_ui 同一实现）。"""
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
def chain_result() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用：调用链执行测试跳过")
    assert V3.is_file(), f"missing {V3}"
    src = V3.read_text(encoding="utf-8")
    m = re.search(r"const WBT_BAGUA_CHUNK = (\d+);", src)
    assert m, "缺少 WBT_BAGUA_CHUNK 定义（前端分批大小）"
    funcs = "\n\n".join(_extract_js_function(src, n) for n in FUNCS)
    script = HARNESS.replace("__CHUNK__", m.group(1)).replace("__FUNCS__", funcs)
    with tempfile.TemporaryDirectory() as td:
        js = Path(td) / "chain.mjs"
        js.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(js)], capture_output=True, text=True, encoding="utf-8", timeout=120
        )
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr[:2000]}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in out, out.get("error")
    return out


def test_first_entry_requests_whole_week(chain_result):
    assert chain_result["firstRequest"] == [["A", "B"]]
    assert chain_result["afterFirst"]["revision"] == "rev1"


def test_missing_codes_detected_after_partial_fill(chain_result):
    assert chain_result["missingAfterFirst"] == ["B"], "只补到 A 时，缺的应当是 B"


def test_leaving_l2_aborts_but_keeps_results(chain_result):
    assert chain_result["runningBeforeLeave"] is True
    after = chain_result["afterLeave"]
    assert after["running"] is False, "离开后不应还在跑"
    assert after["kept"] == ["A"], "已补结果必须保留（不能被清空）"
    assert after["orderKept"], "补齐顺序也要保留，重进才能按同一顺序续传"


def test_reentry_resumes_only_missing(chain_result):
    """核心回归：同周重进只请求缺的票（用户复核发现的实际请求是 A,B）。"""
    assert chain_result["reenterRequest"] == [["B"]], (
        f"重进应只补缺的 B，实际请求：{chain_result['reenterRequest']}"
    )
    after = chain_result["afterReenter"]
    assert after["allLoaded"] == ["A", "B"]
    assert after["guaA"] == "乾为天", "重进后 A 的卦象仍在"


def test_reentry_when_complete_sends_nothing(chain_result):
    """全部补齐后再重进：不发请求、也不清空（避免"重进白重算一遍"）。"""
    assert chain_result["reenterWhenComplete"] == []
    assert chain_result["doneStillThere"] == 2
