# -*- coding: utf-8 -*-
"""Professional rewrite of backtest history task list (UI + field semantics)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index.html"
RUNS = ROOT / "wtpy" / "apps" / "astock" / "service" / "runs.py"


def brace_span(text: str, start_fn: int) -> tuple[int, int]:
    start = text.find("{", start_fn)
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return start_fn, k + 1
    raise RuntimeError("unbalanced braces")


def patch_runs_brief() -> None:
    t = RUNS.read_text(encoding="utf-8")
    old = """    keys = (
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
"""
    new = """    keys = (
        "total_return",
        "mean_symbol_return",
        "median_symbol_return",
        "annual_return",
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
        "sharpe",
        "profit_factor",
        "cost_total",
    )
"""
    if "annual_return" not in t.split("_metrics_brief")[1][:600]:
        if old not in t:
            # try insert after mean_symbol
            if '"mean_symbol_return",' in t and '"annual_return"' not in t.split("keys =")[1][:400]:
                t = t.replace(
                    '"mean_symbol_return",\n',
                    '"mean_symbol_return",\n        "annual_return",\n        "sharpe",\n        "profit_factor",\n',
                    1,
                )
                RUNS.write_text(t, encoding="utf-8")
                print("runs brief patched (insert)")
            else:
                print("runs brief skip")
        else:
            RUNS.write_text(t.replace(old, new, 1), encoding="utf-8")
            print("runs brief patched")
    else:
        print("runs brief ok")


NEW_HISTORY_CSS = r"""
    /* ===== 历史任务列表（专业回测对照表） ===== */
    .history-actions {
      margin: 0 0 0.75rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem 0.55rem;
      align-items: center;
    }
    .history-actions .sel-info {
      color: var(--muted);
      font-size: 0.82rem;
      margin-left: 0.15rem;
    }
    .history-wrap {
      overflow-x: auto;
      max-width: 100%;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #121a28;
    }
    .history-table {
      width: 100%;
      min-width: 1100px;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.84rem;
      table-layout: fixed;
    }
    .history-table thead th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #182233;
      color: var(--muted);
      font-weight: 600;
      font-size: 0.78rem;
      letter-spacing: 0.02em;
      border-bottom: 1px solid var(--border);
      padding: 0.65rem 0.55rem;
      text-align: left;
      vertical-align: middle;
      white-space: nowrap;
    }
    .history-table tbody td {
      border-bottom: 1px solid rgba(45, 58, 77, 0.85);
      padding: 0.7rem 0.55rem;
      text-align: left;
      vertical-align: middle;
    }
    .history-table tbody tr.hist-row {
      cursor: pointer;
      transition: background 0.12s ease;
    }
    .history-table tbody tr.hist-row:hover td {
      background: rgba(61, 139, 253, 0.07);
    }
    .history-table tbody tr.hist-row.active td {
      background: rgba(61, 139, 253, 0.12);
    }
    .history-table th.col-check,
    .history-table td.col-check {
      width: 2.4rem;
      min-width: 2.4rem;
      max-width: 2.4rem;
      text-align: center;
      padding-left: 0.45rem;
      padding-right: 0.25rem;
    }
    .history-table th.col-strategy,
    .history-table td.col-strategy {
      width: 28%;
      min-width: 16rem;
    }
    .history-table th.col-time,
    .history-table td.col-time {
      width: 8.2rem;
      white-space: nowrap;
      color: var(--muted);
      font-size: 0.8rem;
    }
    .history-table th.col-status,
    .history-table td.col-status {
      width: 4.6rem;
      white-space: nowrap;
      text-align: center;
    }
    .history-table th.col-period,
    .history-table td.col-period {
      width: 4.2rem;
      white-space: nowrap !important;
      text-align: center;
      word-break: keep-all;
    }
    .history-table th.col-mode,
    .history-table td.col-mode {
      width: 6.2rem;
      white-space: nowrap;
      text-align: center;
    }
    .history-table th.num,
    .history-table td.num {
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
      width: 6.2rem;
      min-width: 5.2rem;
      padding-left: 0.4rem;
      padding-right: 0.7rem;
    }
    .history-table th.col-ops,
    .history-table td.col-ops {
      width: 8.5rem;
      min-width: 8.5rem;
      white-space: nowrap;
      text-align: right;
      padding-right: 0.65rem;
    }
    .history-table td.col-ops .ops-row {
      display: inline-flex;
      flex-direction: row;
      flex-wrap: nowrap;
      align-items: center;
      justify-content: flex-end;
      gap: 0.35rem;
    }
    .hist-check {
      width: 1.05rem;
      height: 1.05rem;
      cursor: pointer;
      vertical-align: middle;
    }
    .hist-title {
      font-weight: 650;
      color: var(--text);
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      word-break: break-word;
    }
    .hist-tags {
      margin-top: 0.28rem;
      display: flex;
      flex-wrap: wrap;
      gap: 0.28rem;
      align-items: center;
    }
    .hist-tag {
      display: inline-block;
      padding: 0.08rem 0.38rem;
      border-radius: 999px;
      font-size: 0.72rem;
      line-height: 1.35;
      color: #a8b7ce;
      background: rgba(61, 139, 253, 0.08);
      border: 1px solid rgba(61, 139, 253, 0.18);
      white-space: nowrap;
    }
    .hist-tag.mode-per {
      color: #f5c26b;
      background: rgba(245, 165, 36, 0.1);
      border-color: rgba(245, 165, 36, 0.28);
    }
    .hist-tag.mode-pool {
      color: #7eb6ff;
      background: rgba(61, 139, 253, 0.1);
      border-color: rgba(61, 139, 253, 0.28);
    }
    .hist-id {
      margin-top: 0.22rem;
      color: #6d7d96;
      font-size: 0.72rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .hist-extra {
      margin-top: 0.18rem;
      color: #6d7d96;
      font-size: 0.72rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .mode-pill {
      display: inline-block;
      padding: 0.12rem 0.42rem;
      border-radius: 999px;
      font-size: 0.72rem;
      white-space: nowrap;
      border: 1px solid var(--border);
      color: var(--muted);
      background: rgba(255, 255, 255, 0.03);
    }
    .mode-pill.per { color: #f5c26b; border-color: rgba(245,165,36,.35); }
    .mode-pill.pool { color: #7eb6ff; border-color: rgba(61,139,253,.35); }
    /* A股：红涨绿跌 */
    .history-table td.num .pct-pos { color: #f31260; font-weight: 600; }
    .history-table td.num .pct-neg { color: #3dd68c; font-weight: 600; }
    .history-table td.num .pct-zero { color: var(--muted); }
    .history-table td.num .pct-mdd { color: #f5a524; font-weight: 600; }
    .history-table td.num .pct-note { display: none; } /* 主列表隐藏调试小数 */
    .btn-op {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 1.7rem;
      padding: 0 0.55rem;
      font-size: 0.75rem;
      border-radius: 6px;
      cursor: pointer;
      border: 1px solid var(--border);
      background: #1c2838;
      color: var(--text);
      white-space: nowrap;
      line-height: 1;
    }
    .btn-op:hover { border-color: var(--accent); color: #fff; }
    .btn-op.btn-del,
    .btn-del {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 1.7rem;
      margin: 0;
      padding: 0 0.55rem;
      font-size: 0.75rem;
      border-radius: 6px;
      cursor: pointer;
      background: rgba(243, 18, 96, 0.1);
      color: #f07178;
      border: 1px solid rgba(243, 18, 96, 0.35);
      white-space: nowrap;
      line-height: 1;
    }
    .btn-op.btn-del:hover { background: rgba(243, 18, 96, 0.18); }
    .badge-status {
      display: inline-block;
      padding: 0.12rem 0.45rem;
      border-radius: 999px;
      font-size: 0.72rem;
      border: 1px solid var(--border);
      white-space: nowrap;
    }
    .history-empty {
      color: var(--muted);
      padding: 1.25rem 0.5rem;
      text-align: center;
    }
"""


NEW_HELPERS = r"""
    // ---------- 历史任务列表：字段口径与分层展示 ----------
    function isPerSymbolMode(m, r) {
      const mode = String(
        (m && m.account_mode) || (r && r.account_mode) || ""
      ).toLowerCase();
      return mode === "per_symbol" || mode === "tdx" || mode === "per_stock";
    }
    function accountModeLabel(m, r) {
      return isPerSymbolMode(m, r) ? "单票独立资金" : "共享资金池";
    }
    function accountModeShort(m, r) {
      return isPerSymbolMode(m, r) ? "单票独立" : "组合资金";
    }
    /** 列表主收益字段：单票模式用等权平均，否则用总收益 */
    function primaryReturnValue(m, r) {
      if (isPerSymbolMode(m, r) && m.mean_symbol_return != null && !Number.isNaN(Number(m.mean_symbol_return))) {
        return { value: m.mean_symbol_return, label: "单票平均收益", tip: "等权平均各单票账户总收益率（通达信对照口径）" };
      }
      return { value: m.total_return, label: "总收益", tip: "共享资金池组合账户总收益率" };
    }
    /** 最大回撤：列表用正数幅度展示（5.18%），tooltip 说明为回撤幅度 */
    function fmtMaxDrawdownAbs(x, opts) {
      if (x === undefined || x === null || Number.isNaN(Number(x))) return "—";
      const n = Math.abs(Number(x)) * 100;
      const body = n.toFixed(2) + "%";
      const tip = (opts && opts.title) || ("最大回撤幅度（由权益峰值到谷底）。原始小数 " + x);
      if (opts && opts.html) {
        return "<span class='pct-mdd' title='" + String(tip).replace(/'/g, "&#39;") + "'>" + body + "</span>";
      }
      return body;
    }
    /** 策略名：优先 indicator_names，去掉技术前缀；否则从 title 取主段 */
    function strategyPrimaryName(r) {
      let names = r.indicator_names || r.indicator_ids || [];
      if (typeof names === "string") names = [names];
      if (Array.isArray(names) && names.length) {
        const nice = names.map((n) => {
          let s = String(n || "");
          ["tn6_", "txt_", "user_"].forEach((p) => {
            if (s.startsWith(p)) s = s.slice(p.length);
          });
          return s;
        }).filter(Boolean);
        if (nice.length) return nice.join(" + ");
      }
      const title = String(r.title || "");
      if (!title) return r.run_id || "回测任务";
      // title 形如：策略 · 模式 · 日线 · 持有1 · 区间
      const head = title.split(" · ")[0].trim();
      return head || title;
    }
    function strategyParamTags(r, m) {
      const tags = [];
      const pl = r.period_label || periodLabel(r.period);
      if (pl) tags.push(pl);
      if (r.hold != null && r.hold !== "") tags.push("持有" + r.hold + "期");
      if (r.entry_lag != null && r.entry_lag !== "") tags.push("延迟" + r.entry_lag);
      tags.push(accountModeShort(m, r));
      if (r.start || r.end) {
        tags.push(fmtYmd(r.start) + " ~ " + fmtYmd(r.end));
      }
      return tags;
    }
    function strategyExtraMetrics(m) {
      const bits = [];
      if (m.annual_return != null && !Number.isNaN(Number(m.annual_return))) {
        bits.push("年化 " + fmtPct(m.annual_return));
      }
      if (m.sharpe != null && !Number.isNaN(Number(m.sharpe))) {
        bits.push("夏普 " + Number(m.sharpe).toFixed(2));
      }
      if (m.profit_factor != null && !Number.isNaN(Number(m.profit_factor))) {
        bits.push("盈亏比 " + Number(m.profit_factor).toFixed(2));
      }
      return bits.join(" ｜ ");
    }
    function tradeCountValue(m) {
      // 优先完整回合；否则买入次数
      if (m.n_round_trips != null) return m.n_round_trips;
      if (m.n_buys != null) return m.n_buys;
      return null;
    }
    function escAttr(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }
"""


NEW_RENDER_HISTORY = r"""
    function renderHistory(rows, activeRunId) {
      const box = document.getElementById("history");
      if (!box) return;
      window.__historyRows = rows || [];
      const alive = new Set((rows || []).map((r) => r.run_id).filter(Boolean));
      Array.from(selectedRuns).forEach((id) => {
        if (!alive.has(id)) selectedRuns.delete(id);
      });
      if (!rows || !rows.length) {
        box.className = "history-empty";
        box.textContent = "暂无历史任务（完成后会出现在这里）";
        updateHistSelInfo();
        return;
      }
      box.className = "";

      // 统一列定义：表头与单元格共用，避免错位
      // 主列表：策略分层 | 时间 | 状态 | 周期 | 资金模式 | 收益 | 年化 | 最大回撤 | 交易笔数 | 胜率 | 操作
      const head =
        "<div class='history-wrap'><table class='history-table'>" +
        "<thead><tr>" +
        "<th class='col-check'><input type='checkbox' class='hist-check' id='histCheckAll' title='全选/取消全选' /></th>" +
        "<th class='col-strategy'>策略/参数</th>" +
        "<th class='col-time'>创建时间</th>" +
        "<th class='col-status'>状态</th>" +
        "<th class='col-period'>周期</th>" +
        "<th class='col-mode'>资金模式</th>" +
        "<th class='num' title='组合=总收益；单票独立=等权平均单票收益'>收益</th>" +
        "<th class='num' title='年化收益率'>年化</th>" +
        "<th class='num' title='最大回撤幅度（正数）'>最大回撤</th>" +
        "<th class='num' title='优先完整回合数，否则买入次数'>交易笔数</th>" +
        "<th class='num' title='盈利回合/已平仓回合'>胜率</th>" +
        "<th class='col-ops'>操作</th>" +
        "</tr></thead><tbody>";

      const body = rows
        .map((r, idx) => {
          const m = Object.assign({}, r.metrics_brief || {}, r.metrics || {});
          const primary = strategyPrimaryName(r);
          const tags = strategyParamTags(r, m);
          const extra = strategyExtraMetrics(m);
          const retMeta = primaryReturnValue(m, r);
          const modeLabel = accountModeLabel(m, r);
          const modeCls = isPerSymbolMode(m, r) ? "per" : "pool";
          const active = activeRunId && r.run_id === activeRunId ? " active" : "";
          const checked = selectedRuns.has(r.run_id) ? " checked" : "";
          const periodText = r.period_label || periodLabel(r.period) || "—";
          const fullTitle = escAttr(
            primary +
              " | " +
              tags.join(" · ") +
              (r.run_id ? " | " + r.run_id : "") +
              (extra ? " | " + extra : "")
          );

          const tagHtml = tags
            .map((tg) => {
              let cls = "hist-tag";
              if (tg.indexOf("单票") >= 0) cls += " mode-per";
              if (tg.indexOf("组合") >= 0 || tg.indexOf("共享") >= 0) cls += " mode-pool";
              return "<span class='" + cls + "'>" + tg + "</span>";
            })
            .join("");

          const retHtml = fmtPct(retMeta.value, {
            html: true,
            showRaw: false,
            title: retMeta.tip + " · 表头口径：" + retMeta.label,
          });
          const annHtml = fmtPct(m.annual_return, {
            html: true,
            showRaw: false,
            title: "年化收益率",
          });
          const mddHtml = fmtMaxDrawdownAbs(m.max_drawdown, {
            html: true,
            title: "最大回撤幅度（权益峰值到谷底）",
          });
          const winHtml = fmtPct(m.win_rate, {
            html: true,
            showRaw: false,
            title: "胜率 = 盈利已平仓回合 / 已平仓回合",
          });
          const nTrade = tradeCountValue(m);

          return (
            "<tr class='hist-row" +
            active +
            "' data-idx='" +
            idx +
            "' data-run='" +
            (r.run_id || "") +
            "'>" +
            "<td class='col-check'><input type='checkbox' class='hist-check hist-row-check' data-run='" +
            (r.run_id || "") +
            "'" +
            checked +
            " /></td>" +
            "<td class='col-strategy' title=\"" +
            fullTitle +
            '">' +
            "<div class='hist-title'>" +
            primary +
            "</div>" +
            (tagHtml ? "<div class='hist-tags'>" + tagHtml + "</div>" : "") +
            (extra ? "<div class='hist-extra' title='" + escAttr(extra) + "'>" + extra + "</div>" : "") +
            (r.run_id ? "<div class='hist-id'>编号 " + r.run_id + "</div>" : "") +
            "</td>" +
            "<td class='col-time'>" +
            fmtTime(r.created_at) +
            "</td>" +
            "<td class='col-status'><span class='badge-status " +
            statusClass(r.status) +
            "' title='" +
            escAttr(r.status_label || statusLabel(r.status) || r.status || "") +
            "'>" +
            (r.status_label || statusLabel(r.status) || "—") +
            "</span></td>" +
            "<td class='col-period'>" +
            periodText +
            "</td>" +
            "<td class='col-mode'><span class='mode-pill " +
            modeCls +
            "' title='" +
            escAttr(modeLabel) +
            "'>" +
            accountModeShort(m, r) +
            "</span></td>" +
            "<td class='num' title='" +
            escAttr(retMeta.label) +
            "'>" +
            retHtml +
            "</td>" +
            "<td class='num'>" +
            annHtml +
            "</td>" +
            "<td class='num'>" +
            mddHtml +
            "</td>" +
            "<td class='num' title='完整回合优先，否则买入次数'>" +
            fmtInt(nTrade) +
            "</td>" +
            "<td class='num'>" +
            winHtml +
            "</td>" +
            "<td class='col-ops'><div class='ops-row'>" +
            "<button type='button' class='btn-op' data-view-run='" +
            (r.run_id || "") +
            "' title='查看回测报告'>查看</button>" +
            "<button type='button' class='btn-op btn-del' data-del-run='" +
            (r.run_id || "") +
            "' title='删除此任务及结果文件'>删除</button>" +
            "</div></td>" +
            "</tr>"
          );
        })
        .join("");

      box.innerHTML = head + body + "</tbody></table></div>";

      const checkAll = document.getElementById("histCheckAll");
      if (checkAll) {
        const allIds = rows.map((r) => r.run_id).filter(Boolean);
        checkAll.checked = allIds.length > 0 && allIds.every((id) => selectedRuns.has(id));
        checkAll.indeterminate = selectedRuns.size > 0 && !checkAll.checked;
        checkAll.addEventListener("change", () => {
          if (checkAll.checked) allIds.forEach((id) => selectedRuns.add(id));
          else allIds.forEach((id) => selectedRuns.delete(id));
          renderHistory(window.__historyRows || [], activeRunId);
        });
      }

      box.querySelectorAll(".hist-row-check").forEach((cb) => {
        cb.addEventListener("click", (ev) => ev.stopPropagation());
        cb.addEventListener("change", () => {
          const rid = cb.getAttribute("data-run");
          if (!rid) return;
          if (cb.checked) selectedRuns.add(rid);
          else selectedRuns.delete(rid);
          updateHistSelInfo();
          const allIds = (window.__historyRows || []).map((r) => r.run_id).filter(Boolean);
          const all = document.getElementById("histCheckAll");
          if (all) {
            all.checked = allIds.length > 0 && allIds.every((id) => selectedRuns.has(id));
            all.indeterminate = selectedRuns.size > 0 && !all.checked;
          }
        });
      });

      const openRun = async (rid) => {
        if (!rid) return;
        box.querySelectorAll("tr.hist-row").forEach((x) => x.classList.remove("active"));
        const tr = box.querySelector("tr.hist-row[data-run='" + rid + "']");
        if (tr) tr.classList.add("active");
        const msg = document.getElementById("runMsg");
        if (msg) msg.textContent = "正在加载：" + rid + " …";
        try {
          await showRunResult({ run_id: rid }, { fromHistory: true });
          if (msg) msg.innerHTML = '<span class="okmsg">已打开历史回测</span>';
        } catch (e) {
          if (msg) msg.innerHTML = '<span class="err">' + e.message + "</span>";
        }
      };

      box.querySelectorAll("tr.hist-row").forEach((tr) => {
        tr.addEventListener("click", async (ev) => {
          if (
            ev.target &&
            (ev.target.tagName === "BUTTON" ||
              ev.target.tagName === "INPUT" ||
              (ev.target.closest && (ev.target.closest("button") || ev.target.closest("input"))))
          ) {
            return;
          }
          await openRun(tr.getAttribute("data-run"));
        });
      });

      box.querySelectorAll("[data-view-run]").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          await openRun(btn.getAttribute("data-view-run"));
        });
      });

      box.querySelectorAll("[data-del-run]").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          const rid = btn.getAttribute("data-del-run");
          if (!rid) return;
          await deleteRunsByIds([rid]);
        });
      });

      updateHistSelInfo();
    }
"""


def replace_css(t: str) -> str:
    # Remove old history-related CSS blocks and inject new unified block once.
    # Anchor: after .history-actions original if present, or before .hist-title
    start_markers = [
        "    .history-actions {",
        "    .history-table {",
        "    /* ===== 历史任务列表",
    ]
    start = -1
    for m in start_markers:
        i = t.find(m)
        if i >= 0:
            start = i if start < 0 else min(start, i)
    if start < 0:
        # insert before .hist-title
        i = t.find("    .hist-title")
        if i < 0:
            raise SystemExit("cannot locate history CSS region")
        return t[:i] + NEW_HISTORY_CSS + "\n" + t[i:]

    # end: stop before .result-kv or #equityChart or .badge-status without history
    end_candidates = [
        t.find("    .result-kv", start + 10),
        t.find("    #equityChart", start + 10),
        t.find("    .progress-wrap", start + 10),
        t.find("    .pool-mode", start + 10),
    ]
    end_candidates = [e for e in end_candidates if e > start]
    # Also find last history-related rule end by scanning to .history-empty or after col-ops
    end = min(end_candidates) if end_candidates else start + 50

    # Prefer cutting from first history-actions/table through previous history-empty block
    # Expand end if still inside history styles
    probe = t.find("    .history-empty", start)
    if probe > start:
        # include history-empty rule fully
        brace = t.find("}", t.find("{", probe)) + 1
        end = max(end, brace)

    # If we already injected, replace whole professional block
    if "专业回测对照表" in t:
        s2 = t.find("    /* ===== 历史任务列表")
        e2 = t.find("    .history-empty", s2)
        if e2 > 0:
            brace = t.find("}", t.find("{", e2)) + 1
            t = t[:s2] + NEW_HISTORY_CSS + t[brace:]
            print("replaced existing pro CSS")
            return t

    t = t[:start] + NEW_HISTORY_CSS + t[end:]
    print("CSS region replaced", start, end)
    return t


def replace_helpers(t: str) -> str:
    # Replace historyReturnDisplay through fmtMax if needed; insert helpers before historyReturnDisplay or after fmtInt
    if "function isPerSymbolMode" in t:
        # replace from isPerSymbolMode to before function fmtTime or renderHistory
        i = t.find("function isPerSymbolMode")
        # find previous helper start if we re-run
        j = t.find("function renderHistory")
        # keep fmtTime etc. — helpers should end before fmtTime if we placed before it
        # Safer: remove old isPer... through strategyExtra if present, then insert fresh before renderHistory
        # Find start of helper pack
        for name in ["function isPerSymbolMode", "function historyReturnDisplay", "function fracToPctNumber"]:
            pass
        # Delete old pack if exists
        s = t.find("    // ---------- 历史任务列表：字段口径与分层展示 ----------")
        if s < 0:
            s = t.find("function isPerSymbolMode")
            s = t.rfind("\n", 0, s) + 1
        e = t.find("function renderHistory")
        t = t[:s] + NEW_HELPERS + "\n" + t[e:]
        print("helpers replaced")
        return t

    # First time: insert before historyReturnDisplay if present, else before renderHistory
    if "function historyReturnDisplay" in t:
        i = t.find("function historyReturnDisplay")
        # remove old historyReturnDisplay function only
        _, end = brace_span(t, i)
        # also remove leading whitespace line
        line_start = t.rfind("\n", 0, i) + 1
        t = t[:line_start] + NEW_HELPERS + "\n" + t[end:]
        print("helpers inserted (replaced historyReturnDisplay)")
        return t

    i = t.find("function renderHistory")
    t = t[:i] + NEW_HELPERS + "\n" + t[i:]
    print("helpers inserted before renderHistory")
    return t


def replace_render(t: str) -> str:
    i = t.find("function renderHistory")
    if i < 0:
        raise SystemExit("renderHistory not found")
    _, end = brace_span(t, i)
    t = t[:i] + NEW_RENDER_HISTORY.strip() + "\n" + t[end:]
    print("renderHistory replaced", end - i, "chars")
    return t


def tweak_fmt_pct_default_no_raw(t: str) -> str:
    # ensure showRaw default false; history already passes false
    # also hide scientific under metrics panel optional - user asked list mainly
    return t


def main() -> None:
    patch_runs_brief()
    t = HTML.read_text(encoding="utf-8")
    t = replace_css(t)
    t = replace_helpers(t)
    t = replace_render(t)
    t = tweak_fmt_pct_default_no_raw(t)

    # sanity
    assert "function renderHistory" in t
    assert "策略/参数" in t
    assert "最大回撤" in t
    assert "交易笔数" in t
    assert "资金模式" in t
    assert "ops-row" in t
    assert "function fmtMaxDrawdownAbs" in t
    assert t.count("function renderHistory") == 1

    HTML.write_text(t, encoding="utf-8")
    print("OK wrote", HTML, "size", HTML.stat().st_size)


if __name__ == "__main__":
    main()
