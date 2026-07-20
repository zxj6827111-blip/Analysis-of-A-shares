# -*- coding: utf-8 -*-
from pathlib import Path

p = Path(r"E:\Software Development\wtpy-master\wtpy\apps\astock\web\static\index.html")
t = p.read_text(encoding="utf-8")

# ---- CSS ----
css = """
    .progress-wrap {
      margin: 0.75rem 0 0.5rem;
      display: none;
    }
    .progress-wrap.active { display: block; }
    .progress-meta {
      display: flex; justify-content: space-between; gap: 0.75rem;
      font-size: 0.85rem; color: var(--muted); margin-bottom: 0.35rem;
    }
    .progress-meta strong { color: var(--text); font-weight: 600; }
    .progress-bar {
      height: 12px; background: #0c121a; border: 1px solid var(--border);
      border-radius: 999px; overflow: hidden; position: relative;
    }
    .progress-bar > i {
      display: block; height: 100%; width: 0%;
      background: linear-gradient(90deg, #1f6feb, #3dd68c);
      transition: width 0.35s ease;
    }
    .progress-bar.indeterminate > i {
      width: 35% !important;
      animation: progslide 1.2s ease-in-out infinite;
    }
    @keyframes progslide {
      0% { transform: translateX(-120%); }
      100% { transform: translateX(320%); }
    }
    .progress-detail {
      margin-top: 0.4rem; font-size: 0.82rem; color: var(--muted);
      word-break: break-all;
    }
    .history-table {
      width: 100%; border-collapse: collapse; font-size: 0.86rem;
    }
    .history-table th, .history-table td {
      border-bottom: 1px solid var(--border);
      padding: 0.45rem 0.4rem; text-align: left; vertical-align: top;
    }
    .history-table th { color: var(--muted); font-weight: 600; }
    .history-table tr:hover td { background: rgba(61,139,253,0.08); }
    .history-empty { color: var(--muted); }
    .badge-status {
      display: inline-block; padding: 0.1rem 0.4rem; border-radius: 4px;
      font-size: 0.75rem; border: 1px solid var(--border);
    }
    .badge-status.ok { color: var(--ok); border-color: rgba(61,214,140,.4); }
    .badge-status.bad { color: var(--bad); border-color: rgba(243,18,96,.4); }
    .badge-status.warn { color: var(--warn); border-color: rgba(245,165,36,.4); }
"""

if ".progress-wrap" not in t:
    if "</style>" not in t:
        raise SystemExit("no style tag")
    t = t.replace("</style>", css + "\n  </style>", 1)

# ---- HTML under runMsg ----
old_runmsg = """        <div id=\"runMsg\" style=\"margin-top:0.5rem\"></div>
      </div>

      <div class=\"card\">
        <h2>结果</h2>
"""
new_runmsg = """        <div id=\"runMsg\" style=\"margin-top:0.5rem\"></div>
        <div id=\"progressWrap\" class=\"progress-wrap\">
          <div class=\"progress-meta\">
            <span id=\"progressLabel\"><strong>进度</strong></span>
            <span id=\"progressPct\">0%</span>
          </div>
          <div class=\"progress-bar\" id=\"progressBar\"><i id=\"progressFill\"></i></div>
          <div class=\"progress-detail\" id=\"progressDetail\">—</div>
        </div>
      </div>

      <div class=\"card\">
        <h2>结果</h2>
"""
if old_runmsg not in t:
    # try looser
    if 'id="progressWrap"' not in t:
        marker = '<div id="runMsg" style="margin-top:0.5rem"></div>'
        if marker not in t:
            raise SystemExit("runMsg not found")
        t = t.replace(
            marker,
            marker
            + """
        <div id="progressWrap" class="progress-wrap">
          <div class="progress-meta">
            <span id="progressLabel"><strong>进度</strong></span>
            <span id="progressPct">0%</span>
          </div>
          <div class="progress-bar" id="progressBar"><i id="progressFill"></i></div>
          <div class="progress-detail" id="progressDetail">—</div>
        </div>
""",
            1,
        )
else:
    t = t.replace(old_runmsg, new_runmsg, 1)

# history pre -> container
t = t.replace(
    '<pre id="history">—</pre>',
    '<div id="history" class="history-empty">点击「历史任务」加载</div>',
    1,
)

# ---- JS helpers: replace showJobProgress / pollJob / history ----
# Find from function fmtElapsed or showJobProgress / pollJob through btnRefresh
start = t.find("function fmtElapsed")
if start < 0:
    start = t.find("async function pollJob")
if start < 0:
    raise SystemExit("pollJob not found")
end = t.find('document.getElementById("btnRefresh")', start)
if end < 0:
    raise SystemExit("btnRefresh not found")

new_js = r'''function fmtElapsed(sec) {
      sec = Math.max(0, Math.floor(sec));
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      if (m >= 60) {
        const h = Math.floor(m / 60);
        return h + "小时" + (m % 60) + "分" + s + "秒";
      }
      return m + "分" + s + "秒";
    }

    function setProgressUI(pct, label, detail, { indeterminate = false, active = true } = {}) {
      const wrap = document.getElementById("progressWrap");
      const fill = document.getElementById("progressFill");
      const bar = document.getElementById("progressBar");
      const pctEl = document.getElementById("progressPct");
      const labEl = document.getElementById("progressLabel");
      const detEl = document.getElementById("progressDetail");
      if (!wrap) return;
      if (active) wrap.classList.add("active");
      else wrap.classList.remove("active");
      const p = Math.max(0, Math.min(100, Number(pct) || 0));
      if (fill) fill.style.width = p + "%";
      if (pctEl) pctEl.textContent = (indeterminate ? "…" : (p.toFixed(1).replace(/\.0$/, "") + "%"));
      if (labEl) labEl.innerHTML = "<strong>" + (label || "进度") + "</strong>";
      if (detEl) detEl.textContent = detail || "—";
      if (bar) {
        if (indeterminate) bar.classList.add("indeterminate");
        else bar.classList.remove("indeterminate");
      }
    }

    function hideProgressSoon() {
      setTimeout(() => {
        const wrap = document.getElementById("progressWrap");
        if (wrap) wrap.classList.remove("active");
      }, 2500);
    }

    function showJobProgress(job, startedAt) {
      const elapsed = (Date.now() / 1000) - (job.created_at || startedAt);
      const req = job.request || {};
      const prog = job.progress || {};
      let pct = prog.pct;
      if (pct === undefined || pct === null) {
        // fallback estimate when backend has no progress yet
        if (job.status === "succeeded") pct = 100;
        else if (job.status === "failed") pct = prog.pct || 0;
        else if (job.status === "queued") pct = 0;
        else pct = Math.min(95, 5 + elapsed * 0.15); // soft estimate only
      }
      const phaseMap = {
        queued: "排队中",
        starting: "启动中",
        prepare: "准备",
        signals: "计算信号",
        factors: "校验复权",
        portfolio: "组合回测",
        writing: "写结果",
        done: "完成",
        failed: "失败",
      };
      const phase = phaseMap[prog.phase] || prog.phase || job.status || "running";
      const cur = prog.current || 0;
      const total = prog.total || 0;
      const code = prog.code ? (" · " + prog.code) : "";
      const msg = prog.message || "";
      const detail = [
        phase,
        total ? (cur + "/" + total) : "",
        msg,
        code,
        "已运行 " + fmtElapsed(elapsed),
        job.job_id || "",
      ].filter(Boolean).join(" · ");
      const indeterminate = job.status === "queued" || (job.status === "running" && (pct <= 1 || !prog.phase));
      setProgressUI(pct, phase + (total ? (" " + cur + "/" + total) : ""), detail, {
        indeterminate: false,
        active: true,
      });
      const lines = {
        "job_id": job.job_id,
        "status": job.status,
        "progress_pct": Number(pct).toFixed(2),
        "phase": prog.phase || null,
        "current": cur,
        "total": total,
        "message": msg,
        "code": prog.code || null,
        "已运行": fmtElapsed(elapsed),
        "规则": (req.rule_ids || []).join(", "),
        "周期": req.period,
        "股票池": (req.codes && req.codes.length) ? req.codes.join(",") : "(默认)",
        "区间": String(req.start || "") + " ~ " + String(req.end || ""),
        "run_id": job.run_id || "(完成后生成)",
      };
      const box = document.getElementById("resultJson");
      if (box) box.textContent = JSON.stringify(lines, null, 2);
      return "异步任务 " + job.status + " · " + Number(pct).toFixed(1) + "% · 已运行 " + fmtElapsed(elapsed);
    }

    async function pollJob(jobId, msg) {
      const startedAt = Date.now() / 1000;
      setProgressUI(0, "排队/启动", "job " + jobId, { active: true });
      try {
        const j0 = await api("/api/v1/backtests/jobs/" + jobId);
        msg.textContent = showJobProgress(j0, j0.created_at || startedAt);
        if (j0.status === "succeeded") {
          setProgressUI(100, "完成", "run_id=" + (j0.run_id || ""), { active: true });
          hideProgressSoon();
          return j0.result || j0;
        }
        if (j0.status === "failed") {
          setProgressUI((j0.progress && j0.progress.pct) || 0, "失败", j0.error || "job failed", { active: true });
          const box = document.getElementById("resultJson");
          if (box) {
            box.textContent = JSON.stringify({
              status: "failed",
              job_id: jobId,
              run_id: j0.run_id,
              error: j0.error,
              progress: j0.progress || null,
              result: j0.result || null
            }, null, 2);
          }
          throw new Error(j0.error || "job failed");
        }
      } catch (e) {
        if (String(e.message || e).indexOf("job failed") >= 0 ||
            String(e.message || e).indexOf("adjustment") >= 0 ||
            String(e.message || e).indexOf("No-Go") >= 0 ||
            String(e.message || e).indexOf("blocked") >= 0) {
          throw e;
        }
        msg.textContent = "任务 " + jobId + " 已提交，等待状态…";
      }
      for (let i = 0; i < 3600; i++) {
        await new Promise(r => setTimeout(r, 1000));
        const j = await api("/api/v1/backtests/jobs/" + jobId);
        msg.textContent = showJobProgress(j, j.created_at || startedAt);
        if (j.status === "succeeded") {
          setProgressUI(100, "完成", "run_id=" + (j.run_id || ""), { active: true });
          hideProgressSoon();
          return j.result || j;
        }
        if (j.status === "failed") {
          setProgressUI((j.progress && j.progress.pct) || 0, "失败", j.error || "job failed", { active: true });
          const box = document.getElementById("resultJson");
          if (box) {
            box.textContent = JSON.stringify({
              status: "failed",
              job_id: jobId,
              run_id: j.run_id,
              error: j.error,
              progress: j.progress || null,
              result: j.result || null
            }, null, 2);
          }
          throw new Error(j.error || "job failed");
        }
      }
      throw new Error("任务超时");
    }

    function fmtPct(x) {
      if (x === undefined || x === null || Number.isNaN(Number(x))) return "—";
      return (Number(x) * 100).toFixed(2) + "%";
    }

    function fmtTime(ts) {
      if (!ts) return "—";
      try {
        const d = new Date((Number(ts) < 1e12 ? Number(ts) * 1000 : Number(ts)));
        if (Number.isNaN(d.getTime())) return String(ts);
        return d.toLocaleString();
      } catch (_) {
        return String(ts);
      }
    }

    function statusClass(st) {
      const s = String(st || "");
      if (s === "ok" || s === "succeeded") return "ok";
      if (s === "no_go" || s === "failed" || s.indexOf("reject") >= 0) return "bad";
      if (s.indexOf("research") >= 0) return "warn";
      return "";
    }

    function renderHistory(rows) {
      const box = document.getElementById("history");
      if (!box) return;
      if (!rows || !rows.length) {
        box.className = "history-empty";
        box.textContent = "暂无历史任务（完成后会出现在这里）";
        return;
      }
      box.className = "";
      const head = "<table class='history-table'><thead><tr>"
        + "<th>时间</th><th>run_id</th><th>状态</th><th>规则</th><th>周期</th>"
        + "<th>收益</th><th>回撤</th><th>买入</th><th>胜率</th>"
        + "</tr></thead><tbody>";
      const body = rows.map(r => {
        const m = r.metrics_brief || r.metrics || {};
        const rules = (r.indicator_ids || []).join(", ");
        return "<tr>"
          + "<td>" + fmtTime(r.created_at) + "</td>"
          + "<td><code>" + (r.run_id || "") + "</code></td>"
          + "<td><span class='badge-status " + statusClass(r.status) + "'>" + (r.status || "") + "</span></td>"
          + "<td>" + (rules || "—") + "</td>"
          + "<td>" + (r.period || "—") + "</td>"
          + "<td>" + fmtPct(m.total_return) + "</td>"
          + "<td>" + fmtPct(m.max_drawdown) + "</td>"
          + "<td>" + (m.n_buys != null ? m.n_buys : "—") + "</td>"
          + "<td>" + fmtPct(m.win_rate) + "</td>"
          + "</tr>";
      }).join("");
      box.innerHTML = head + body + "</tbody></table>";
    }

    '''

t = t[:start] + new_js + t[end:]

# Fix btnLoadRuns handler to use renderHistory
old_hist = '''    document.getElementById("btnLoadRuns").onclick = async () => {
      try {
        const rows = await api("/api/v1/runs?limit=30");
        document.getElementById("history").textContent = JSON.stringify(rows, null, 2);
      } catch (e) {
        document.getElementById("history").textContent = e.message;
      }
    };
'''
new_hist = '''    document.getElementById("btnLoadRuns").onclick = async () => {
      const box = document.getElementById("history");
      try {
        box.className = "history-empty";
        box.textContent = "加载中…";
        const rows = await api("/api/v1/runs?limit=50");
        renderHistory(rows);
      } catch (e) {
        box.className = "history-empty";
        box.textContent = e.message;
      }
    };
'''
if old_hist not in t:
    # looser replace
    import re
    t2, n = re.subn(
        r'document\.getElementById\("btnLoadRuns"\)\.onclick = async \(\) => \{[\s\S]*?\n    \};',
        new_hist.strip() + "\n",
        t,
        count=1,
    )
    if n != 1:
        raise SystemExit("btnLoadRuns handler not replaced")
    t = t2
else:
    t = t.replace(old_hist, new_hist, 1)

# On successful run, refresh history automatically
old_ok = '''        msg.innerHTML = '<span class="okmsg">完成 run_id=' + (r.run_id || "") + " status=" + r.status + "</span>";
        document.getElementById("resultJson").textContent = JSON.stringify(r, null, 2);
        showMetrics(r.metrics || {});
'''
new_ok = '''        msg.innerHTML = '<span class="okmsg">完成 run_id=' + (r.run_id || "") + " status=" + r.status + "</span>";
        document.getElementById("resultJson").textContent = JSON.stringify(r, null, 2);
        showMetrics(r.metrics || {});
        setProgressUI(100, "完成", "run_id=" + (r.run_id || ""), { active: true });
        hideProgressSoon();
        // auto refresh history so the new run is visible without extra click
        try {
          const rows = await api("/api/v1/runs?limit=50");
          renderHistory(rows);
        } catch (_) {}
'''
if old_ok not in t:
    raise SystemExit("success block not found")
t = t.replace(old_ok, new_ok, 1)

# On submit, activate progress
old_sub = '''        msg.textContent = "正在提交回测任务…";
'''
if old_sub in t:
    t = t.replace(
        old_sub,
        '''        msg.textContent = "正在提交回测任务…";
        setProgressUI(0, "提交中", "正在创建任务…", { active: true, indeterminate: true });
''',
        1,
    )

# page load: auto load history once
old_boot = '''    loadHealth();
    loadRules().catch(e => { document.getElementById("ruleList").textContent = e.message; });
    loadUniverseAndCalendar();
'''
new_boot = '''    loadHealth();
    loadRules().catch(e => { document.getElementById("ruleList").textContent = e.message; });
    loadUniverseAndCalendar();
    // auto-load history so recent runs are visible
    api("/api/v1/runs?limit=50").then(renderHistory).catch(() => {});
'''
if old_boot in t:
    t = t.replace(old_boot, new_boot, 1)

p.write_text(t, encoding="utf-8")
print("ui progress+history patched")
print("has progressWrap", 'id="progressWrap"' in t)
print("has renderHistory", "function renderHistory" in t)
print("has setProgressUI", "function setProgressUI" in t)
