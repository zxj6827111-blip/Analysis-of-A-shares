# -*- coding: utf-8 -*-
"""Fix history percentage display and table column layout."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index.html"
RUNS = ROOT / "wtpy" / "apps" / "astock" / "service" / "runs.py"


def patch_runs() -> None:
    t = RUNS.read_text(encoding="utf-8")
    old = """    keys = (
        "total_return",
        "max_drawdown",
        "final_equity",
        "n_buys",
        "n_sells",
        "n_round_trips",
        "win_rate",
        "n_days",
    )
    return {k: metrics[k] for k in keys if k in metrics}
"""
    new = """    keys = (
        "total_return",
        "mean_symbol_return",
        "median_symbol_return",
        "max_drawdown",
        "final_equity",
        "n_buys",
        "n_sells",
        "n_round_trips",
        "win_rate",
        "n_days",
        "account_mode",
        "n_symbol_accounts",
        "capital_base",
        "pct_symbols_profitable",
    )
    return {k: metrics[k] for k in keys if k in metrics}
"""
    if "mean_symbol_return" not in t.split("_metrics_brief")[1][:500]:
        if old not in t:
            raise SystemExit("metrics_brief keys not found")
        RUNS.write_text(t.replace(old, new, 1), encoding="utf-8")
        print("OK runs.py metrics_brief")
    else:
        print("runs already")


def patch_html() -> None:
    t = HTML.read_text(encoding="utf-8")

    # CSS for numeric columns
    old_css = """    .history-table th, .history-table td {
      border-bottom: 1px solid var(--border);
      padding: 0.45rem 0.4rem; text-align: left; vertical-align: top;
    }
"""
    new_css = """    .history-table th, .history-table td {
      border-bottom: 1px solid var(--border);
      padding: 0.45rem 0.5rem; text-align: left; vertical-align: top;
    }
    .history-table th.num, .history-table td.num {
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
      min-width: 4.5rem;
      padding-left: 0.65rem;
    }
    .history-table td.num .pct-pos { color: #3dd68c; }
    .history-table td.num .pct-neg { color: #f07178; }
    .history-table td.num .pct-zero { color: var(--muted); }
    .history-table td.num .pct-note {
      display: block; font-size: 0.72rem; color: var(--muted); font-weight: 400;
      margin-top: 0.1rem;
    }
    .history-wrap { overflow-x: auto; max-width: 100%; }
"""
    if "history-table td.num" not in t:
        if old_css not in t:
            raise SystemExit("history css not found")
        t = t.replace(old_css, new_css, 1)

    # Replace fmtPct with smarter version + helpers
    old_fmt = """    function fmtPct(x) {
      if (x === undefined || x === null || Number.isNaN(Number(x))) return "—";
      return (Number(x) * 100).toFixed(2) + "%";
    }
"""
    new_fmt = """    /** Metrics store fractions (0.05 = 5%). Never treat them as already-percent. */
    function fracToPctNumber(x) {
      const n = Number(x);
      if (x === undefined || x === null || Number.isNaN(n)) return null;
      return n * 100;
    }
    function fmtPct(x, opts) {
      const p = fracToPctNumber(x);
      if (p === null) return "—";
      const abs = Math.abs(p);
      // tiny but non-zero: show more decimals so 0.02% is not mistaken for "broken %"
      let digits = 2;
      if (abs > 0 && abs < 0.1) digits = 4;
      else if (abs > 0 && abs < 1) digits = 3;
      const sign = p > 0 ? "+" : (p < 0 ? "" : "");
      const body = sign + p.toFixed(digits) + "%";
      if (opts && opts.html) {
        const cls = p > 1e-12 ? "pct-pos" : (p < -1e-12 ? "pct-neg" : "pct-zero");
        return "<span class='" + cls + "' title='" + (opts.title || ("原始小数 " + Number(x))) + "'>" + body + "</span>";
      }
      return body;
    }
    function fmtInt(x) {
      if (x === undefined || x === null || Number.isNaN(Number(x))) return "—";
      try { return Number(x).toLocaleString("zh-CN"); } catch (e) { return String(x); }
    }
    function historyReturnDisplay(m) {
      // 通达信对照：优先展示等权平均单票收益（更贴近导出表综合口径）
      const mode = String(m.account_mode || "").toLowerCase();
      const isPer = mode === "per_symbol" || mode === "tdx" || mode === "per_stock";
      if (isPer && m.mean_symbol_return != null && !Number.isNaN(Number(m.mean_symbol_return))) {
        return fmtPct(m.mean_symbol_return, {
          html: true,
          title: "等权平均单票收益率（通达信对照口径）。汇总权益总收益=" + fmtPct(m.total_return)
        }) + "<span class='pct-note'>等权单票</span>";
      }
      return fmtPct(m.total_return, {
        html: true,
        title: "组合/汇总总收益率（小数 " + m.total_return + "）"
      });
    }
"""
    if "fracToPctNumber" not in t:
        if old_fmt not in t:
            raise SystemExit("fmtPct block not found")
        t = t.replace(old_fmt, new_fmt, 1)

    # renderHistory table head + cells
    old_head = """      const head = "<table class='history-table'><thead><tr>"
        + "<th style='width:2rem'><input type='checkbox' class='hist-check' id='histCheckAll' title='全选/取消全选' /></th>"
        + "<th>回测内容</th><th>时间</th><th>状态</th><th>周期</th>"
        + "<th>收益</th><th>回撤</th><th>买入</th><th>胜率</th><th>操作</th>"
        + "</tr></thead><tbody>";
"""
    new_head = """      const head = "<div class='history-wrap'><table class='history-table'><thead><tr>"
        + "<th style='width:2rem'><input type='checkbox' class='hist-check' id='histCheckAll' title='全选/取消全选' /></th>"
        + "<th>回测内容</th><th>时间</th><th>状态</th><th>周期</th>"
        + "<th class='num' title='收益率（小数已×100）。通达信对照显示等权单票收益'>收益</th>"
        + "<th class='num' title='最大回撤（小数已×100）'>回撤</th>"
        + "<th class='num'>买入次数</th>"
        + "<th class='num' title='胜率（小数已×100，如 0.45 → 45%）'>胜率</th>"
        + "<th>操作</th>"
        + "</tr></thead><tbody>";
"""
    if "history-wrap" not in t or "买入次数" not in t:
        if old_head not in t:
            raise SystemExit("history head not found")
        t = t.replace(old_head, new_head, 1)

    old_cells = """          + "<td>" + fmtPct(m.total_return) + "</td>"
          + "<td>" + fmtPct(m.max_drawdown) + "</td>"
          + "<td>" + (m.n_buys != null ? m.n_buys : "—") + "</td>"
          + "<td>" + fmtPct(m.win_rate) + "</td>"
          + "<td><button type='button' class='btn-del' data-del-run='" + (r.run_id || "") + "'>删除</button></td>"
          + "</tr>";
      }).join("");
      box.innerHTML = head + body + "</tbody></table>";
"""
    new_cells = """          + "<td class='num'>" + historyReturnDisplay(m) + "</td>"
          + "<td class='num'>" + fmtPct(m.max_drawdown, { html: true, title: "最大回撤" }) + "</td>"
          + "<td class='num'>" + fmtInt(m.n_buys) + "</td>"
          + "<td class='num'>" + fmtPct(m.win_rate, { html: true, title: "胜率 0~1 小数 → 百分比" }) + "</td>"
          + "<td><button type='button' class='btn-del' data-del-run='" + (r.run_id || "") + "'>删除</button></td>"
          + "</tr>";
      }).join("");
      box.innerHTML = head + body + "</tbody></table></div>";
"""
    if "historyReturnDisplay" not in t.split("renderHistory")[1][:2500]:
        if old_cells not in t:
            raise SystemExit("history cells not found")
        t = t.replace(old_cells, new_cells, 1)

    # Prefer full metrics over incomplete brief when both exist and brief lacks win_rate etc.
    old_m = "        const m = r.metrics_brief || r.metrics || {};\n"
    new_m = (
        "        const m = Object.assign({}, r.metrics || {}, r.metrics_brief || {});\n"
        "        // prefer full metrics for any key; brief only fills gaps\n"
        "        if (r.metrics && typeof r.metrics === 'object') Object.assign(m, r.metrics);\n"
    )
    # Actually simpler: use metrics first
    new_m = "        const m = Object.assign({}, r.metrics_brief || {}, r.metrics || {});\n"
    if "Object.assign({}, r.metrics_brief" not in t:
        if old_m not in t:
            print("WARN metrics merge skip")
        else:
            t = t.replace(old_m, new_m, 1)

    HTML.write_text(t, encoding="utf-8")
    print("OK index.html")


def main() -> None:
    patch_runs()
    patch_html()
    print("done")


if __name__ == "__main__":
    main()
