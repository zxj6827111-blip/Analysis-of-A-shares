# -*- coding: utf-8 -*-
"""Make percent displays show both % and raw fraction; unify result panel fmt."""
from pathlib import Path

HTML = Path("wtpy/apps/astock/web/static/index.html")
t = HTML.read_text(encoding="utf-8")

# 1) Improve fmtPct body to always attach raw fraction in title; for small values more digits
old = """    function fmtPct(x, opts) {
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
"""

new = """    function fmtPct(x, opts) {
      const p = fracToPctNumber(x);
      if (p === null) return "—";
      const abs = Math.abs(p);
      // 后端存 0~1 小数：0.0046 → +0.46%，0.46 → +46.00%
      let digits = 2;
      if (abs > 0 && abs < 0.1) digits = 4;
      else if (abs > 0 && abs < 1) digits = 3;
      const sign = p > 0 ? "+" : (p < 0 ? "" : "");
      const body = sign + p.toFixed(digits) + "%";
      const raw = Number(x);
      const rawStr = (Math.abs(raw) > 0 && Math.abs(raw) < 0.01)
        ? raw.toExponential(3)
        : String(Math.round(raw * 1e8) / 1e8);
      const tip = (opts && opts.title) ? opts.title : ("百分比 = 小数×100。原始小数 " + rawStr);
      if (opts && opts.html) {
        const cls = p > 1e-12 ? "pct-pos" : (p < -1e-12 ? "pct-neg" : "pct-zero");
        const showRaw = !!(opts && opts.showRaw);
        const extra = showRaw
          ? ("<span class='pct-note'>小数 " + rawStr + "</span>")
          : "";
        return "<span class='" + cls + "' title='" + tip.replace(/'/g, "&#39;") + "'>" + body + "</span>" + extra;
      }
      return body;
    }
"""

if "百分比 = 小数×100" not in t:
    if old not in t:
        raise SystemExit("fmtPct block mismatch")
    t = t.replace(old, new, 1)
    print("fmtPct updated")
else:
    print("fmtPct already")

# history return always show raw under percent
t = t.replace(
    "return fmtPct(m.mean_symbol_return, {\n"
    "          html: true,\n"
    "          title: \"等权平均单票收益率（通达信对照口径）。汇总权益总收益=\" + fmtPct(m.total_return)\n"
    "        }) + \"<span class='pct-note'>等权单票</span>\";",
    "return fmtPct(m.mean_symbol_return, {\n"
    "          html: true,\n"
    "          showRaw: true,\n"
    "          title: \"等权平均单票收益率（通达信对照）。0.0046 小数 = +0.46%。汇总总收益=\" + fmtPct(m.total_return)\n"
    "        }) + \"<span class='pct-note'>等权单票</span>\";",
)
t = t.replace(
    "return fmtPct(m.total_return, {\n"
    "        html: true,\n"
    "        title: \"组合/汇总总收益率（小数 \" + m.total_return + \"）\"\n"
    "      });",
    "return fmtPct(m.total_return, {\n"
    "        html: true,\n"
    "        showRaw: true,\n"
    "        title: \"组合/汇总总收益率。后端为小数(0.0046=+0.46%)，已×100显示\"\n"
    "      });",
)

# result panel: use fmtPct instead of ad-hoc *100
old_v = """        let v = m[k];
        if (typeof v === "number" && (k.includes("return") || k.includes("drawdown") || k === "win_rate" || k === "volatility" || k === "cost_impact")) {
          v = (v * 100).toFixed(2) + "%";
        } else if (typeof v === "number") {
          v = Number.isInteger(v) ? v : v.toFixed(2);
        }
        d.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${v}</di
"""
# find exact
i = t.find('let v = m[k];')
print("let v at", i)
print(repr(t[i:i+280]))

old_block = """        let v = m[k];
        if (typeof v === "number" && (k.includes("return") || k.includes("drawdown") || k === "win_rate" || k === "volatility" || k === "cost_impact")) {
          v = (v * 100).toFixed(2) + "%";
        } else if (typeof v === "number") {
          v = Number.isInteger(v) ? v : v.toFixed(2);
        }
"""
new_block = """        let v = m[k];
        let vHtml = null;
        if (typeof v === "number" && (k.includes("return") || k.includes("drawdown") || k === "win_rate" || k === "volatility" || k === "cost_impact" || k === "pct_symbols_profitable")) {
          // 统一：小数 → 百分比（0.0046 → +0.46%）
          vHtml = fmtPct(v, { html: true, showRaw: true });
          v = null;
        } else if (typeof v === "number") {
          v = Number.isInteger(v) ? v.toLocaleString("zh-CN") : v.toFixed(2);
        }
"""
if "统一：小数 → 百分比" not in t:
    if old_block not in t:
        raise SystemExit("metric v block not found")
    t = t.replace(old_block, new_block, 1)

# fix innerHTML line for vHtml
old_html = '        d.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${v}</div>`;'
# might be slightly different
if "vHtml" in t and "vHtml !== null" not in t:
    import re
    t2, n = re.subn(
        r"d\.innerHTML = `<div class=\"k\">\$\{metricLabel\(k\)}</div><div class=\"v\">\$\{v}</div>`;",
        'd.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${vHtml != null ? vHtml : v}</div>`;',
        t,
        count=1,
    )
    if n != 1:
        # try without escape
        old_line = None
        for line in t.splitlines():
            if "metricLabel(k)" in line and "innerHTML" in line:
                old_line = line
                print("found line", line)
                break
        if old_line:
            t = t.replace(
                old_line,
                '        d.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${vHtml != null ? vHtml : v}</div>`;',
                1,
            )
            print("innerHTML fixed via line")
        else:
            print("WARN innerHTML not fixed")
    else:
        t = t2
        print("innerHTML fixed re")

# expand metrics keys for mean_symbol
old_keys = """      const keys = [
        "total_return", "annual_return", "max_drawdown", "final_equity",
        "n_buys", "n_sells", "n_round_trips", "win_rate",
        "n_days", "cost_total", "n_open_positions", "zero_cost_return"
      ];
"""
new_keys = """      const keys = [
        "total_return", "mean_symbol_return", "median_symbol_return", "annual_return", "max_drawdown", "final_equity",
        "n_buys", "n_sells", "n_round_trips", "win_rate",
        "n_days", "cost_total", "n_open_positions", "n_symbol_accounts", "zero_cost_return", "account_mode"
      ];
"""
if "mean_symbol_return" not in t[t.find("const keys = [") : t.find("const keys = [") + 300]:
    if old_keys in t:
        t = t.replace(old_keys, new_keys, 1)
        print("keys expanded")
    else:
        print("WARN keys")

# metric labels
old_map = """        total_return: "总收益率",
        annual_return: "年化收益率",
"""
new_map = """        total_return: "总收益率（小数×100）",
        mean_symbol_return: "等权平均单票收益",
        median_symbol_return: "单票收益中位数",
        annual_return: "年化收益率",
        account_mode: "账户模式",
        n_symbol_accounts: "单票账户数",
"""
if "等权平均单票收益" not in t[t.find("function metricLabel") : t.find("function metricLabel") + 800]:
    t = t.replace(old_map, new_map, 1)
    print("labels ok")

# account_mode display as text not percent
# in the number branch, account_mode is string so ok

HTML.write_text(t, encoding="utf-8")
print("written", HTML.stat().st_size)
