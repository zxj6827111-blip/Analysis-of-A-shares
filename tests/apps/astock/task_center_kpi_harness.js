/**
 * Execute the task-center status classifier shipped in index_v3.html.
 * Usage: node task_center_kpi_harness.js <path-to-index_v3.html>
 */
const fs = require("fs");

const path = process.argv[2];
if (!path) {
  console.error("usage: node task_center_kpi_harness.js <index_v3.html>");
  process.exit(2);
}

const html = fs.readFileSync(path, "utf8");
const startMark = "/* TASK_STATUS_SUMMARY_START */";
const endMark = "/* TASK_STATUS_SUMMARY_END */";
const start = html.indexOf(startMark);
const end = html.indexOf(endMark);
if (start < 0 || end <= start) {
  console.error("FAIL task status summary markers missing");
  process.exit(1);
}

const source = html.slice(start + startMark.length, end);
// eslint-disable-next-line no-eval
eval(source);

const tasks = [];
for (let i = 0; i < 7; i++) {
  tasks.push({ kind: "backtest", status: "ok" });
}
for (let i = 0; i < 6; i++) {
  tasks.push({ kind: "backtest", status: "unsupported_corporate_action" });
}

const stats = summarizeTaskStatuses(tasks);
const expected = {
  running: 0,
  succeeded: 7,
  attention: 6,
  cancelled: 0,
  ended: 13,
  backtests: 13,
  experiments: 0,
};
for (const [key, value] of Object.entries(expected)) {
  if (stats[key] !== value) {
    console.error("FAIL", key, "expected", value, "got", stats[key]);
    process.exit(1);
  }
}

const mixed = summarizeTaskStatuses([
  { kind: "job", status: "running" },
  { kind: "experiment", status: "completed" },
  { kind: "experiment", status: "cancelled" },
  { kind: "backtest", status: "research_unconfirmed_formula" },
  { kind: "backtest", status: "no_go" },
  { kind: "experiment", status: "draft" },
]);
if (
  mixed.running !== 1 ||
  mixed.succeeded !== 2 ||
  mixed.attention !== 1 ||
  mixed.cancelled !== 1 ||
  mixed.ended !== 4 ||
  mixed.backtests !== 2 ||
  mixed.experiments !== 3
) {
  console.error("FAIL mixed status summary", JSON.stringify(mixed));
  process.exit(1);
}

console.log("PASS task-center KPI summary");
