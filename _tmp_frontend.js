function dsFmtYmd(v) {
    if (v === null || v === undefined || v === "" || v === 0) return "";
    return String(v);
  }
  function dsFmtYmdDash(v) {
    const s = dsFmtYmd(v);
    if (!s) return "";
    if (/^\d{8}$/.test(s)) return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
    return s;
  }
  const DS_WD = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  function dsYmdWd(v) {
    const s = dsFmtYmdDash(v);
    if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const d = new Date(parseInt(s.slice(0, 4), 10), parseInt(s.slice(5, 7), 10) - 1, parseInt(s.slice(8, 10), 10));
    return s + " " + DS_WD[d.getDay()];
  }
  function dsDateRange(a, b) {
    const s = dsYmdWd(a), t = dsYmdWd(b);
    if (!s && !t) return "";
    return (s || "—") + "~" + (t || "—");
  }
  function dsNum(v) {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    return isFinite(n) ? n.toLocaleString() : "—";
  }
  function dsTodayYmdInt() {
    const d = new Date();
    return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  }
  function dsLagTradingDays(updatedTo) {
    const n = parseInt(String(updatedTo || "").replace(/\D/g, ""), 10);
    if (!n || !isFinite(n)) return null;
    const today = dsTodayYmdInt();
    if (n >= today) return 0;
    // workday (Mon-Fri) count from the day after the cutoff through today;
    // the cutoff day itself already contains data, so it is not counted
    const y = Math.floor(n / 10000), m = Math.floor((n % 10000) / 100), day = n % 100;
    const a = new Date(y, m - 1, day);
    a.setDate(a.getDate() + 1);
    const b = new Date();
    b.setHours(0, 0, 0, 0);
    let lag = 0;
    for (const d = a; d <= b; d.setDate(d.getDate() + 1)) {
      const wd = d.getDay();
      if (wd !== 0 && wd !== 6) lag++;
    }
    return lag;
  }
  function pickSourceFreshnessClient(datasets) {
    const list = Array.isArray(datasets) ? datasets : [];
    const specs = [
      { key: "tushare", label: "Tushare日线", match: function(d) { return d.source === "tushare" && d.adjustment === "none"; } },
      { key: "factor", label: "Tushare前复权因子", match: function(d) { return d.source === "tushare" && d.adjustment === "adj_factor"; } },
      { key: "l2_product", label: "正式L2(未复权)", match: function(d) { return d.source === "internal" && d.adjustment === "composite_none" && (d.data_policy === "tushare_only_v1" || (d.provenance || {}).data_policy === "tushare_only_v1"); } },
      { key: "l1_product", label: "正式L1(前复权)", match: function(d) { return d.source === "internal" && d.adjustment === "composite_tushare_factor_qfq" && (d.data_policy === "tushare_only_v1" || (d.provenance || {}).data_policy === "tushare_only_v1"); } },
    ];
    const rank = { ready: 3, partial: 2, building: 1, failed: 0 };
    return specs.map(function(sp) {
      const cands = list.filter(function(d) { return d && sp.match(d); });
      if (!cands.length) {
        return { key: sp.key, label: sp.label, status: "missing", updated_to: null, latest_date: null, earliest_date: null, symbol_count: 0, row_count: 0, dataset_id: null, created_at: null };
      }
      cands.sort(function(a, b) {
        const ua = parseInt(a.data_cutoff_date || a.latest_date || 0, 10) || 0;
        const ub = parseInt(b.data_cutoff_date || b.latest_date || 0, 10) || 0;
        const ra = rank[a.status] || -1, rb = rank[b.status] || -1;
        if (ra !== rb) return rb - ra;
        if (ua !== ub) return ub - ua;
        return String(b.created_at || "").localeCompare(String(a.created_at || ""));
      });
      const best = cands[0];
      const updated = best.data_cutoff_date || best.latest_date || null;
      return {
        key: sp.key, label: sp.label, status: best.status,
        dataset_id: best.dataset_id, source: best.source, adjustment: best.adjustment,
        earliest_date: best.earliest_date, latest_date: best.latest_date,
        data_cutoff_date: best.data_cutoff_date, updated_to: updated,
        symbol_count: best.symbol_count, row_count: best.row_count, created_at: best.created_at,
      };
    });
  }
  async function loadCAStatus() {
    const card = document.querySelector('.src-fresh-card[data-src="ca"]');
    if (!card) return;
    const dateEl = card.querySelector(".src-fresh-date");
    const metaEl = card.querySelector(".src-fresh-meta");
    const stEl = card.querySelector(".src-fresh-status");
    try {
      const st = await api("/api/v1/ca-events/status");
      if (!st || !st.exists) {
        if (dateEl) dateEl.textContent = "未同步";
        if (dateEl) dateEl.style.color = "var(--red)";
        if (metaEl) metaEl.textContent = "点击「更新CA数据」首次拉取";
        if (stEl) stEl.innerHTML = '<span class="tag tag-red">缺失</span>';
        card.style.borderColor = "rgba(255,79,123,.45)";
        return;
      }
      const syncAt = st.last_sync_at || "—";
      if (dateEl) { dateEl.textContent = syncAt.split(" ")[0] || syncAt; dateEl.style.color = "var(--green)"; }
      if (metaEl) metaEl.textContent = "上次: " + syncAt + " · 模式: " + (st.last_sync_mode || "—") + " · 股票: " + (st.total_files || 0) + "只";
      if (stEl) stEl.innerHTML = '<span class="tag tag-green">已同步</span>';
      card.style.borderColor = "rgba(53,212,147,.35)";
    } catch (e) {
      if (dateEl) { dateEl.textContent = "加载失败"; dateEl.style.color = "var(--red)"; }
      if (metaEl) metaEl.textContent = String(e.message || e);
    }
  }
  function renderSourceFreshness(data) {
    const grid = $("srcFreshnessGrid");
    if (!grid) return;
    let items = (data && Array.isArray(data.source_freshness)) ? data.source_freshness : null;
    if (!items) items = pickSourceFreshnessClient(data && data.datasets);
    const byKey = {};
    items.forEach(function(it) { if (it && it.key) byKey[it.key] = it; });
    grid.querySelectorAll(".src-fresh-card").forEach(function(card) {
      const key = card.getAttribute("data-src");
      if (key === "ca") return; // CA 卡由 loadCAStatus 独立渲染（并发后防止被覆盖为缺失）
      const it = byKey[key] || { status: "missing" };
      const dateEl = card.querySelector(".src-fresh-date");
      const metaEl = card.querySelector(".src-fresh-meta");
      const stEl = card.querySelector(".src-fresh-status");
      const status = it.status || "missing";
      const updated = it.updated_to || it.latest_date || it.data_cutoff_date;
      const dateStr = dsYmdWd(updated);
      const lag = dsLagTradingDays(updated);
      let dateColor = "var(--text)";
      let borderColor = "var(--line2)";
      if (status === "missing" || status === "failed" || !dateStr) {
        dateColor = "var(--red)";
        borderColor = "rgba(255,79,123,.45)";
      } else if (status === "partial" || (lag != null && lag > 3)) {
        dateColor = "var(--orange)";
        borderColor = "rgba(255,173,51,.45)";
      } else if (status === "ready" && lag != null && lag <= 3) {
        dateColor = "var(--green)";
        borderColor = "rgba(53,212,147,.35)";
      }
      card.style.borderColor = borderColor;
      if (dateEl) {
        dateEl.textContent = dateStr || "无数据";
        dateEl.style.color = dateColor;
        dateEl.title = it.dataset_id ? String(it.dataset_id) : "";
      }
      if (stEl) {
        if (status === "missing") {
          stEl.innerHTML = '<span class="tag tag-red" style="font-size:10px;padding:1px 5px">缺失</span>';
        } else {
          const cls = status === "ready" ? "tag-green" : status === "partial" ? "tag-orange" : "tag-red";
          stEl.innerHTML = '<span class="tag ' + cls + '" style="font-size:10px;padding:1px 5px">' + esc(status) + '</span>';
        }
      }
      if (metaEl) {
        if (!dateStr) {
          metaEl.textContent = "本地尚无该数据集";
        } else {
          const range = dsDateRange(it.earliest_date, it.latest_date);
          const parts = [];
          parts.push("更新到 " + dateStr);
          if (lag != null) parts.push(lag === 0 ? "当天" : "落后" + lag + "天");
          if (it.symbol_count) parts.push(dsNum(it.symbol_count) + "只");
          if (range) parts.push(esc(range));
          metaEl.innerHTML = parts.join(" · ");
        }
      }
    });
  }

  