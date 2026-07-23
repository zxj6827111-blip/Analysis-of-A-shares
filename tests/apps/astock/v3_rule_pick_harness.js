/**
 * Load real index_v3.html, extract openRulePicker/renderPickList, run open/select/confirm.
 * Usage: node v3_rule_pick_harness.js <path-to-index_v3.html>
 */
const fs = require("fs");
const path = process.argv[2];
if (!path) {
  console.error("usage: node v3_rule_pick_harness.js <index_v3.html>");
  process.exit(2);
}
const html = fs.readFileSync(path, "utf8");

// --- structural: modal not nested in unclosed drawer ---
const gi = html.indexOf('id="guaDrawer"');
const mi = html.indexOf('id="rulePickModal"');
if (!(gi >= 0 && mi > gi)) {
  console.error("FAIL order gi/mi", gi, mi);
  process.exit(1);
}
const between = html.slice(gi, mi);
const opens = (between.match(/<div\b/g) || []).length;
const closes = (between.match(/<\/div>/g) || []).length;
if (opens !== closes) {
  console.error("FAIL nest open=" + opens + " close=" + closes);
  process.exit(1);
}
if (!html.includes("async function openRulePicker")) {
  console.error("FAIL missing openRulePicker");
  process.exit(1);
}
if (!html.includes("AppState.expRuleIds = Array.from(_pickTemp")) {
  console.error("FAIL missing confirm assignment");
  process.exit(1);
}
if (!/rule-pick-modal\.show\{[^}]*display:\s*flex/.test(html)) {
  console.error("FAIL missing show flex css");
  process.exit(1);
}
if (!html.includes("z-index:5000") && !html.includes("z-index: 5000")) {
  console.error("FAIL missing z-index 5000");
  process.exit(1);
}

function extractFunction(src, name) {
  const re = new RegExp("(async\\s+)?function\\s+" + name + "\\s*\\(");
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

function extractConfirmBody(src) {
  let m = src.match(
    /_bindClick\(\s*"btnPickConfirm"\s*,\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*\)\s*;/
  );
  if (!m) {
    m = src.match(
      /\$\(\s*"btnPickConfirm"\s*\)\.onclick\s*=\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*;/
    );
  }
  if (!m) throw new Error("confirm handler not found");
  return m[1];
}

const openFn = extractFunction(html, "openRulePicker");
const renderFn = extractFunction(html, "renderPickList");
const confirmBody = extractConfirmBody(html);

// --- minimal DOM ---
function El(id) {
  this.id = id || "";
  this._cls = new Set();
  this.value = "";
  this._html = "";
  this._checks = [];
  this.attrs = {};
  const self = this;
  this.classList = {
    add(c) {
      self._cls.add(c);
    },
    remove(c) {
      self._cls.delete(c);
    },
    contains(c) {
      return self._cls.has(c);
    },
  };
  this.getAttribute = (k) => self.attrs[k];
  this.setAttribute = (k, v) => {
    self.attrs[k] = String(v);
  };
  this.focus = () => {};
  this.querySelectorAll = (sel) => {
    if (String(sel).includes("checkbox")) return self._checks;
    return [];
  };
  this.addEventListener = () => {};
  Object.defineProperty(this, "innerHTML", {
    get() {
      return self._html;
    },
    set(v) {
      self._html = String(v);
      self._checks = [];
      const re = /value="([^"]+)"/g;
      let m;
      while ((m = re.exec(self._html))) {
        const val = m[1];
        const cb = {
          value: val,
          checked: false,
          addEventListener(type, fn) {
            this._fn = fn;
          },
        };
        self._checks.push(cb);
      }
    },
  });
}

const byId = {};
function ensure(id) {
  if (!byId[id]) byId[id] = new El(id);
  return byId[id];
}
[
  "rulePickModal",
  "rulePickList",
  "rulePickSearch",
  "btnPickRules",
  "btnPickConfirm",
  "btnClosePick",
  "btnPickCancel",
].forEach(ensure);

const AppState = { rules: [], expRuleIds: [] };
let _pickTemp = new Set();
function $(id) {
  return byId[id] || null;
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function toast() {}
function ruleStatus(r) {
  return {
    cls: r.backtestable ? "ok" : "",
    label: r.backtestable ? "可回测" : r.compile_status || "",
  };
}
async function loadRules() {
  AppState.rules = [
    { id: "tn6_735", name: "735", backtestable: true, compile_status: "ready" },
    { id: "user_0707", name: "0707", backtestable: true, compile_status: "ready" },
  ];
  return AppState.rules;
}
function renderExperimentCreate() {
  global.__rendered = true;
}
async function expEstimate() {
  global.__estimated = true;
}

// eslint-disable-next-line no-eval
eval(openFn + "\n" + renderFn + "\nfunction confirmPick(){\n" + confirmBody + "\n}\n");

(async () => {
  AppState.rules = [];
  AppState.expRuleIds = [];
  await openRulePicker();
  if (!$("rulePickModal").classList.contains("show")) {
    console.error("FAIL modal not shown");
    process.exit(1);
  }
  if (!AppState.rules.length) {
    console.error("FAIL rules not loaded");
    process.exit(1);
  }
  const cbs = $("rulePickList").querySelectorAll("input[type=checkbox]");
  if (!cbs.length) {
    console.error("FAIL no checkboxes", $("rulePickList").innerHTML.slice(0, 180));
    process.exit(1);
  }
  cbs[0].checked = true;
  if (cbs[0]._fn) cbs[0]._fn();
  else _pickTemp.add(cbs[0].value);
  if (!_pickTemp.size) {
    console.error("FAIL pickTemp empty");
    process.exit(1);
  }
  confirmPick();
  if (!AppState.expRuleIds.length) {
    console.error("FAIL expRuleIds", AppState.expRuleIds);
    process.exit(1);
  }
  if ($("rulePickModal").classList.contains("show")) {
    console.error("FAIL modal still open");
    process.exit(1);
  }
  console.log("PASS open/select/confirm " + AppState.expRuleIds.join(","));
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
