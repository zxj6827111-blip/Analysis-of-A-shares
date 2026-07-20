# -*- coding: utf-8 -*-
"""Fix duplicate fmtPct overwriting fraction→percent conversion."""
from pathlib import Path

HTML = Path("wtpy/apps/astock/web/static/index.html")
t = HTML.read_text(encoding="utf-8")

# 1) Remove the second broken fmtPct that overwrites the good one
old_second = """    function fmtPct(v) {
      if (v == null || v === "" || Number.isNaN(Number(v))) return "—";
      const n = Number(v);
      const sign = n > 0 ? "+" : "";
      return sign + n.toFixed(2) + "%";
    }
"""
# Forecast week ret is usually already in percent points (e.g. 1.23 for +1.23%)
new_second = """    /** Forecast / quote cards: value already in percent points (1.23 => +1.23%). */
    function fmtPctPoints(v) {
      if (v == null || v === "" || Number.isNaN(Number(v))) return "—";
      const n = Number(v);
      const sign = n > 0 ? "+" : "";
      return sign + n.toFixed(2) + "%";
    }
"""
if old_second not in t:
    raise SystemExit("second fmtPct not found exactly")
t = t.replace(old_second, new_second, 1)

# 2) forecast call site: use fmtPctPoints
if "fmtPct(ret)" in t:
    t = t.replace("fmtPct(ret)", "fmtPctPoints(ret)", 1)

# 3) Harden good fmtPct: rename is fine; ensure only one function fmtPct remains
count = t.count("function fmtPct")
print("function fmtPct count after", count)
if count != 1:
    raise SystemExit(f"expected 1 fmtPct, got {count}")

# 4) showMetrics: format money/counts better; account_mode Chinese
old_show = """        } else if (typeof v === "number") {
          v = Number.isInteger(v) ? v.toLocaleString("zh-CN") : v.toFixed(2);
        }
        d.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${vHtml != null ? vHtml : v}</div>`;
"""
new_show = """        } else if (k === "account_mode") {
          const mode = String(v || "").toLowerCase();
          v = (mode === "per_symbol" || mode === "tdx")
            ? "通达信对照·单票独立资金"
            : (mode === "portfolio" ? "组合账户·共享资金" : String(v));
        } else if (typeof v === "number") {
          if (k === "final_equity" || k === "cost_total" || k === "open_market_value" || k === "capital_base") {
            v = Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
          } else if (Number.isInteger(v) || Math.abs(v - Math.round(v)) < 1e-9) {
            v = Math.round(v).toLocaleString("zh-CN");
          } else {
            v = Number(v).toFixed(2);
          }
        }
        d.innerHTML = `<div class="k">${metricLabel(k)}</div><div class="v">${vHtml != null ? vHtml : v}</div>`;
"""
if "通达信对照·单票独立资金" not in t.split("function showMetrics")[1][:2000]:
    if old_show not in t:
        # try without trailing
        print("WARN showMetrics number branch not exact")
        print(repr(t[t.find("} else if (typeof v === \"number\")") : t.find("} else if (typeof v === \"number\")") + 400]))
    else:
        t = t.replace(old_show, new_show, 1)
        print("showMetrics money format ok")

# 5) metric labels cleaner (remove confusing 小数×100 from title; keep in hover)
t = t.replace(
    'total_return: "总收益率（小数×100）"',
    'total_return: "总收益率"',
)

# 6) Self-test comment block in HTML for maintainers
# Add a tiny runtime assert after fmtPct definition once
marker = "function fracToPctNumber(x) {"
if "/* pct_selfcheck" not in t:
    insert = """/* pct_selfcheck: win_rate 0.4614 => ~46.14%; return 0.005 => +0.50% */
    function fracToPctNumber(x) {"""
    t = t.replace(marker, insert, 1)

HTML.write_text(t, encoding="utf-8")

# verify
t2 = HTML.read_text(encoding="utf-8")
assert t2.count("function fmtPct") == 1
assert "function fmtPctPoints" in t2
assert "function fracToPctNumber" in t2
print("OK file written")
