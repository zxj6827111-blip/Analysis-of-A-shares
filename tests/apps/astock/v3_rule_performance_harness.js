/**
 * Independent verification harness: load the real index_v3.html, extract the
 * shipped rule-performance functions, and exercise the frontend contracts:
 *  - renderRulePerformancePanel: backend JSON never renders "加载失败"
 *  - auto-refresh guard: drawer closed => no loadRulePerformance / timer cleared
 *  - runRuleBenchmark: native MIN60 toasts and never POSTs
 *
 * Usage:
 *   node v3_rule_performance_harness.js <index_v3.html> [payloads.json]
 *
 * payloads.json: [{ name, data, expect: [substr...], forbid: [substr...] }, ...]
 * With no payloads file a built-in contract matrix is executed.
 */
const fs = require("fs");

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error("usage: node v3_rule_performance_harness.js <index_v3.html> [payloads.json]");
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, "utf8");

function extractFunction(src, name) {
  const re = new RegExp("(?:async\\s+)?function\\s+" + name + "\\s*\\(");
  const m = re.exec(src);
  if (!m) throw new Error("function not found: " + name);
  const start = m.index;
  const brace = src.indexOf("{", m.index);
  let depth = 0;
  for (let j = brace; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error("unbalanced " + name);
}

const FUNCS = [
  "renderRulePerformancePanel",
  "renderRuleDetail",
  "runRuleBenchmark",
  "_validityLabel",
  "_validityCls",
  "_benchMetaHtml",
  "_bindRulePerfEmptyActions",
  "_bindRulePerfActions",
  "_fmtRulePct",
  "_fmtRuleNum",
  "_fmtRuleMdd",
  "_buildEquityPolyline",
  "_renderRulePerfChart",
  "formatTaskTime",
  "esc",
];

let code = "";
for (const name of FUNCS) code += extractFunction(html, name) + "\n";

// --- minimal DOM + globals the extracted functions touch (no browser) ---
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = { _html: "", onclick: null, textContent: "", style: {} };
    Object.defineProperty(elements[id], "innerHTML", {
      get() { return this._html; },
      set(v) { this._html = String(v); },
    });
  }
  return elements[id];
}

// spies shared with the eval'd stubs
const spies = {
  toasts: [],
  apiCalls: [],
  loadCalls: [],
  clearedTimers: [],
  timers: {},
  timerSeq: 0,
};

let VISUAL_TEST = false;
let AppState = { activeRule: null, ruleDrawerOpen: false };
let __rulePerfReqToken = 0;

function $(id) { return el(id); }
function toast(msg) { spies.toasts.push(String(msg)); }
function switchModule() {}
function goTaskDetail() {}
function confirm() { return true; }
function alert() {}
function api(path, opts) {
  spies.apiCalls.push({ path: String(path), opts: opts || {} });
  if (String(path).indexOf("/benchmark-profile") >= 0) {
    return Promise.resolve({ profile: {} });
  }
  return Promise.resolve({ job_id: "job_spy" });
}
// Timers are recorded, never fired: keeps the harness synchronous/deterministic.
function setTimeout(fn) { spies.timerSeq += 1; spies.timers[spies.timerSeq] = fn; return spies.timerSeq; }
function clearTimeout(id) { spies.clearedTimers.push(id); }
function loadRulePerformance(rule, opts) {
  spies.loadCalls.push({ rule: rule, opts: opts || {} });
  return Promise.resolve();
}
function _demoRulePerformance() { throw new Error("demo path not expected"); }

// eslint-disable-next-line no-eval
eval(code);

function render(data) {
  el("rulePerfPanel")._html = "";
  renderRulePerformancePanel({ id: "user_probe", name: "探针规则" }, { data: data });
  return el("rulePerfPanel")._html;
}

const EQUITY = [
  { date: 20260101, cash: 1.0, market_value: 0.0, equity: 1.0 },
  { date: 20260102, cash: 1.0, market_value: 0.1, equity: 1.1 },
  { date: 20260103, cash: 1.0, market_value: 0.2, equity: 1.2 },
];
const METRICS = {
  total_return: 0.2, annual_return: 0.15, max_drawdown: -0.08,
  win_rate: 0.55, sharpe: 1.3, n_round_trips: 42,
};

const builtin = [
  {
    name: "untested-empty",
    data: { has_performance: false, validity: "untested" },
    expect: ["暂无基准回测数据", "尚未测试"],
    forbid: ["加载失败", "测试失败"],
  },
  {
    name: "has-performance-false-only",
    data: { has_performance: false },
    expect: ["暂无基准回测数据"],
    forbid: ["加载失败"],
  },
  {
    name: "has-performance-true-validity-untested",
    data: { has_performance: true, validity: "untested" },
    expect: ["暂无基准回测数据"],
    forbid: ["加载失败"],
  },
  {
    name: "valid-full-payload",
    data: {
      has_performance: true, validity: "valid", metrics: METRICS, equity: EQUITY,
      benchmark_profile: {
        profile_id: "rule_benchmark_v1", universe_label: "全市场 A股",
        codes: ["ALL"], period: "DAY", start: 20180101, end: 20260731,
        account_mode: "portfolio", entry_lag: 1, hold: 1, buy_on: "open",
        sell_on: "close", data_quality: "全市场 · 最新就绪数据",
      },
      source_run_id: "bt_verify_1", updated_at: 1770000000, demo: false,
      show_metrics: true, show_chart: true,
    },
    expect: ["有效绩效", "胜率", "年化收益", "最大回撤", "polyline", "查看完整回测"],
    forbid: ["加载失败", "测试失败"],
  },
  {
    name: "custom-codes-meta",
    data: {
      has_performance: true, validity: "valid", metrics: METRICS, equity: EQUITY,
      benchmark_profile: {
        profile_id: "rule_benchmark_v1",
        universe_label: "自定义股票池（2 只）",
        codes: ["SSE.STK.600000", "SZSE.STK.000001"],
        period: "DAY", start: 20180101, end: 20260731,
        account_mode: "portfolio", entry_lag: 1, hold: 1, buy_on: "open",
        sell_on: "close", data_quality: "自定义股票池 · 最新就绪数据（非点时宇宙）",
      },
      source_run_id: "bt_verify_custom", updated_at: 1770000000, demo: false,
      show_metrics: true, show_chart: true,
    },
    expect: ["有效绩效", "自定义股票池（2 只）", "自定义股票池 · 最新就绪数据"],
    forbid: ["加载失败", "测试失败"],
  },
  {
    name: "no-trades",
    data: {
      has_performance: true, validity: "no_trades", metrics: METRICS, equity: [],
      source_run_id: "bt_verify_2", updated_at: 1770000000, demo: false,
      show_metrics: false, show_chart: false, detail: "本次基准测试没有产生有效交易",
    },
    expect: ["零成交", "样本无效"],
    forbid: ["加载失败", '<div class="l">胜率</div>'],
  },
  {
    name: "failed",
    data: {
      has_performance: true, validity: "failed", metrics: {}, equity: [],
      source_run_id: "bt_verify_3", updated_at: 1770000000, demo: false,
      show_metrics: false, show_chart: false,
      message: "基准回测结果不可读", detail: "回测产物缺失或不可读：FileNotFoundError",
    },
    expect: ["测试失败", "回测产物缺失"],
    forbid: ["加载失败"],
  },
  {
    // second-round contract: a queued/running job is NOT a failure; the panel
    // shows 运行中/排队中 with a refresh button.
    name: "pending-running",
    data: {
      has_performance: false, validity: "untested", benchmark_status: "running",
      benchmark_job_id: "job_pending_r",
      message: "基准回测任务运行中（37% · 信号计算中）…",
      updated_at: 1770000000, demo: false, show_metrics: false, show_chart: false,
      metrics: {}, equity: [], benchmark_equity: [],
    },
    expect: ["运行中", "刷新状态", "信号计算中", "37"],
    forbid: ["加载失败", "测试失败"],
  },
  {
    name: "pending-queued",
    data: {
      has_performance: false, validity: "untested", benchmark_status: "queued",
      benchmark_job_id: "job_pending_q",
      message: "基准回测任务排队中（前面还有 4 个任务 · 排队中（前面还有 4 个任务，并行 1/6））",
      updated_at: 1770000000, demo: false, show_metrics: false, show_chart: false,
      metrics: {}, equity: [], benchmark_equity: [],
    },
    expect: ["排队中", "刷新状态", "前面还有 4 个任务"],
    forbid: ["加载失败", "测试失败"],
  },
  {
    name: "proxy-data",
    data: {
      has_performance: true, validity: "proxy_data", metrics: METRICS, equity: EQUITY,
      source_run_id: "bt_verify_4", updated_at: 1770000000, demo: false,
      show_metrics: true, show_chart: true,
      message: "该规则依赖 60 分钟数据，当前以日线研究代理运行，结果仅供研究",
    },
    expect: ["代理数据", "60 分钟"],
    forbid: ["加载失败"],
  },
  {
    name: "insufficient-samples",
    data: {
      has_performance: true, validity: "insufficient_samples", metrics: METRICS,
      equity: EQUITY, source_run_id: "bt_verify_5", updated_at: 1770000000,
      demo: false, show_metrics: true, show_chart: true,
      message: "样本不足：已平仓 5 笔，低于标准基准门槛 30 笔，结果仅供研究",
    },
    expect: ["样本不足", "胜率"],
    forbid: ["加载失败"],
  },
];

let cases = builtin;
if (process.argv[3]) {
  cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
}

let failures = 0;
for (const c of cases) {
  let out;
  try {
    out = render(c.data);
  } catch (e) {
    console.error("FAIL render " + c.name + ": " + (e && e.stack ? e.stack : e));
    failures++;
    continue;
  }
  const missing = (c.expect || []).filter((s) => out.indexOf(s) < 0);
  const forbidden = (c.forbid || []).filter((s) => out.indexOf(s) >= 0);
  if (missing.length || forbidden.length) {
    console.error(
      "FAIL " + c.name + " missing=" + JSON.stringify(missing) +
      " forbidden=" + JSON.stringify(forbidden)
    );
    console.error(out.slice(0, 500));
    failures++;
  } else {
    console.log("PASS js-contract " + c.name);
  }
}

// Sanity: the real error path (state.error) must still render 加载失败, proving
// the check above is meaningful and not vacuously true.
renderRulePerformancePanel({ id: "user_probe" }, { error: "boom" });
if (el("rulePerfPanel")._html.indexOf("加载失败") < 0) {
  console.error("FAIL error-state-marker: state.error did not render 加载失败");
  failures++;
} else {
  console.log("PASS js-contract error-state-marker");
}

// ---------------------------------------------------------------------------
// drawer lifecycle / native-rule scenarios
// ---------------------------------------------------------------------------
function checkScenario(name, condition, detail) {
  if (condition) {
    console.log("PASS js-scenario " + name);
  } else {
    console.error("FAIL js-scenario " + name + (detail ? ": " + detail : ""));
    failures++;
  }
}

const PENDING_RUNNING = {
  has_performance: false, validity: "untested", benchmark_status: "running",
  benchmark_job_id: "job_guard",
  message: "基准回测任务运行中（37% · 信号计算中）…",
  updated_at: 1770000000, demo: false, show_metrics: false, show_chart: false,
  metrics: {}, equity: [], benchmark_equity: [],
};

// 1) auto-refresh must not fire after the drawer is closed
spies.timers = {}; spies.timerSeq = 0; spies.loadCalls.length = 0;
AppState.ruleDrawerOpen = true;
AppState.activeRule = { id: "user_probe" };
render(PENDING_RUNNING);
const guardCb = spies.timers[spies.timerSeq];
AppState.ruleDrawerOpen = false;
AppState.activeRule = null;
guardCb();
checkScenario(
  "drawer-closed-no-autorefresh",
  spies.loadCalls.length === 0,
  "loadRulePerformance called " + spies.loadCalls.length + " times"
);

// 2) auto-refresh fires with silent=true while the drawer stays open
spies.timers = {}; spies.timerSeq = 0; spies.loadCalls.length = 0;
AppState.ruleDrawerOpen = true;
AppState.activeRule = { id: "user_probe" };
render(PENDING_RUNNING);
const openCb = spies.timers[spies.timerSeq];
openCb();
checkScenario(
  "autorefresh-silent-when-open",
  spies.loadCalls.length === 1 &&
    spies.loadCalls[0].opts && spies.loadCalls[0].opts.silent === true,
  JSON.stringify(spies.loadCalls)
);

// 3) renderRuleDetail(null) clears the pending timer and flips ruleDrawerOpen
el("rulePerfPanel").__perfAutoTimer = 4242;
spies.clearedTimers.length = 0;
AppState.ruleDrawerOpen = true;
renderRuleDetail(null);
checkScenario(
  "drawer-close-clears-timer",
  spies.clearedTimers.indexOf(4242) >= 0 &&
    el("rulePerfPanel").__perfAutoTimer === 0 &&
    AppState.ruleDrawerOpen === false,
  "cleared=" + JSON.stringify(spies.clearedTimers) +
    " timer=" + el("rulePerfPanel").__perfAutoTimer +
    " open=" + AppState.ruleDrawerOpen
);

// 4) native MIN60 rule: toast, never POST /benchmark
spies.toasts.length = 0;
spies.apiCalls.length = 0;
runRuleBenchmark({ id: "user_native", min60_native: true });
checkScenario(
  "native-min60-no-post",
  !spies.apiCalls.some((c) => c.path.indexOf("/benchmark") >= 0) &&
    spies.toasts.some((t) => t.indexOf("原生 60 分钟") >= 0),
  "api=" + JSON.stringify(spies.apiCalls) + " toasts=" + JSON.stringify(spies.toasts)
);

// 5) plain rule still issues the POST synchronously (before the poll await)
spies.apiCalls.length = 0;
runRuleBenchmark({ id: "user_plain" });
checkScenario(
  "plain-rule-posts",
  spies.apiCalls.some(
    (c) => c.path.indexOf("/api/v1/rules/user_plain/benchmark") >= 0 && c.opts.method === "POST"
  ),
  JSON.stringify(spies.apiCalls)
);

process.exit(failures ? 1 : 0);
