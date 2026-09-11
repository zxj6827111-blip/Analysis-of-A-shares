/**
 * Independent verification harness (not the coder's script):
 * exercises the shipped signal-week-returns loader race guards and the
 * experiment-center concurrency dropdown sync in index_v3.html.
 *
 * Usage: node v3_signal_returns_race_harness.js <index_v3.html>
 */
const fs = require("fs");

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error("usage: node v3_signal_returns_race_harness.js <index_v3.html>");
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
  "loadTaskSignalReturns",
  "srActiveRunId",
  "srBuildRuleNameMap",
  "srRuleLabel",
  "srPctHtml",
  "renderTaskWeekRet",
  "_demoSignalReturns",
  "btQueueWorkersSyncExp",
  "fmtPctUi",
  "formatNumber",
  "formatPercent",
  "fmtYmd",
  "esc",
];

let code = "";
for (const name of FUNCS) code += extractFunction(html, name) + "\n";

// --- minimal DOM + globals the extracted functions touch -------------------
const elements = {};
function makeEl(id) {
  const obj = { id: id, value: id === "srHorizon" ? "5" : "", textContent: "", _html: "", style: {} };
  Object.defineProperty(obj, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  return obj;
}
function $(id) {
  if (!elements[id]) elements[id] = makeEl(id);
  return elements[id];
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

let pendingCalls = [];
function api(path, opts) {
  const d = deferred();
  pendingCalls.push({ path: String(path), opts: opts || {}, d: d });
  return d.promise;
}

function makeOption(value, attrs) {
  return {
    value: String(value),
    textContent: "",
    parentNode: null,
    _attrs: Object.assign({}, attrs || {}),
    getAttribute(k) { return this._attrs[k] !== undefined ? this._attrs[k] : null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
  };
}
function makeSelect(values) {
  const sel = {
    value: "",
    options: [],
    querySelectorAll(selector) {
      if (/data-bt-current/.test(String(selector))) {
        return this.options.filter((o) => o.getAttribute("data-bt-current") === "1");
      }
      return [];
    },
    appendChild(o) {
      o.parentNode = this;
      this.options.push(o);
      return o;
    },
    removeChild(o) {
      o.parentNode = null;
      const i = this.options.indexOf(o);
      if (i >= 0) this.options.splice(i, 1);
      return o;
    },
  };
  values.forEach((v) => {
    const o = makeOption(v);
    o.parentNode = sel;
    sel.options.push(o);
  });
  return sel;
}

const documentStub = {
  createElement(tag) {
    if (String(tag).toLowerCase() === "option") return makeOption("");
    return { style: {} };
  },
};

// mutable globals mirrored from index_v3.html
let VISUAL_TEST = false;
let AppState = {
  activeTask: null,
  srPayload: null,
  srCacheKey: "",
  srLoading: false,
  srRuleNames: {},
};
let __srReqToken = 0;
let __btQueueWorkersState = { userExpTouched: false };
const DEMO_RULES = [
  { id: "rule_000128", name: "演示规则128" },
  { id: "rule_000125", name: "演示规则125" },
];
const window = { __lastTaskDetail: {} };
const document = documentStub;

// eslint-disable-next-line no-eval
eval(code);

// ---------------------------------------------------------------------------
let failures = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log("PASS race " + name);
  } else {
    console.error("FAIL race " + name + (detail ? ": " + detail : ""));
    failures++;
  }
}
function flush() {
  return new Promise((r) => setImmediate(r));
}
function payloadFor(tag, horizon) {
  return {
    horizon: horizon || 5,
    plane: "qfq",
    plane_meta: { dataset_id: "ds_" + tag },
    reused: false,
    demo: false,
    summary: {
      n_ok: 1, n_pending: 0, n_no_data: 0, mean: 0.1, median: 0.1,
      win_rate: 1, max: 0.1, min: 0.1, p25: 0.1, p75: 0.1,
    },
    rows: [{
      std_code: tag + "0001", name: "票-" + tag, signal_date: "20260714", period: "DAY",
      indicator_ids: ["rule_000128"], entry_close: 10, exit_date: "20260721",
      exit_close: 11, ret: 0.1, status: "ok",
    }],
  };
}
function tableHtml() { return $("taskWeekRetTable").innerHTML; }
function busyHtml() { return tableHtml().indexOf("计算中") >= 0; }
function lastPending() { return pendingCalls[pendingCalls.length - 1]; }
function resetState() {
  AppState.srPayload = null;
  AppState.srCacheKey = "";
  AppState.srLoading = false;
  AppState.srRuleNames = {};
  pendingCalls = [];
}

(async () => {
  // -------------------------------------------------------------------------
  // A) request A in flight -> switch to B -> B must be issued; late A dropped
  // -------------------------------------------------------------------------
  resetState();
  AppState.activeTask = { id: "runA" };
  $("srHorizon").value = "5";
  loadTaskSignalReturns("runA");
  check("switch-B-issues-A", pendingCalls.length === 1 && pendingCalls[0].path.indexOf("/runA/") >= 0, JSON.stringify(pendingCalls.map((c) => c.path)));

  AppState.activeTask = { id: "runB" };
  loadTaskSignalReturns("runB");
  check(
    "switch-B-issues-B",
    pendingCalls.length === 2 &&
      pendingCalls[1].path.indexOf("/runB/") >= 0 &&
      pendingCalls[1].opts.method === undefined,
    JSON.stringify(pendingCalls.map((c) => c.path))
  );
  check("switch-B-loading", AppState.srLoading === true && busyHtml());

  pendingCalls[1].d.resolve(payloadFor("B"));
  await flush();
  check(
    "switch-B-renders-B",
    tableHtml().indexOf("票-B") >= 0 &&
      AppState.srPayload &&
      AppState.srPayload.rows[0].std_code === "B0001" &&
      AppState.srCacheKey === "runB:5",
    tableHtml().slice(0, 200)
  );

  pendingCalls[0].d.resolve(payloadFor("A"));
  await flush();
  check(
    "switch-B-late-A-dropped",
    tableHtml().indexOf("票-B") >= 0 &&
      tableHtml().indexOf("票-A") < 0 &&
      AppState.srPayload.rows[0].std_code === "B0001" &&
      AppState.srCacheKey === "runB:5",
    "table=" + tableHtml().slice(0, 200) + " cache=" + AppState.srCacheKey
  );

  // -------------------------------------------------------------------------
  // B) horizon 5 -> 10: late h=5 response dropped, cache ends at run:10
  // -------------------------------------------------------------------------
  resetState();
  AppState.activeTask = { id: "runH" };
  $("srHorizon").value = "5";
  loadTaskSignalReturns("runH");
  $("srHorizon").value = "10";
  loadTaskSignalReturns("runH", { force: true });
  check("horizon-two-requests", pendingCalls.length === 2, JSON.stringify(pendingCalls.map((c) => c.path)));
  check(
    "horizon-request-paths",
    pendingCalls[0].path.indexOf("horizon=5") >= 0 && pendingCalls[1].path.indexOf("horizon=10") >= 0,
    JSON.stringify(pendingCalls.map((c) => c.path))
  );

  pendingCalls[1].d.resolve(payloadFor("H10", 10));
  await flush();
  check(
    "horizon-10-renders",
    tableHtml().indexOf("票-H10") >= 0 && AppState.srCacheKey === "runH:10",
    tableHtml().slice(0, 160) + " cache=" + AppState.srCacheKey
  );

  pendingCalls[0].d.resolve(payloadFor("H5", 5));
  await flush();
  check(
    "horizon-5-late-dropped",
    tableHtml().indexOf("票-H10") >= 0 &&
      tableHtml().indexOf("票-H5") < 0 &&
      AppState.srCacheKey === "runH:10" &&
      AppState.srPayload.horizon === 10,
    "table=" + tableHtml().slice(0, 160) + " cache=" + AppState.srCacheKey
  );

  // -------------------------------------------------------------------------
  // C) stale exception must not overwrite the newer success render/state
  // -------------------------------------------------------------------------
  resetState();
  AppState.activeTask = { id: "runE" };
  $("srHorizon").value = "5";
  loadTaskSignalReturns("runE");
  $("srHorizon").value = "10";
  loadTaskSignalReturns("runE", { force: true });
  pendingCalls[1].d.resolve(payloadFor("E10", 10));
  await flush();
  pendingCalls[0].d.reject(new Error("stale boom"));
  await flush();
  check(
    "stale-exception-cannot-overwrite",
    tableHtml().indexOf("票-E10") >= 0 &&
      tableHtml().indexOf("加载失败") < 0 &&
      AppState.srPayload.rows[0].std_code === "E100001" &&
      AppState.srCacheKey === "runE:10",
    "table=" + tableHtml().slice(0, 200) + " cache=" + AppState.srCacheKey
  );

  // -------------------------------------------------------------------------
  // D) srLoading stays true while the newer request is pending; old finally
  //    must not flip it false; only the latest request clears it
  // -------------------------------------------------------------------------
  resetState();
  AppState.activeTask = { id: "runL" };
  loadTaskSignalReturns("runL");
  check("loading-after-first", AppState.srLoading === true);
  loadTaskSignalReturns("runL", { force: true });
  check("loading-after-second", AppState.srLoading === true);

  pendingCalls[0].d.resolve(payloadFor("L1"));
  await flush();
  check(
    "old-finally-does-not-clear-loading",
    AppState.srLoading === true && busyHtml(),
    "loading=" + AppState.srLoading + " busy=" + busyHtml()
  );

  pendingCalls[1].d.resolve(payloadFor("L2"));
  await flush();
  check(
    "latest-finally-clears-loading",
    AppState.srLoading === false && tableHtml().indexOf("票-L2") >= 0,
    "loading=" + AppState.srLoading
  );

  // -------------------------------------------------------------------------
  // E) VISUAL_TEST: zero fetch, demo rows rendered, no payload/cache pollution
  // -------------------------------------------------------------------------
  resetState();
  VISUAL_TEST = true;
  AppState.activeTask = { id: "runV" };
  $("srHorizon").value = "7";
  const visualPromise = loadTaskSignalReturns("runV");
  check("visual-zero-fetch", pendingCalls.length === 0, JSON.stringify(pendingCalls.map((c) => c.path)));
  // safety valve: if the VISUAL_TEST branch ever regresses, unblock the
  // network path so the harness fails loudly instead of exiting silently.
  pendingCalls.forEach((c) => c.d.resolve(payloadFor("VNET", 7)));
  await visualPromise.catch(() => {});
  check(
    "visual-demo-rows",
    tableHtml().indexOf("贵州茅台") >= 0 &&
      tableHtml().indexOf("五粮液") >= 0 &&
      tableHtml().indexOf("中国平安") >= 0 &&
      tableHtml().indexOf("宁德时代") >= 0,
    tableHtml().slice(0, 200)
  );
  check("visual-meta-demo", $("srMeta").textContent.indexOf("演示数据") >= 0, $("srMeta").textContent);
  check(
    "visual-no-pollution",
    AppState.srPayload === null && AppState.srCacheKey === "" && AppState.srLoading === false,
    "payload=" + AppState.srPayload + " cacheKey=" + AppState.srCacheKey
  );
  VISUAL_TEST = false;

  // -------------------------------------------------------------------------
  // F) experiment-center concurrency dropdown sync
  // -------------------------------------------------------------------------
  function dynamicValues(sel) {
    return sel.options.filter((o) => o.getAttribute("data-bt-current") === "1").map((o) => o.value);
  }
  function countValue(sel, v) {
    return sel.options.filter((o) => o.value === String(v)).length;
  }

  __btQueueWorkersState.userExpTouched = false;
  let sel = makeSelect([1, 2, 4, 8, 16]);
  elements["expConcurrency"] = sel;

  btQueueWorkersSyncExp(6);
  check(
    "dropdown-adds-dynamic",
    countValue(sel, 6) === 1 && dynamicValues(sel).length === 1 && sel.value === "6",
    JSON.stringify(sel.options.map((o) => o.value))
  );

  // back to a static value: the dynamic item must be cleaned up
  btQueueWorkersSyncExp(4);
  check(
    "dropdown-cleans-dynamic-on-static",
    countValue(sel, 6) === 0 && dynamicValues(sel).length === 0 && sel.value === "4",
    JSON.stringify(sel.options.map((o) => o.value))
  );

  // val equals the dynamic value: every repeated call must remove then re-add
  // exactly one dynamic option (a check-before-remove regression loses it).
  btQueueWorkersSyncExp(6);
  let readdOk = true;
  for (let round = 0; round < 3; round++) {
    btQueueWorkersSyncExp(6);
    if (
      countValue(sel, 6) !== 1 ||
      dynamicValues(sel).join(",") !== "6" ||
      sel.value !== "6" ||
      sel.options.filter((o) => o.value === "6")[0].textContent.indexOf("当前设置") < 0
    ) {
      readdOk = false;
      break;
    }
  }
  check(
    "dropdown-readd-no-duplicate",
    readdOk,
    JSON.stringify(sel.options.map((o) => o.value))
  );

  // switching dynamic 6 -> dynamic 12 leaves no stale 6 behind
  btQueueWorkersSyncExp(12);
  check(
    "dropdown-switch-dynamic",
    countValue(sel, 6) === 0 && countValue(sel, 12) === 1 && dynamicValues(sel).join(",") === "12",
    JSON.stringify(sel.options.map((o) => o.value))
  );

  // user touched the dropdown -> sync must not mutate it
  __btQueueWorkersState.userExpTouched = true;
  btQueueWorkersSyncExp(3);
  check(
    "dropdown-respects-user-touched",
    countValue(sel, 3) === 0 && sel.value === "12",
    JSON.stringify(sel.options.map((o) => o.value)) + " value=" + sel.value
  );

  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error("HARNESS ERROR: " + (e && e.stack ? e.stack : e));
  process.exit(3);
});
