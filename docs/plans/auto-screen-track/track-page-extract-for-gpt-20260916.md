# 跟踪板块页面代码提取（供外部分析）

- 来源：`wtpy/apps/astock/web/static/index_v3.html`（单页应用，共 16640 行）；下列行号为原文件行号
- 提取日期：2026-09-16；已包含跟踪前端全部代码（导航/CSS/HTML/JS），仅区块间少量共用代码省略
- 页面结构：一级栏目「跟踪」→ L0 指标总览表 → L1 单指标历史周列表 → L2 周明细（可展开行详情）
- 样式依赖：`.wb-root *` 部分基础类（wb-title/wb-muted/wb-secondary 等）复用卦象工作台共用样式
  （原文件约 1300–1388 行），本提取未包含；如需完整视觉上下文可向我索取。
- 后端接口：数据来自 `/api/v1/bagua/track/*` 与 `/api/astock/track/*`
  （路由 `wtpy/apps/astock/api_routes/tracking.py` 655 行、补算 `track_backfill.py` 438 行；
  服务层 `wtpy/apps/astock/service/screen_tracking.py`、`track_export.py`）。
  统计口径文档：`docs/plans/auto-screen-track/contract.md` §3（双口径/分母/null 语义）§6（覆盖率警示）。

---


## 主导航栏（跟踪为一级栏目入口）

原文件第 1420–1430 行

```html
  <nav class="main-nav" id="mainNav">
    <button type="button" class="nav-btn" data-view="backtest"><span class="nav-icon">⌂</span>回测</button>
    <!-- 跟踪：一级栏目（2026-09-15 从卦象工作台页签提升为独立栏目） -->
    <button type="button" class="nav-btn" data-view="track"><span class="nav-icon">◔</span>跟踪</button>
    <!-- 预测栏目已按需求从导航隐藏（view-forecast 区块与 ?module=forecast 深链保留，可随时恢复） -->
    <button type="button" class="nav-btn" data-view="bagua-query"><span class="nav-icon">☰</span>卦象</button>
    <button type="button" class="nav-btn" data-view="rules"><span class="nav-icon">▣</span>规则</button>
    <button type="button" class="nav-btn" data-view="experiment"><span class="nav-icon">⚗</span>实验</button>
    <button type="button" class="nav-btn" data-view="tasks"><span class="nav-icon">▤</span>任务</button>
    <button type="button" class="nav-btn" data-view="datastore"><span class="nav-icon">◈</span>数据</button>
  </nav>
```

## 跟踪板块专用样式（.wb-root 前缀）

原文件第 1389–1402 行

```css
/* 跟踪页签（阶段 3）：只补表格与覆盖率警示的少量样式，复用 --wb-* 配色。
   黄色警示用于「覆盖率低」——契约 §6 要求低覆盖率不得渲染成绿色「已结算」。 */
.wb-root .wb-table{width:100%;border-collapse:collapse;font-size:12px}
.wb-root .wb-table th,.wb-root .wb-table td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--wb-line);white-space:nowrap}
.wb-root .wb-table th:first-child,.wb-root .wb-table td:first-child{text-align:left}
.wb-root .wb-table th[data-wbt-sort]{cursor:pointer;user-select:none}
/* L2 明细展开行：与主行视觉分层（2026-09-15） */
.wb-root .wb-l2-detail > td{background:var(--wb-soft);text-align:left;white-space:normal}
/* 收益类数字涨跌配色（A 股习惯：红涨绿跌）：一眼看出哪周赚钱 */
.wb-root .wb-pos{color:var(--red);font-weight:600}
.wb-root .wb-neg{color:var(--green);font-weight:600}
.wb-root .wb-scroll{overflow-x:auto}
.wb-root .wb-warn{color:#f0c674}
.wb-root .wb-warnbox{padding:10px 12px;border:1px solid #8a6d1f;border-radius:8px;background:rgba(240,198,116,.12);display:grid;gap:6px}
```

## 跟踪页面 HTML 结构（L0/L1/L2 三级钻取）

原文件第 1809–1904 行

```html
<section class="view" id="view-track">
  <div class="page-title-row">
    <div>
      <h1 class="page-title">跟踪</h1>
      <p class="page-sub">周五信号名单的次周真实表现：按指标看历史每周的入选与收益，点进去是当周每只股票的完整明细（开盘价、收盘价、最高涨幅、本周涨幅）。</p>
    </div>
  </div>
  <div id="wbTrackRoot" class="wb-root" aria-label="跟踪">
    <section class="wb-body" id="wb-track-panel" aria-label="跟踪">
      <!-- L0 指标总览 -->
      <div class="wb-block" id="wbTrackL0">
        <h3 class="wb-title"><strong>指标跟踪总览</strong><span class="wb-muted" style="font-size:12px">一行一个规则版本；本周入选可直达周明细</span></h3>
        <div class="wb-row">
          <button type="button" class="wb-secondary" id="wbTrackExport">导出跟踪结果</button>
          <input id="wbTrackSearch" placeholder="按规则 ID 或名称搜索…" autocomplete="off" aria-label="搜索跟踪指标" style="max-width:260px">
        </div>
        <!-- 补算指定历史周：默认折叠（2026-09-16 用户要求收起，需要时点开）。
             信号日必须是该周最后一个交易日——填错会被入口拒绝并提示正确日期。
             2026-09-16 v2（UI 重做）：「补某一周」与「批量」分块成行；规则选择
             复用筛选页同款「检索+复选+chips」（原生多选框要 Ctrl+点击、无搜索、
             选中态滚出视野后不可见），并收进嵌套折叠，标题实时显示当前是
             「全部规则」还是「已选 N 条」。 -->
        <details id="wbTrackBfBox">
          <summary class="wb-muted" style="cursor:pointer;font-size:12px">补算指定历史周（验证规则在过去某周选出的股票与表现）</summary>
          <div class="wb-block" style="margin-top:8px">
            <div class="wb-row">
              <strong style="font-size:13px">补某一周</strong>
              <span class="wb-muted" style="font-size:12px">信号日=该周最后一个交易日（通常周五，节假日周是周四，填错会提示正确日期）</span>
            </div>
            <div class="wb-row">
              <input id="wbTrackBfWeek" placeholder="如 20260731 或 2026-07-31" autocomplete="off" aria-label="补算信号日" style="max-width:210px">
              <button type="button" class="wb-secondary" id="wbTrackBfRun">补算该周</button>
              <span class="wb-muted" style="font-size:12px">不选规则=补全部规则（整周回填）</span>
            </div>
            <!-- 规则选择：默认全部规则；嵌套折叠，只有要指定规则才展开 -->
            <details id="wbTrackBfRulesBox" style="border-top:none;padding-top:0">
              <summary class="wb-muted" style="cursor:pointer;font-size:12px" id="wbTrackBfRuleSummary">规则：全部规则（点开可只补指定规则，单条快约 6~10 倍）</summary>
              <div class="wb-block" style="margin-top:6px;max-width:580px">
                <input id="wbTrackBfRuleSearch" placeholder="检索规则…" autocomplete="off" aria-label="检索补算规则">
                <div class="wb-row" style="font-size:12px">
                  <button type="button" class="wb-secondary" id="wbTrackBfRuleClear" style="min-height:32px;padding:4px 10px">清空</button>
                  <span class="wb-muted" id="wbTrackBfRuleCount">已选 0 条</span>
                  <span class="wb-muted">（最多 5 条，更多请整周回填）</span>
                </div>
                <div class="wb-rulebox" id="wbTrackBfRuleList" aria-label="可补算规则"><span class="wb-muted">展开即加载规则…</span></div>
                <div class="wb-chips" id="wbTrackBfRuleChips" aria-live="polite"></div>
              </div>
            </details>
          </div>
          <div class="wb-block" style="margin-top:4px">
            <div class="wb-row">
              <strong style="font-size:13px">批量补最近 N 周</strong>
              <span class="wb-muted" style="font-size:12px">总是补全部规则，不支持指定规则</span>
            </div>
            <div class="wb-row">
              <select id="wbTrackBfWeeks" aria-label="批量补算最近 N 周" style="max-width:140px">
                <option value="4">最近 4 周</option>
                <option value="8">最近 8 周</option>
                <option value="12">最近 12 周</option>
              </select>
              <button type="button" class="wb-secondary" id="wbTrackBfRunN">批量补算</button>
            </div>
          </div>
          <p class="wb-muted" style="font-size:12px;line-height:1.7">
            补算按<b>当前规则</b>重建该周名单与收益，<b>不等于当时真实发布的名单</b>；
            指定规则只能补<b>更早的、还没发布名单的历史周</b>（最新的周归周五链；已有全量名单的周直接点进去看单条规则即可）。
          </p>
          <div class="wb-jobs" id="wbTrackBfJobs" aria-live="polite"></div>
        </details>
        <p class="wb-muted" id="wbTrackL0Note"></p>
        <p class="wb-error" id="wbTrackL0Error" role="alert" hidden></p>
        <div class="wb-scroll" id="wbTrackL0List" aria-live="polite"></div>
      </div>

      <!-- L1 单指标历史周列表 -->
      <div class="wb-block" id="wbTrackL1" hidden>
        <div class="wb-row">
          <button type="button" class="wb-secondary" id="wbTrackL1Back">← 返回指标总览</button>
        </div>
        <div class="wb-stats" id="wbTrackL1Summary"></div>
        <p class="wb-error" id="wbTrackL1Error" role="alert" hidden></p>
        <div class="wb-scroll" id="wbTrackL1List" aria-live="polite"></div>
      </div>

      <!-- L2 周明细 -->
      <div class="wb-block" id="wbTrackL2" hidden>
        <div class="wb-row">
          <button type="button" class="wb-secondary" id="wbTrackL2Back">← 返回周列表</button>
        </div>
        <div id="wbTrackL2Summary"></div>
        <p class="wb-error" id="wbTrackL2Error" role="alert" hidden></p>
        <div class="wb-scroll" id="wbTrackL2List" aria-live="polite"></div>
      </div>
    </section>
  </div>
</section>
```

## 视图切换函数 showView（跟踪栏目初始化挂钩 wbtBind/wbtEnterTrack）

原文件第 2974–3248 行

```javascript
  function v3MainPath() {
    // Prefer current SPA pathname so / stays on / when switching modules (not forced to /v3).
    if (isTaskDetailRoute) return "/v3";
    const p = location.pathname || "/";
    if (p === "/" || p === "/v3" || p.endsWith("/v3")) return p === "/" ? "/" : "/v3";
    return "/";
  }
  function getCurrentModule() {
    const module = new URLSearchParams(location.search).get("module");
    // G3 fix: "datastore" was missing, so navigating from /v3/task-detail to
    // 数据仓库 bounced to the default module. Keep in sync with nav-btn views.
    const allowed = ["backtest", "forecast", "bagua-query", "workbench", "rules", "experiment", "tasks", "datastore", "track"];
    if (!allowed.includes(module)) return "backtest";
    // 旧 `?module=workbench` 兼容：独立工作台视图已合并进「查询卦象」，
    // 这里统一规范化后再做视图切换（否则 view-workbench 不存在 → 空白页）
    return module === "workbench" ? "bagua-query" : module;
  }


  window.AppState = {
    activeModule: "rules",
    taskPage: 1,
    taskPageSize: 10,

    btRuleIds: [],
    tasks: [],

    rules: [],
    ruleCategories: [],
    selectedRuleIds: new Set(),
    activeRule: null,
    ruleDrawerOpen: false,
    runs: [],
    activeRun: null,
    experiments: [],
    activeExperiment: null,
    forecast: { kb: null, weekly: null, results: [] },
    expType: "规则效果对比",
    expRuleIds: [],
    estimate: null,
    health: null,
    visualTest: VISUAL_TEST,
  };

  /* ---------- utils ---------- */
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function toast(msg) {
    const el = $("liveToast");
    el.textContent = msg;
    el.classList.remove("show");
    void el.offsetWidth;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 2800);
  }
  async function api(path, opts) {
    let r;
    try {
      r = await fetch(path, {
        headers: { "Content-Type": "application/json", ...((opts && opts.headers) || {}) },
        ...opts,
      });
    } catch (netErr) {
      // 网络层失败（断连/超时/被取消）：与 HTTP 错误分开标记，
      // 轮询逻辑据此区分「暂时断连」与「记录确已失效(404)」
      const ne = new Error("网络请求失败: " + (netErr && netErr.message));
      ne.network = true;
      throw ne;
    }
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
    if (!r.ok) {
      // D4: structured dataset errors ({code,message,remediation,...}) are
      // rendered readably instead of a generic server-error blob.
      let d = (data && data.detail !== undefined) ? data.detail : (data && data.message) || data;
      let msg;
      if (d && typeof d === "object" && (d.code || d.message)) {
        msg = "[" + (d.code || "ERROR") + "] " + (d.message || "");
        if (d.dataset_id) msg += " (dataset: " + d.dataset_id + ")";
        if (d.requested_source || d.manifest_source) {
          msg += " 请求=" + (d.requested_source || "—") + "/" + (d.requested_adjustment || "—") +
                 " manifest=" + (d.manifest_source || "—") + "/" + (d.manifest_adjustment || "—");
        }
        if (d.remediation) msg += " — " + d.remediation;
      } else {
        msg = (typeof d === "string" && d) || text || r.statusText;
      }
      const he = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      he.status = r.status;
      throw he;
    }
    return data;
  }
  function parseNumList(s) {
    return String(s || "")
      .split(/[,，\s]+/)
      .map((x) => x.trim())
      .filter(Boolean)
      .map((x) => Number(x))
      .filter((n) => !Number.isNaN(n));
  }
  function parseRange(s) {
    const m = String(s || "").match(/(\d{8})\s*[~～\-]\s*(\d{8})/);
    if (m) return { start: Number(m[1]), end: Number(m[2]) };
    return { start: 20200101, end: 20240701 };
  }
  function periodLabel(p) {
    const map = { DAY: "日线", WEEK: "周线", MONTH: "月线", MIN60: "60分钟", DWM: "日周月" };
    if (Array.isArray(p)) return p.map((x) => map[x] || x).join(" / ") || "—";
    return map[p] || p || "—";
  }
  function ruleCategory(r) {
    r = r || {};
    const custom = String(r.category || "").trim();
    if (custom) return custom;
    if (r.source === "user") return "自定义";
    if ((r.name || "").includes("卦") || (r.id || "").includes("gua")) return "卦象";
    if ((r.name || "").includes("735") || (r.name || "").includes("金叉")) return "动量";
    if ((r.name || "").includes("跌") || (r.name || "").includes("涨")) return "反转";
    return "内置";
  }
  function refreshRuleCategoryOptions() {
    const dl = $("ruleCategoryOptions");
    if (!dl) return;
    const set = new Set((AppState.ruleCategories || []).concat((AppState.rules || []).map(ruleCategory)));
    dl.innerHTML = Array.from(set)
      .filter(Boolean)
      .sort()
      .map((c) => '<option value="' + esc(c) + '"></option>')
      .join("");
  }
  function catTagClass(cat) {
    if (cat === "自定义") return "tag-purple";
    if (cat === "动量") return "tag-purple";
    if (cat === "反转") return "tag-orange";
    if (cat === "卦象") return "tag-blue";
    return "tag-blue";
  }
  function ruleStatus(r) {
    r = r || {};
    var av = r.availability;
    if (!av) {
      if (r.min60_day_proxy && r.backtestable) av = "research_proxy";
      else if (r.backtestable) av = "backtestable";
      else if (r.compile_status && r.compile_status !== "ready") av = "unavailable";
      else if (r.failure_reason && !r.min60_day_proxy) av = "unavailable";
      else av = "unavailable";
    }
    if (av === "research_proxy" || (r.min60_day_proxy && r.backtestable))
      return { key: "research_proxy", label: "研究代理", cls: "tag-blue", availability: "research_proxy" };
    if (av === "backtestable" || r.backtestable)
      return { key: "backtestable", label: "可回测", cls: "tag-green", availability: "backtestable" };
    return { key: "unavailable", label: "不可回测", cls: "tag-orange", availability: "unavailable" };
  }
  function ruleDescriptionText(rule) {
    var d = (rule && (rule.description || "")).trim();
    if (d && d.indexOf("User formulas default") < 0) return d;
    var n = (rule && (rule.formal_note || "")).trim();
    if (n && n.indexOf("User formulas default") < 0) return n;
    return "用户自定义公式默认按研究用途管理；完成校验后可用于回测或研究代理。正式使用前请确认公式来源与数据依赖。";
  }
  function ruleDepsHtml(rule) {
    var deps = (rule && rule.dependencies) || [];
    if (!deps.length) {
      return '<span class="tag tag-orange">未声明，建议重新校验公式</span>' +
        '<div class="muted" style="font-size:11px;margin-top:6px;line-height:1.5">' +
        "完成规则校验后将自动提取 OHLCV、成交量、分钟线、停复牌、财务数据和跨周期等依赖。</div>";
    }
    return '<div class="chip-row">' + deps.map(function (d) {
      return '<span class="tag tag-blue">' + esc(d) + "</span>";
    }).join("") + "</div>";
  }

  /* ---------- visual demo data ---------- */
  const DEMO_RULES = [
    {
      id: "rule_000128", name: "动量突破选股 v2", source: "builtin", backtestable: true,
      compile_status: "ready", supported_periods: ["DAY"], description: "基于20日均线突破与成交量放大，捕捉短期动量上升的股票，适用于趋势跟随策略。",
      formal_note: "基于20日均线突破与成交量放大…", failure_reason: null, _demo_ver: "v2.1.0",
      _demo_test: "2026-07-16 14:32", _demo_pass: true,
    },
    {
      id: "rule_000127", name: "均值回归策略 v1", source: "builtin", backtestable: false,
      compile_status: "error", supported_periods: ["DAY"], description: "基于 Z-score 的均值回归信号…",
      failure_reason: "缺少字段", _demo_ver: "v1.3.2", _demo_test: "2026-07-15 10:05", _demo_pass: false,
    },
    {
      id: "rule_000126", name: "财报超预期因子 v1", source: "builtin", backtestable: false,
      compile_status: "ready", supported_periods: ["DAY", "WEEK"], description: "基于净利润超预期构建因子…",
      failure_reason: null, _demo_ver: "v1.0.3", _demo_test: "2026-07-14 18:20", _demo_pass: true,
    },
    {
      id: "rule_000125", name: "换手率异动策略 v1", source: "user", backtestable: true,
      compile_status: "ready", supported_periods: ["DAY"], description: "高换手率异动选股策略…",
      failure_reason: null, _demo_ver: "v1.0.0", _demo_test: null, _demo_pass: null,
    },
    {
      id: "rule_000124", name: "北向资金流向因子 v2", source: "builtin", backtestable: true,
      compile_status: "ready", supported_periods: ["DAY", "WEEK"], description: "基于北向资金净流入构建因子…",
      failure_reason: null, _demo_ver: "v2.0.1", _demo_test: "2026-07-13 16:40", _demo_pass: true,
    },
  ];

  /* ---------- navigation ---------- */
  function switchModule(name, opts) {
    opts = opts || {};
    if (isTaskDetailRoute && name !== "tasks" && !opts.forceLocal) {
      // leave detail page to other modules via full navigation
      location.href = v3MainPath() + "?module=" + encodeURIComponent(name);
      return;
    }
    if (isTaskDetailRoute && name === "tasks" && !opts.forceLocal) {
      location.href = v3MainPath() + "?module=tasks";
      return;
    }
    AppState.activeModule = name;
    // 离开跟踪栏目即停止补算轮询（后台任务继续跑；回来 wbtEnterTrack 会续上）
    if (name !== "track" && typeof wbt !== "undefined" && wbt.bfTimer) {
      clearTimeout(wbt.bfTimer);
      wbt.bfTimer = null;
    }
    try {
      const u = new URL(location.href);
      u.searchParams.set("module", name);
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    } catch (e) {}
    document.querySelectorAll(".nav-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    const el = $("view-" + name);
    if (el) el.classList.add("active");
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (name === "rules") renderRuleCenter();
    if (name === "experiment") {
      renderExperimentCreate();
      try { refreshMdStatus().catch(function () {}); } catch (eMd) {}
      if ($("expPanelMonitor") && $("expPanelMonitor").classList.contains("active")) renderExperimentMonitor();
    }
    if (name === "forecast") {
      renderForecastPage();
    }
    if (name === "track") {
      // 跟踪一级栏目（2026-09-15 从工作台页签提升）：进入即绑定面板按钮 +
      // 懒加载 L0 指标总览，L1/L2 仍是点开才拉。原逻辑挂在 wbSetMode("track")
      // 分支，迁出页签后由本处接管（wbEnsureScreenRules 供规则名映射）。
      try {
        wbtBind();
        wbEnsureScreenRules();
        wbtEnterTrack();
      } catch (eT) {
        console.error("track init", eT);
      }
    }
    if (name === "bagua-query") {
      // PLAN-BAGUA-UX-V1.1 整改：查询卦象视图 = 卦象工作台（内部三页签），
      // 主导航只保留这一个卦象入口（旧 workbench 链接由 getCurrentModule 兼容跳转）
      try { initWorkbenchPage(); } catch (eWb) {
        window._wbInitError = String(eWb && eWb.stack || eWb);
        console.error("workbench init", eWb);
      }
    }
    if (name === "datastore") {
      loadDataStoreStatus();
    }
    if (name === "tasks") {
      bindTaskUi();
      try { bindTaskQueueUi(); } catch (eQ0) {}
      loadTasks()
```

## 跟踪前端逻辑全量 JS 之一（wbt* 三级钻取 + 补算 + 导出，行 15595–16435）

原文件第 15595–16435 行

```javascript
     入口四：跟踪（阶段 3 三级钻取 UI）
     L0 指标总览 → L1 单指标历史周列表 → L2 周明细。
     三级的异步加载统一遵守同一防护模式：请求前自增 token + 捕获条件版本
     （condVersion），响应回来两者任一不等即丢弃——防网络乱序（后发先至）
     与切换期间的迟到响应覆盖用户当前视图。任何请求失败只渲染错误提示，
     绝不让整个页签白屏。统计口径见 docs/plans/auto-screen-track/contract.md
     §3（双口径/分母/null 语义）与 §6（覆盖率低黄色警示，绝不绿色「已结算」）。
     ======================================================================= */
  const wbt = {
    loaded: false, token: 0, condVersion: 0, error: "",
    rules: [], windowWeeks: 12,
    // 口径固定「首日开盘」（可执行口径）：2026-09-15 用户要求隐藏「信号收盘」
    // 切换；产物里的 sig 口径数据仍在（未被删除），将来要恢复只需加回按钮
    sortKey: "win_rate", sortDesc: true, caliber: "exec", search: "",
    level: "l0", ruleId: null, ruleRow: null, weeks: null, week: null,
    weekId: null, weekRule: "", l2From: "l1",
    // L2 列表：默认按本周涨幅降序；展开集合按「代码|规则」去重记录
    l2SortKey: "week_ret", l2SortDesc: true, l2Expanded: new Set(),
    // 补算任务：轮询定时器 + 「本轮是否真有任务在跑」（决定结束后是否刷新）
    bfTimer: null, bfHadRunning: false,
  };
  // 跟踪完成状态中文映射（运行状态，只表示评估是否结束，不代表数据完整）
  const WBT_COMPLETION = {
    complete: "已结算", pending: "待结算", blocked_benchmark: "基准缺失",
    no_trading_week: "整周休市", failed: "失败", no_product: "未生成产物",
    data_version_changed: "数据版本变更",
  };
  // 成交性中文标签：未知/停牌/不可成交如实标注，绝不默认成「正常」（契约 §1）
  const WBT_FILL = {
    ok: "可成交", limit_up_unbuyable: "一字涨停不可买",
    no_bar: "停牌/缺数据", unknown: "判定元数据不足",
  };
  const WBT_RUNKIND = { weekly_chain: "周五链", backfill: "回填", recompute: "重算" };

  function wbtCompletionLabel(c) { return WBT_COMPLETION[c] || (c ? String(c) : "未生成产物"); }
  function wbtFillLabel(f) { return WBT_FILL[f] || (f ? String(f) : "—"); }
  function wbtCaliberIsExec() { return wbt.caliber === "exec"; }
  /** 收益率（小数）→ 百分数两位；null/空/NaN 一律「—」，空仓周绝不当成 0。 */
  function wbtPct(v) {
    if (v == null || v === "" || isNaN(Number(v))) return "—";
    return (Number(v) * 100).toFixed(2) + "%";
  }
  function wbtRaw(v) { return v == null || v === "" ? "—" : String(v); }
  function wbtHtmlTag(text, cls) {
    return '<span class="wb-tag' + (cls ? " " + cls : "") + '">' + esc(text) + "</span>";
  }
  /** 规则名映射（来自筛选规则目录，与规则中心同步）；拿不到就回落到 rule_id。 */
  function wbtRuleName(ruleId) {
    const r = (wbs.rules || []).find((x) => x.id === ruleId);
    return r ? r.name : "";
  }
  function wbtShowLevel(level) {
    wbt.level = level;
    const l0 = $("wbTrackL0"), l1 = $("wbTrackL1"), l2 = $("wbTrackL2");
    if (l0) l0.hidden = level !== "l0";
    if (l1) l1.hidden = level !== "l1";
    if (l2) l2.hidden = level !== "l2";
  }
  function wbtEnterTrack() {
    // 已加载过则保留当前层级，不重复拉取（切栏目回来仍是原来位置）
    if (!wbt.loaded) { wbtLoadOverview(); }
    else {
      if (wbt.level === "l0") wbtRenderOverview();
      if (wbt.level === "l1") wbtRenderRuleWeeks();
      if (wbt.level === "l2") wbtRenderWeekDetail();
    }
    // 补算状态：进来就同步一次（可能有在跑的任务），有在跑才继续轮询
    wbtPollBackfill();
  }
  /** 收益类百分比 + 涨跌色（红涨绿跌，A 股习惯）；null 仍显示「—」。
   *  口径已固定首日开盘，无需再按口径分支取数。 */
  function wbtPctSigned(v) {
    if (v == null || v === "" || isNaN(Number(v))) return "—";
    const n = Number(v);
    const cls = n > 0 ? "wb-pos" : (n < 0 ? "wb-neg" : "");
    const txt = (n * 100).toFixed(2) + "%";
    return cls ? '<span class="' + cls + '">' + txt + "</span>" : txt;
  }

  /* --------------------------- L0 指标总览 --------------------------- */
  async function wbtLoadOverview() {
    // 竞态防护：token 防乱序（后发请求先返回时旧响应作废），condVersion
    // 防切换期间条件变化。响应回来必须双重校验，任一不等直接丢弃。
    const token = ++wbt.token;
    const ver = wbt.condVersion;
    const box = $("wbTrackL0List"), err = $("wbTrackL0Error");
    if (err) err.hidden = true;
    if (box) box.innerHTML = '<p class="wb-muted">跟踪数据加载中…</p>';
    try {
      const j = await api("/api/v1/bagua/track/rules?weeks=" + wbt.windowWeeks);
      if (token !== wbt.token || ver !== wbt.condVersion) return; // 迟到响应丢弃
      wbt.rules = (j && j.rules) || [];
      if (j && j.window_weeks) wbt.windowWeeks = j.window_weeks;
      wbt.error = (j && j.ok !== true) ? "跟踪数据暂不可用" : "";
      wbt.loaded = true;
      wbtRenderOverview();
    } catch (e) {
      if (token !== wbt.token || ver !== wbt.condVersion) return;
      // 失败也不能白屏：清空列表并给出错误 + 空态文案（由渲染函数统一处理）
      wbt.rules = [];
      wbt.error = "跟踪数据加载失败：" + (e && e.message ? e.message : e);
      wbt.loaded = true;
      wbtRenderOverview();
    }
  }

  function wbtSortValue(r, key) {
    if (key === "win_rate") {
      return wbtCaliberIsExec(wbt.caliber)
        ? r.weekly_equal_win_rate_exec : r.weekly_equal_win_rate_sig;
    }
    if (key === "excess") return r.weekly_equal_mean_excess_sig;
    if (key === "total") return r.total_selected;
    return null;
  }

  function wbtRenderOverview() {
    const box = $("wbTrackL0List"), note = $("wbTrackL0Note"), err = $("wbTrackL0Error");
    if (!box) return;
    if (err) {
      if (wbt.error) { err.textContent = wbt.error; err.hidden = false; }
      else err.hidden = true;
    }
    if (note) {
      note.textContent = "近 " + wbt.windowWeeks + " 个信号自然周汇总（周历口径，不是倒找有收益的周）；"
        + "同规则改公式后按指纹分段展示。默认每周等权，收益与胜率口径可切换；"
        + "超额接口仅提供信号口径。";
    }
    if (!wbt.rules.length) {
      // 无任何已发布周是常态（周五链尚未首次运行）：只给引导，不当错误
      box.innerHTML = '<div class="wb-status"><strong>暂无跟踪数据</strong>'
        + '<span class="wb-muted">周五链首次运行后自动生成。</span></div>';
      return;
    }
    const term = (wbt.search || "").trim().toLowerCase();
    let rows = wbt.rules.filter((r) => !term
      || String(r.rule_id).toLowerCase().includes(term)
      || String(wbtRuleName(r.rule_id)).toLowerCase().includes(term));
    const desc = wbt.sortDesc ? -1 : 1;
    rows = rows.slice().sort((a, b) => {
      const va = wbtSortValue(a, wbt.sortKey), vb = wbtSortValue(b, wbt.sortKey);
      // null（空仓/无有效样本）永远排最后，不参与数值比较、不冒充 0
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return (Number(va) - Number(vb)) * desc;
    });
    if (!rows.length) {
      box.innerHTML = '<span class="wb-muted">没有匹配的指标。</span>';
      return;
    }
    const arrow = (k) => wbt.sortKey === k ? (wbt.sortDesc ? " ↓" : " ↑") : "";
    const th = (k, label) =>
      '<th data-wbt-sort="' + k + '" title="点击切换排序">' + esc(label) + arrow(k) + "</th>";
    const exec = wbtCaliberIsExec();
    // 列头必须标口径（用户反馈「看不懂信号收盘/首日开盘」）：切口径时列头跟着变，
    // 不然两张表的数字会看起来矛盾
    const caliberCn = exec ? "首日开盘" : "信号收盘";
    const retKey = exec ? "weekly_equal_mean_ret_exec" : "weekly_equal_mean_ret_sig";
    const vwKey = exec ? "weekly_equal_valid_weeks_exec" : "weekly_equal_valid_weeks_sig";
    // 胜率同样双口径（后端 weekly_equal_win_rate_{sig,exec}）；超额接口只有信号口径
    const winKey = exec ? "weekly_equal_win_rate_exec" : "weekly_equal_win_rate_sig";
    let html = '<table class="wb-table"><thead><tr>'
      + "<th>指标</th><th>本周入选</th><th>跟踪/已结算</th>"
      + th("total", "总票次")
      + th("win_rate", "近" + wbt.windowWeeks + "周胜率(" + caliberCn + ")")
      + '<th title="随上方口径：首日开盘=周一开盘买→周五收盘卖；信号收盘=按信号日收盘买入">'
      + "近" + wbt.windowWeeks + "周平均收益(" + esc(caliberCn) + ")</th>"
      + th("excess", "近" + wbt.windowWeeks + "周平均超额")
      + "<th>有效周数</th><th>操作</th></tr></thead><tbody>";
    html += rows.map((r) => {
      // 名称优先后端直出的规则中心名（r.rule_name），再回落本地映射与裸 id
      const name = r.rule_name || wbtRuleName(r.rule_id) || r.rule_id;
      const fp = String(r.fingerprint || "");
      const insuff = r.insufficient_sample ? ' <span class="wb-warn">样本不足</span>' : "";
      // 其中若干周来自「指定规则补算」（只补了这条规则）：用 ⓘ 提示而不占版面，
      // 避免用户把这几周当成全市场全规则周去比较
      const subsetN = Number(r.subset_weeks || 0);
      const subsetMark = subsetN
        ? ' <span class="wb-muted" title="跟踪的 ' + subsetN + ' 周来自「指定规则补算」'
          + '（只补了这条规则；那几周其他规则没有名单与收益）">ⓘ</span>'
        : "";
      // 本周入选：点击直达该周 L2 明细
      const latestBtn = r.latest_week != null
        ? '<button type="button" class="wb-secondary" data-wbt-week="' + esc(String(r.latest_week))
          + '" data-wbt-week-rule="' + esc(r.rule_id) + '" title="查看该周明细">'
          + esc(String(r.latest_selected != null ? r.latest_selected : "—")) + "</button>"
        : '<span class="wb-muted">—</span>';
      return "<tr>"
        + "<td><strong>" + esc(name) + "</strong>"
        + (name !== r.rule_id ? ' <span class="wb-muted wb-mono">' + esc(r.rule_id) + "</span>" : "")
        + (fp ? '<div class="wb-muted">版本 ' + esc(fp.slice(0, 6)) + "</div>" : "")
        + "</td>"
        + "<td>" + latestBtn + "</td>"
        + '<td class="wb-mono">' + esc(String(r.tracked_weeks != null ? r.tracked_weeks : 0))
        + " / " + esc(String(r.settled_weeks != null ? r.settled_weeks : 0)) + subsetMark + "</td>"
        + '<td class="wb-mono">' + esc(String(r.total_selected != null ? r.total_selected : 0)) + insuff + "</td>"
        + '<td class="wb-mono">' + wbtPct(r[winKey]) + "</td>"
        + '<td class="wb-mono">' + wbtPctSigned(r[retKey]) + "</td>"
        + '<td class="wb-mono">' + wbtPctSigned(r.weekly_equal_mean_excess_sig) + "</td>"
        + '<td class="wb-mono">' + wbtRaw(r[vwKey]) + "</td>"
        + '<td><button type="button" class="wb-secondary" data-wbt-l1="' + esc(r.rule_id)
        + '" data-wbt-l1-fp="' + esc(fp) + '">查看历史 →</button></td></tr>';
    }).join("");
    html += "</tbody></table>";
    box.innerHTML = html;
    wbtBindOverview();
  }

  function wbtBindOverview() {
    const box = $("wbTrackL0List");
    if (!box) return;
    box.querySelectorAll("[data-wbt-sort]").forEach((el) => {
      el.onclick = () => {
        const k = el.dataset.wbtSort;
        if (wbt.sortKey === k) wbt.sortDesc = !wbt.sortDesc;
        else { wbt.sortKey = k; wbt.sortDesc = true; }
        wbtRenderOverview();
      };
    });
    box.querySelectorAll("[data-wbt-l1]").forEach((el) => {
      el.onclick = () => {
        const rid = el.dataset.wbtL1, fp = el.dataset.wbtL1Fp;
        const row = wbt.rules.find((r) =>
          r.rule_id === rid && String(r.fingerprint || "") === String(fp || ""));
        wbtOpenRuleWeeks(rid, row || null);
      };
    });
    box.querySelectorAll("[data-wbt-week]").forEach((el) => {
      el.onclick = () => wbtOpenWeek(el.dataset.wbtWeek, el.dataset.wbtWeekRule);
    });
  }

  /* --------------------- L1 单指标历史周列表 --------------------- */
  async function wbtOpenRuleWeeks(ruleId, row) {
    const token = ++wbt.token;
    const ver = wbt.condVersion;
    wbt.ruleId = ruleId;
    wbt.ruleRow = row;
    wbt.weeks = null;
    wbtShowLevel("l1");
    const list = $("wbTrackL1List"), err = $("wbTrackL1Error");
    if (err) err.hidden = true;
    if (list) list.innerHTML = '<p class="wb-muted">周列表加载中…</p>';
    wbtRenderL1Summary();
    try {
      // 带上 L0 行的指纹：同公式可能对应多个 rule_id（tn6_ 与其配对源 txt_、
      // 改名前后的 user_ 规则），L0 已按指纹归并成一行，L1 必须按同一指纹组
      // 取数——否则「新导入的 id + 只有兄弟 id 的老周」会查不到任何周。
      let url = "/api/v1/bagua/track/rules/"
        + encodeURIComponent(ruleId) + "/weeks?weeks=26";
      const fpOfRow = row && row.fingerprint ? String(row.fingerprint) : "";
      if (fpOfRow) url += "&fingerprint=" + encodeURIComponent(fpOfRow);
      const j = await api(url);
      if (token !== wbt.token || ver !== wbt.condVersion) return; // 迟到响应丢弃
      wbt.weeks = (j && j.weeks) || [];
      wbtRenderRuleWeeks();
    } catch (e) {
      if (token !== wbt.token || ver !== wbt.condVersion) return;
      wbt.weeks = [];
      if (list) list.innerHTML = "";
      if (err) { err.textContent = "周列表加载失败：" + (e && e.message ? e.message : e); err.hidden = false; }
    }
  }

  /** L1 顶部汇总：直接复用 L0 收集到的该指标行数据，避免重复请求。 */
  function wbtRenderL1Summary() {
    const box = $("wbTrackL1Summary");
    if (!box) return;
    const r = wbt.ruleRow;
    if (!r) { box.innerHTML = '<span>指标 <b>' + esc(wbt.ruleId || "—") + "</b></span>"; return; }
    const fp = String(r.fingerprint || "");
    box.innerHTML = "<span>指标 <b>" + esc(wbtRuleName(r.rule_id) || r.rule_id) + "</b></span>"
      + (fp ? '<span class="wb-muted">版本 <span class="wb-mono">' + esc(fp.slice(0, 6)) + "</span></span>" : "")
      + "<span>跟踪周 <b>" + esc(String(r.tracked_weeks || 0)) + "</b></span>"
      + "<span>已结算 <b>" + esc(String(r.settled_weeks || 0)) + "</b></span>"
      + "<span>近" + wbt.windowWeeks + "周胜率 <b>" + wbtPct(r.weekly_equal_win_rate_sig) + "</b></span>"
      + "<span>平均收益 <b>" + wbtPctSigned(wbtCaliberIsExec()
          ? r.weekly_equal_mean_ret_exec : r.weekly_equal_mean_ret_sig) + "</b></span>"
      + "<span>平均超额 <b>" + wbtPctSigned(r.weekly_equal_mean_excess_sig) + "</b></span>";
  }

  function wbtRenderRuleWeeks() {
    const box = $("wbTrackL1List");
    if (!box) return;
    const weeks = wbt.weeks || [];
    if (!weeks.length) {
      box.innerHTML = '<div class="wb-status"><strong>该指标暂无跟踪周</strong>'
        + '<span class="wb-muted">窗口内没有该规则版本的已发布快照记录（可能规则刚上线或尚无发布周）。</span></div>';
      return;
    }
    // 口径固定「首日开盘」：列头与取值必须是同一口径（原先列头随口径切换、
    // 取值却固定用信号收盘字段，列头写「首日开盘」而数字是信号口径）
    const l1Ret = "平均收益(首日开盘)";
    const l1Win = "胜率(首日开盘)";
    let html = '<table class="wb-table"><thead><tr>'
      + "<th>信号日（周）</th><th>入选</th><th>" + esc(l1Ret) + "</th><th>" + esc(l1Win) + "</th>"
      + '<th title="周内最高价相对信号日收盘的均值（与口径无关）">平均最大涨幅</th>'
      + "<th>平均回吐</th>"
      + '<th title="相对沪深300；接口只提供信号口径">平均超额</th>'
      + "<th>状态</th><th>操作</th>"
      + "</tr></thead><tbody>";
    html += weeks.map((w) => {
      const a = w.aggregate || null;
      // 聚合字段以实现为准：接口未提供 mean_max_gain_sig，缺失时显示 —，不回退成 0；
      // 平均回吐实际字段名为 mean_giveback（旧文档写 mean_giveback_sig，兼容两者）
      const maxGain = a ? (a.mean_max_gain_sig != null ? a.mean_max_gain_sig : a.mean_max_gain) : null;
      const giveback = a ? (a.mean_giveback_sig != null ? a.mean_giveback_sig : a.mean_giveback) : null;
      const runTag = w.run_kind === "backfill" ? " " + wbtHtmlTag("回填", "running") : "";
      // 「指定规则补算」周（子集快照）：该周只有部分规则跑过筛选，必须标注，
      // 否则这一行会被读成那周（全市场全规则）的完整结果
      const scopeTag = w.rules_scope === "subset"
        ? " " + wbtHtmlTag("指定规则补算", "running") : "";
      return "<tr>"
        + '<td><strong class="wb-mono">' + esc(wbFmtDate(w.week_id)) + "</strong>"
        + '<div class="wb-muted wb-mono">' + esc(w.week_id != null ? String(w.week_id) : "") + runTag + scopeTag + "</div></td>"
        + '<td class="wb-mono">' + esc(String(w.selected_count != null ? w.selected_count : 0)) + "</td>"
        + '<td class="wb-mono">' + wbtPctSigned(a ? a.mean_ret_close_exec : null) + "</td>"
        + '<td class="wb-mono">' + wbtPct(a ? a.win_rate_exec : null) + "</td>"
        + '<td class="wb-mono">' + wbtPct(maxGain) + "</td>"
        + '<td class="wb-mono">' + wbtPct(giveback) + "</td>"
        + '<td class="wb-mono">' + wbtPctSigned(a ? a.mean_excess_sig : null) + "</td>"
        + "<td>" + wbtHtmlTag(wbtCompletionLabel(w.completion),
            w.completion === "complete" ? "" : "running") + "</td>"
        + '<td><button type="button" class="wb-secondary" data-wbt-week="' + esc(String(w.week_id))
        + '" data-wbt-week-rule="' + esc(wbt.ruleId || "") + '">周明细 →</button></td></tr>';
    }).join("");
    html += "</tbody></table>"
      + '<p class="wb-muted">「已结算」只表示本轮评估已结束，不代表数据完整；覆盖率请进入周明细查看。</p>';
    box.innerHTML = html;
    const list = $("wbTrackL1List");
    if (list) list.querySelectorAll("[data-wbt-week]").forEach((el) => {
      el.onclick = () => wbtOpenWeek(el.dataset.wbtWeek, el.dataset.wbtWeekRule);
    });
  }

  /* --------------------------- L2 周明细 --------------------------- */
  async function wbtOpenWeek(weekId, ruleId) {
    const token = ++wbt.token;
    const ver = wbt.condVersion;
    wbt.l2From = wbt.level; // 记住来源层级：L0 直达或 L1 进入，返回按钮文案/目标不同
    wbt.week = null;
    wbt.l2Expanded.clear(); // 换周必须清展开集合——键是 code|rule_id，跨周同票同规则会误以为用户展开过
    wbt.weekId = weekId;
    wbt.weekRule = ruleId || "";
    wbtShowLevel("l2");
    const sum = $("wbTrackL2Summary"), list = $("wbTrackL2List"), err = $("wbTrackL2Error");
    if (err) err.hidden = true;
    if (sum) sum.innerHTML = '<span class="wb-muted">周明细加载中…</span>';
    if (list) list.innerHTML = "";
    try {
      let url = "/api/v1/bagua/track/weeks/" + encodeURIComponent(String(weekId));
      if (ruleId) url += "?rule_id=" + encodeURIComponent(ruleId);
      const j = await api(url);
      if (token !== wbt.token || ver !== wbt.condVersion) return; // 迟到响应丢弃
      wbt.week = j || null;
      wbtRenderWeekDetail();
    } catch (e) {
      if (token !== wbt.token || ver !== wbt.condVersion) return;
      if (sum) sum.innerHTML = "";
      if (err) { err.textContent = "周明细加载失败：" + (e && e.message ? e.message : e); err.hidden = false; }
    }
  }

  /**
   * 覆盖率展示（分口径）：分母为 0 时接口返回 null → 「—」。
   * 覆盖率 < 0.9 一律黄色警示；达标也只用中性文字，绝不渲染绿色「已结算」，
   * 防止把「评估结束」误读成「数据完整」（契约 §1/§6）。
   */
  function wbtCoverageHtml(cov) {
    const items = [
      ["信号收盘", cov ? cov.signal_close : null],
      ["首日开盘", cov ? cov.week_first_open : null],
      ["超额", cov ? cov.excess : null],
    ];
    return items.map((it) => {
      const label = it[0], v = it[1];
      if (v == null || v === "" || isNaN(Number(v))) {
        return '<span class="wb-muted">' + esc(label) + " —</span>";
      }
      const low = Number(v) < 0.9;
      return '<span class="' + (low ? "wb-warn" : "") + '">' + esc(label) + " "
        + esc((Number(v) * 100).toFixed(2)) + "%" + (low ? " ⚠覆盖率低" : "") + "</span>";
    }).join("");
  }
  function wbtWeekday(dateInt) {
    const s = String(dateInt == null ? "" : dateInt);
    if (s.length !== 8) return 0;
    const dt = new Date(Number(s.slice(0, 4)), Number(s.slice(4, 6)) - 1, Number(s.slice(6, 8)));
    const wd = dt.getDay();
    return wd === 0 ? 7 : wd;
  }
    /** 星期中文（1=周一…7=周日）；非法日期返回空串（不猜、不填占位）。 */
  function wbtWeekdayCn(dateInt) {
    const wd = wbtWeekday(dateInt);
    return wd >= 1 && wd <= 7 ? "周" + "一二三四五六日"[wd - 1] : "";
  }
  /** 数值化：null/空/非数 → null（排序时缺值下沉，绝不参与大小比较）。 */
  function wbtNum(v) {
    return v == null || v === "" || isNaN(Number(v)) ? null : Number(v);
  }
  /** 展开行的键：同票多规则必须各展开各的。 */
  function wbtRowKey(r) { return String(r.code || "") + "|" + String(r.rule_id || ""); }

  /** L2 默认列（用户要求：代码/名称/开盘价/周五收盘价/最高涨幅/本周涨幅）。
   *  逐日收益、回撤、超额等诊断列收进「详情」行——18 列平铺太密，
   *  列表页只留看得懂的少数列。 */
  const WBT_L2_COLS = [
    { key: "code", label: "代码" },
    { key: "name", label: "名称" },
    { key: "open", label: "周一开盘价", title: "跟踪周首日开盘价——「首日开盘」口径的买入基准" },
    { key: "close_end", label: "周五收盘价", title: "跟踪周最后交易日收盘价（节假日短周即当周最后一天）" },
    { key: "max_gain", label: "最高涨幅", title: "周内最高价相对信号日收盘价；下方小字为见顶在周几" },
    { key: "week_ret", label: "本周涨幅", title: "随上方口径：首日开盘=周一开盘买→周五收盘卖；信号收盘=按信号日收盘买入" },
    { key: "fill", label: "可成交性", title: "一字涨停买不进的票不进「首日开盘」口径统计" },
    { key: "status", label: "状态" },
    { key: "op", label: "操作" },
  ];

  function wbtL2SortValue(r, key) {
    if (key === "code") return String(r.code || "");
    if (key === "name") return String(r.name || "");
    if (key === "open") return wbtNum(r.entry_open_week);
    if (key === "close_end") return wbtNum(r.close_week_end);
    if (key === "max_gain") return wbtNum(r.max_gain_sig);
    if (key === "week_ret") return wbtNum(wbtCaliberIsExec() ? r.ret_close_exec : r.ret_close_sig);
    return null;
  }

  /** L2 行排序：数字键升/降序、文本键按中文序；缺值恒沉底。 */
  function wbtSortL2Rows(rows) {
    const key = wbt.l2SortKey || "week_ret";
    const dir = wbt.l2SortDesc ? -1 : 1;
    return rows.slice().sort((a, b) => {
      const va = wbtL2SortValue(a, key), vb = wbtL2SortValue(b, key);
      if (va == null && vb == null) return String(a.code || "").localeCompare(String(b.code || ""));
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string" || typeof vb === "string") {
        return String(va).localeCompare(String(vb)) * dir;
      }
      return (va - vb) * dir;
    });
  }

  function wbtPendingHtml(picks) {
    if (!picks || !picks.length) return "";
    return '<div class="wb-block"><h3 class="wb-title"><strong>待结算（' + picks.length + " 只）</strong>"
      + '<span class="wb-muted" style="font-size:12px">快照已命中、跟踪尚未结算：每周五晚自动结算上一信号周，'
      + '急用可用「补算指定周」立即结算</span></h3>'
      + '<div class="wb-chips">' + picks.map((p) =>
          '<span class="wb-chip">' + esc(p.name ? (p.code_disp || p.code) + " " + p.name : (p.code_disp || p.code))
          + '<span class="wb-muted">' + esc(wbtRuleName(p.rule_id) || p.rule_id || "")
          + (p.close != null ? " · 命中收盘 " + esc(String(p.close)) : "") + "</span>"
          + wbtHtmlTag("待结算", "running") + "</span>").join("")
      + "</div></div>";
  }


    /** 详情行：逐日收益（按跟踪周**实际交易日**渲染，不写死周一~周五）+
   *  入场/回撤/双口径/超额等诊断字段。短周、停牌日都按真实日期显示。 */
  function wbtRowDetailHtml(r, weekDates) {
    const byDate = {};
    (r.daily || []).forEach((d) => { byDate[Number(d.date)] = d; });
    let dates = (weekDates || []).slice();
    if (!dates.length) dates = Object.keys(byDate).map(Number).sort((a, b) => a - b);
    const exec = wbtCaliberIsExec();
    const dayCells = dates.map((d) => {
      const dd = byDate[Number(d)];
      const head = esc(wbFmtDate(d)) + '<div class="wb-muted">' + esc(wbtWeekdayCn(d)) + "</div>";
      if (!dd) return "<td>" + head + '<div class="wb-muted">停牌/缺数据</div></td>';
      return "<td>" + head
        + '<div class="wb-mono">' + wbtPct(dd.ret_vs_signal_close) + "</div>"
        + '<div class="wb-muted wb-mono">收盘 ' + esc(wbtRaw(dd.close)) + "</div></td>";
    }).join("");
    const bench = exec ? r.bench_ret_exec : r.bench_ret_sig;
    const excess = exec ? r.excess_exec : r.excess_sig;
    return '<div class="wb-block"><div class="wb-stats">'
      + "<span>入场价（信号日收盘）<b>" + esc(wbtRaw(r.entry_close_signal)) + "</b></span>"
      + "<span>首日开盘 " + esc(wbtRaw(r.entry_open_week)) + "</span>"
      + "<span>周五收盘 " + esc(wbtRaw(r.close_week_end)) + "</span>"
      + "<span>周内最低相对入场 " + wbtPct(r.min_low_ret_sig) + "</span>"
      + "<span>峰谷回撤（按收盘）" + wbtPct(r.drawdown_close_sig) + "</span>"
      + "<span>周五收益(信号) " + wbtPct(r.ret_close_sig) + "</span>"
      + "<span>周五收益(开盘) " + wbtPct(r.ret_close_exec) + "</span>"
      + "</div>"
      + (r.theoretical_open_ret != null
          ? '<p class="wb-muted" style="font-size:12px">理论开盘收益 ' + esc(wbtPct(r.theoretical_open_ret))
            + "（首日无 K 线或买不进时的参考值，未计入成交口径统计）</p>" : "")
      + '<div class="wb-stats"><span>沪深300 ' + esc(wbtPct(bench)) + "</span>"
      + "<span>超额（" + esc(exec ? "首日开盘" : "信号收盘") + "）" + esc(wbtPct(excess)) + "</span></div>"
      + '<div class="wb-scroll"><table class="wb-table"><thead><tr>'
      + "<th>逐日（相对信号日收盘）</th>" + dayCells + "</tr></thead></table></div>"
      + "</div>";
  }

  function wbtRenderWeekDetail() {
    const sum = $("wbTrackL2Summary"), box = $("wbTrackL2List");
    if (!sum || !box) return;
    const j = wbt.week;
    if (!j) { sum.innerHTML = ""; box.innerHTML = ""; return; }
    const isBackfill = !!j.backfill || String(j.run_kind) === "backfill";
    const isSubsetScope = String(j.rules_scope || "") === "subset";
    const back = $("wbTrackL2Back");
    if (back) back.textContent = wbt.l2From === "l0" ? "← 返回指标总览" : "← 返回周列表";
    const tw = Array.isArray(j.track_week) && j.track_week.length === 2 ? j.track_week : null;
    const weekDates = (j.track_week_dates || []).map(Number).filter((d) => d > 0);
    let head = "<div class='wb-row'><strong>信号周 " + esc(wbFmtDate(j.week_id)) + "</strong>"
      + '<span class="wb-muted wb-mono">' + esc(j.week_id != null ? String(j.week_id) : "") + "</span>"
      + (wbt.weekRule ? '<span class="wb-muted">规则 ' + esc(wbtRuleName(wbt.weekRule) || wbt.weekRule) + "</span>" : "")
      + "</div>"
      + '<div class="wb-row">'
      + wbtHtmlTag(wbtCompletionLabel(j.completion), j.completion === "complete" ? "" : "running")
      + wbtHtmlTag("运行：" + (WBT_RUNKIND[j.run_kind] || j.run_kind || "—"))
      + (isBackfill ? wbtHtmlTag("回填", "running") : "")
      + (isSubsetScope ? wbtHtmlTag("指定规则补算", "running") : "")
      + (tw ? '<span class="wb-muted">跟踪周 ' + esc(wbFmtDate(tw[0])) + " ~ " + esc(wbFmtDate(tw[1]))
            + "（" + weekDates.length + " 个交易日" + (j.short_week ? "，短周" : "") + "）</span>" : "")
      + (j.snapshot_id ? '<span class="wb-muted wb-mono">快照 ' + esc(String(j.snapshot_id)) + "</span>" : "")
      + "</div>"
      + '<div class="wb-stats">' + wbtCoverageHtml(j.coverage) + "</div>";
    // 回填数据必须显著位置展示免责全文（契约 §9：UI/导出必带）
    if (j.backfill_notice) {
      head += '<div class="wb-warnbox"><strong class="wb-warn">回填数据提示</strong>'
        + '<span class="wb-warn">' + esc(j.backfill_notice) + "</span></div>";
    }
    // 指定规则补算：该周只有这几条规则跑过筛选，其余规则当周**没有名单**
    // （不是"当周空仓"）——文案来自后端单一来源（契约 §6），前端不另写一套
    if (isSubsetScope && j.scope_notice) {
      head += '<div class="wb-warnbox"><strong class="wb-warn">指定规则补算</strong>'
        + '<span class="wb-warn">' + esc(j.scope_notice) + "</span></div>";
    }
    // 窗口未结束：说明「现在看到的是进行中的浮动值」，避免被误读成已结算收益
    if (String(j.completion) === "pending" || j.window_ended === false) {
      head += '<div class="wb-warnbox"><strong class="wb-warn">进行中（未结算）</strong>'
        + '<span class="wb-warn">跟踪周尚未走完或行情未到齐，下方收益为截至最新数据日的浮动值，'
        + "每周五结算后自动更新为最终值。</span></div>";
    }
    sum.innerHTML = head;
    const rows = wbtSortL2Rows(j.rows || []);
    if (!rows.length) {
      box.innerHTML = '<div class="wb-status"><strong>该周暂无已结算明细</strong>'
        + '<span class="wb-muted">跟踪产物尚未生成：每周五晚自动结算上一信号周，'
        + "也可用下方「补算指定周」立即结算。</span></div>"
        + wbtPendingHtml(j.pending_picks);
      wbtBindWeekDetail(j);
      return;
    }
    const retLabel = wbtCaliberIsExec() ? "本周涨幅(首日开盘)" : "本周涨幅(信号收盘)";
    let html = '<table class="wb-table"><thead><tr>';
    WBT_L2_COLS.forEach((c) => {
      const label = c.key === "week_ret" ? retLabel : c.label;
      const sortable = ["code", "name", "open", "close_end", "max_gain", "week_ret"].indexOf(c.key) >= 0;
      const arrow = wbt.l2SortKey === c.key ? (wbt.l2SortDesc ? " ▾" : " ▴") : "";
      html += "<th"
        + (sortable ? ' data-wbt-l2sort="' + esc(c.key) + '" style="cursor:pointer"' : "")
        + (c.title ? ' title="' + esc(c.title) + '"' : "")
        + ">" + esc(label + arrow) + "</th>";
    });
    html += "</tr></thead><tbody>";
    html += rows.map((r) => {
      const key = wbtRowKey(r);
      const weekRet = wbtCaliberIsExec() ? r.ret_close_exec : r.ret_close_sig;
      const unbuyable = String(r.fill_status) === "limit_up_unbuyable";
      const mg = r.max_gain_sig != null
        ? wbtPct(r.max_gain_sig) + '<div class="wb-muted">'
          + esc(wbtWeekdayCn(r.max_gain_sig_date) || "—") + "</div>"
        : "—";
      const statusLabel = r.status === "ok"
        ? (String(j.completion) === "pending" ? "进行中" : "已结算")
        : r.status === "no_bar" ? "无行情（停牌/缺数据）"
        : r.status === "pending" ? "进行中" : (r.status || "—");
      const nameCell = r.name ? esc(String(r.name)) : '<span class="wb-muted">—</span>';
      return "<tr>"
        + "<td><strong>" + esc(r.code_disp || r.code) + "</strong></td>"
        + "<td>" + nameCell + "</td>"
        + '<td class="wb-mono">' + wbtRaw(r.entry_open_week) + "</td>"
        + '<td class="wb-mono">' + wbtRaw(r.close_week_end) + "</td>"
        + '<td class="wb-mono">' + mg + "</td>"
        + '<td class="wb-mono">' + wbtPctSigned(weekRet) + "</td>"
        + "<td>" + (unbuyable
            ? wbtHtmlTag(wbtFillLabel(r.fill_status), "running")
            : esc(wbtFillLabel(r.fill_status))) + "</td>"
        + "<td>" + esc(statusLabel) + "</td>"
        + "<td>"
        + '<button type="button" class="wb-secondary" data-wbt-expand="' + esc(key) + '">'
        + (wbt.l2Expanded.has(key) ? "收起" : "详情") + "</button>"
        + '<button type="button" class="wb-secondary" data-wbt-q="' + esc(r.code) + '">查卦象</button>'
        + "</td></tr>"
        + (wbt.l2Expanded.has(key)
            ? '<tr class="wb-l2-detail"><td colspan="' + WBT_L2_COLS.length + '">'
              + wbtRowDetailHtml(r, weekDates) + "</td></tr>"
            : "");
    }).join("");
    html += "</tbody></table>";
    html += '<p class="wb-muted">点「详情」展开该票的逐日收益、回撤与超额；'
      + "「最高涨幅」是周内最高价相对信号日收盘，见顶星期标在数值下方。</p>";
    html += wbtPendingHtml(j.pending_picks);
    box.innerHTML = html;
    wbtBindWeekDetail(j);
  }


    function wbtBindWeekDetail(j) {
    const box = $("wbTrackL2List");
    if (!box) return;
    const weekDate = j && j.week_id != null ? j.week_id : wbt.weekId;
    box.querySelectorAll("[data-wbt-q]").forEach((btnEl) => {
      btnEl.onclick = () => wbtQueryFromTrack(btnEl.dataset.wbtQ, weekDate);
    });
    // 展开/收起：只重绘明细区，不重发请求（数据已在 wbt.week 里）
    box.querySelectorAll("[data-wbt-expand]").forEach((btnEl) => {
      btnEl.onclick = () => {
        const k = btnEl.dataset.wbtExpand;
        if (wbt.l2Expanded.has(k)) wbt.l2Expanded.delete(k);
        else wbt.l2Expanded.add(k);
        wbtRenderWeekDetail();
      };
    });
    // 表头排序：切换键/方向后本地重排（口径切换复用同一套排序值）
    box.querySelectorAll("[data-wbt-l2sort]").forEach((th) => {
      th.onclick = () => {
        const k = th.dataset.wbtL2sort;
        if (wbt.l2SortKey === k) wbt.l2SortDesc = !wbt.l2SortDesc;
        else { wbt.l2SortKey = k; wbt.l2SortDesc = true; }
        wbtRenderWeekDetail();
      };
    });
  }


  /** 跨页签跳转：把该票 + 该周信号日带入查卦象（与筛选命中同款交接）。 */
  function wbtQueryFromTrack(code, weekDate) {
    wbq.picked = [{ id: code, code: code.split(".").pop(), name: code, type: "STK" }];
    wbq.period = "DAY";
    wbq.price = "tushare_qfq";
    wbq.timeMode = "custom";
    const perBtn = document.querySelector('[data-wb-period="DAY"]');
    if (perBtn) perBtn.click();
    const timeSel = $("wbQTime");
    if (timeSel) { timeSel.value = "custom"; timeSel.dispatchEvent(new Event("change")); }
    const custom = $("wbQCustom");
    if (custom) custom.value = wbFmtDate(weekDate);
    const priceSel = $("wbQPrice");
    if (priceSel) priceSel.value = "tushare_qfq";
    wbq.optionsCache = {};
    wbq.snapshot = null;
    wbRefreshOptions();
    wbQueryChanged(false);
    wbtShowLevel("l0"); // 离开跟踪栏目时把层级复位，回来仍是总览
    // 跟踪已是一级栏目：先切回卦象视图（含工作台初始化），再切「查卦象」页签
    switchModule("bagua-query");
    wbSetMode("query");
    toast("已带入跟踪明细条件（" + wbFmtDate(weekDate) + " · Tushare 前复权），请重新查询。");
  }

  /* ---------- 补算指定历史周（异步 CLI 子进程） ---------- */

  // 单次指定规则补算的规则数上限（与后端 MAX_SUBSET_RULES 一致；前端先挡一道，
  // 免得用户选十几条再被 400 顶回来）
  const WBT_BF_MAX_RULES = 5;

  /** 提交补算（payload：{week} / {weeks_back} / {week, rule_ids}），提交后开始轮询。 */
  async function wbtRunBackfill(payload) {
    try {
      const j = await api("/api/v1/bagua/track/backfill", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast((j && j.message) || "已提交补算任务");
      wbtPollBackfill();
    } catch (e) {
      toast("补算提交失败：" + (e && e.message ? e.message : e));
    }
  }

  /** 规则勾选状态：与筛选页同款——状态独立于 DOM（检索过滤不丢已选）。 */
  if (!wbt.bfChecked) wbt.bfChecked = new Set();
  if (!wbt.bfRules) wbt.bfRules = [];

  /** 规则列表：首次展开补算面板时懒加载一次（不占进入跟踪页的开销）。 */
  async function wbtLoadBackfillRules() {
    if (wbt.bfRulesLoaded) return;
    wbt.bfRulesLoaded = true;
    const box = $("wbTrackBfRuleList");
    try {
      const j = await api("/api/v1/bagua/screen/rules");
      // 只列可执行规则：不可执行的提交会被后端 400 拒绝，列出来只会误导
      wbt.bfRules = ((j && j.rules) || []).filter((r) => r && r.executable);
      // 目录就位后剔除失效勾选（规则被删还保持勾选，提交时会 400）
      const live = new Set(wbt.bfRules.map((r) => r.id));
      Array.from(wbt.bfChecked).forEach((id) => {
        if (!live.has(id)) wbt.bfChecked.delete(id);
      });
    } catch (e) {
      wbt.bfRulesLoaded = false; // 失败允许下次展开重试
      wbt.bfRules = [];
      if (box) box.innerHTML = '<span class="wb-error">规则列表加载失败：' + esc(e && e.message ? e.message : e) + "</span>";
      return;
    }
    wbtRenderBackfillRules();
  }

  /** 渲染规则复选列表（检索过滤；选中状态以 Set 为准，过滤只改可见行）。 */
  function wbtRenderBackfillRules() {
    const box = $("wbTrackBfRuleList");
    if (!box) return;
    if (!wbt.bfRules.length) {
      box.innerHTML = '<span class="wb-muted">没有可执行规则</span>';
      return;
    }
    const term = (($("wbTrackBfRuleSearch") || {}).value || "").trim().toLowerCase();
    const rows = wbt.bfRules.filter((r) =>
      !term || String(r.name).toLowerCase().includes(term) || String(r.id).toLowerCase().includes(term));
    box.innerHTML = rows.map((r) => {
      // 同名规则（如两条「735金叉及趋势」）靠来源区分，前端不按名去重
      const nm = (r.name || r.id) + (r.source ? " [" + r.source + "]" : "");
      return '<label class="wb-check"><input type="checkbox" data-wb-bf-rule="' + esc(r.id) + '"'
        + (wbt.bfChecked.has(r.id) ? " checked" : "") + "><span>" + esc(nm) + "</span></label>";
    }).join("") || '<span class="wb-muted">没有匹配的规则（已选 ' + wbt.bfChecked.size + " 条仍在生效）</span>";
    box.querySelectorAll("input[data-wb-bf-rule]").forEach((x) => {
      x.onchange = () => {
        if (x.checked && wbt.bfChecked.size >= WBT_BF_MAX_RULES) {
          x.checked = false; // 当场拦住，别等提交时被 400 顶回来
          toast("指定规则最多 " + WBT_BF_MAX_RULES + " 条（更多请整周回填）");
          return;
        }
        if (x.checked) wbt.bfChecked.add(x.dataset.wbBfRule);
        else wbt.bfChecked.delete(x.dataset.wbBfRule);
        wbtRenderBackfillRuleState();
      };
    });
    wbtRenderBackfillRuleState();
  }

  /** 已选状态可视化：chips + 计数 + 折叠标题三处同步——任何时候用户都看得懂
   *  当前是「全部规则」还是「指定这几条」。 */
  function wbtRenderBackfillRuleState() {
    const n = wbt.bfChecked.size;
    const cnt = $("wbTrackBfRuleCount");
    if (cnt) cnt.textContent = "已选 " + n + " 条";
    const names = Array.from(wbt.bfChecked).map((id) => {
      const r = wbt.bfRules.find((x) => x.id === id);
      return r ? (r.name || id) : id;
    });
    const chips = $("wbTrackBfRuleChips");
    if (chips) {
      chips.innerHTML = names.length
        ? names.map((nm) => '<span class="wb-chip">' + esc(nm) + "</span>").join("")
        : "";
    }
    const sum = $("wbTrackBfRuleSummary");
    if (sum) {
      if (!n) {
        sum.textContent = "规则：全部规则（点开可只补指定规则，单条快约 6~10 倍）";
      } else {
        sum.textContent = "规则：已选 " + n + " 条（"
          + names.slice(0, 3).join("、") + (n > 3 ? " 等" : "")
          + "）——补算只跑这几条";
      }
    }
  }

  /** 当前选中的规则 ID（空数组 = 全部规则 = 整周回填）。 */
  function wbtBackfillPickedRules() {
    return Array.from(wbt.bfChecked);
  }

  const WBT_BF_STATUS = {
    queued: "排队中", running: "进行中", done: "已完成",
    retryable: "稍后自动重试", failed: "失败",
  };

  function wbtRenderBackfillJobs(body) {
    const box = $("wbTrackBfJobs");
    if (!box) return;
    const jobs = (body && body.jobs) || [];
    if (!jobs.length) { box.innerHTML = '<span class="wb-muted">暂无补算任务</span>'; return; }
    box.innerHTML = jobs.slice(0, 5).map((j) => {
      const picked = (j.rule_ids || []).length;
      const target = j.week
        ? (wbFmtDate(j.week) + (picked ? "（指定 " + picked + " 条规则）" : ""))
        : ("最近 " + esc(String(j.weeks_back || 0)) + " 周");
      let note = "";
      if (j.status === "retryable") note = "（重任务锁被占用/数据版本变化，已记待办，服务端稍后自动重试）";
      if (j.status === "failed") note = "（退出码 " + esc(String(j.exit_code)) + "，见服务端日志）";
      const cls = j.status === "done" ? "" : " running";
      return '<div class="wb-job"><span' + (cls ? ' class="wb-warn"' : "")
        + ">" + esc(WBT_BF_STATUS[j.status] || j.status) + "</span>"
        + "<span>" + esc(String(target)) + "</span>"
        + (note ? '<span class="wb-muted">' + esc(note) + "</span>" : "")
        + "</div>";
    }).join("");
  }

  /** 轮询补算状态；有任务在跑才继续轮询，全部结束后刷新一次 L0
   *  （补算成功的周会出现在指标列表里，用户直接点进去看）。 */
  async function wbtPollBackfill() {
    if (wbt.bfTimer) return; // 已在轮询
    const tick = async () => {
      let body = null;
      try { body = await api("/api/v1/bagua/track/backfill/status"); } catch (e) { body = null; }
      if (body) wbtRenderBackfillJobs(body);
      const running = !!(body && body.running > 0);
      if (running) {
        wbt.bfHadRunning = true;
        wbt.bfTimer = setTimeout(tick, 5000);
        return;
      }
      wbt.bfTimer = null;
      if (wbt.bfHadRunning) {
        wbt.bfHadRunning = false;
        wbt.loaded = false;      // 强制重新拉取：补算结果可能刚出现
        wbtLoadOverview();
      }
    };
    tick();
  }

  /** 导出跟踪结果：后端由另一同事实现，这里只负责调用与失败兜底。 */
  async function wbtExportTrack() {
    try {
      const j = await api("/api/v1/bagua/track/export?weeks=" + wbt.windowWeeks);
      if (j && j.ok === true && j.download_url) {
        const a = document.createElement("a");
        a.href = j.download_url;
        if (j.file) a.download = String(j.file);
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
        toast("已开始下载跟踪导出文件");
      } else {
        // ok:false（后端尚未实现/未产出）与 404 同处理，不打断页面
        toast("跟踪导出暂不可用");
      }
    } catch (e) {
      toast("跟踪导出暂不可用：" + (e && e.message ? e.message : e));
    }
  }

```

## 跟踪前端逻辑全量 JS 之二（wbtBind：控件绑定与补算入口）

原文件第 16558–16610 行

```javascript
  function wbtBind() {
    const trackRoot = $("wbTrackRoot");
    if (!trackRoot || trackRoot.dataset.wbtBound === "1") return;
    trackRoot.dataset.wbtBound = "1";
    const trackSearch = $("wbTrackSearch");
    if (trackSearch) trackSearch.addEventListener("input", () => {
      wbt.search = trackSearch.value || "";
      wbtRenderOverview();
    });
    const trackExport = $("wbTrackExport");
    if (trackExport) trackExport.onclick = wbtExportTrack;
    const trackL1Back = $("wbTrackL1Back");
    if (trackL1Back) trackL1Back.onclick = () => { wbtShowLevel("l0"); wbtRenderOverview(); };
    const trackL2Back = $("wbTrackL2Back");
    if (trackL2Back) trackL2Back.onclick = () => {
      // 从 L0 直达的（如点本周入选）退回 L0，从 L1 进来的退回周列表
      if (wbt.l2From === "l0" || !wbt.weeks) { wbtShowLevel("l0"); wbtRenderOverview(); }
      else wbtShowLevel("l1");
    };
    // 补算指定历史周：单周（YYYYMMDD，可选只补指定规则）+ 批量（最近 N 周，全规则）
    const bfBox = $("wbTrackBfBox");
    if (bfBox) bfBox.ontoggle = () => { if (bfBox.open) wbtLoadBackfillRules(); };
    const bfRuleSearch = $("wbTrackBfRuleSearch");
    if (bfRuleSearch) bfRuleSearch.oninput = wbtRenderBackfillRules;
    const bfRuleClear = $("wbTrackBfRuleClear");
    if (bfRuleClear) bfRuleClear.onclick = () => { wbt.bfChecked.clear(); wbtRenderBackfillRules(); };
    const bfRun = $("wbTrackBfRun");
    if (bfRun) bfRun.onclick = () => {
      const el = $("wbTrackBfWeek");
      const raw = ((el && el.value) || "").trim();
      if (!raw) { toast("请先填信号日（YYYYMMDD，如 20260731）"); return; }
      const picked = wbtBackfillPickedRules();
      if (picked.length > WBT_BF_MAX_RULES) {
        toast("指定规则最多 " + WBT_BF_MAX_RULES + " 条（已选 " + picked.length + " 条）");
        return;
      }
      // 不选规则 = 整周回填（现状）；选中 = 只补这几条（子集快照）
      wbtRunBackfill(picked.length ? { week: raw, rule_ids: picked } : { week: raw });
    };
    const bfRunN = $("wbTrackBfRunN");
    if (bfRunN) bfRunN.onclick = () => {
      if (wbtBackfillPickedRules().length) {
        // 批量锚点是最近发布周——那几周早有发布名单，指定规则逐周都会被
        // 护栏拒绝（后端也是 400），前端先说清，别让用户白点
        toast("批量补算不支持指定规则：请在上方「清空」规则，或用「补算该周」逐周指定");
        return;
      }
      const sel = $("wbTrackBfWeeks");
      const n = parseInt(((sel && sel.value) || "4"), 10) || 4;
      wbtRunBackfill({ weeks_back: n });
    };
  }

```
