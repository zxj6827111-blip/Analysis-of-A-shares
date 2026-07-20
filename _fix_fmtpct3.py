# -*- coding: utf-8 -*-
from pathlib import Path
import re

p = Path("wtpy/apps/astock/web/static/index.html")
t = p.read_text(encoding="utf-8")

# Replace ONLY the second definition: function fmtPct(v) { ... no *100 }
pattern = re.compile(
    r"    function fmtPct\(v\) \{\n"
    r"      if \(v == null \|\| v === \"\" \|\| Number\.isNaN\(Number\(v\)\)\) return \"—\";\n"
    r"      const n = Number\(v\);\n"
    r'      const sign = n > 0 \? "\+" : "";\n'
    r'      return sign \+ n\.toFixed\(2\) \+ "%";\n'
    r"    \}\n"
)
m = pattern.search(t)
if not m:
    # looser
    i = t.find("function fmtPct(v)")
    if i < 0:
        raise SystemExit("no fmtPct(v)")
    j = t.find("\n    function ", i + 10)
    if j < 0:
        j = t.find("\n    function renderQuoteCard", i)
    print("loose block:", repr(t[i:j]))
    t = t[:i] + (
        "function fmtPctPoints(v) {\n"
        '      if (v == null || v === "" || Number.isNaN(Number(v))) return "—";\n'
        "      const n = Number(v);\n"
        '      const sign = n > 0 ? "+" : "";\n'
        '      return sign + n.toFixed(2) + "%";\n'
        "    }"
    ) + t[j:]
else:
    t = pattern.sub(
        "    function fmtPctPoints(v) {\n"
        '      if (v == null || v === "" || Number.isNaN(Number(v))) return "—";\n'
        "      const n = Number(v);\n"
        '      const sign = n > 0 ? "+" : "";\n'
        '      return sign + n.toFixed(2) + "%";\n'
        "    }\n",
        t,
        count=1,
    )
    print("regex replaced")

t = t.replace("fmtPct(ret)", "fmtPctPoints(ret)")

# Count real fmtPct defs (not Points)
defs = re.findall(r"function fmtPct(?:Points)?\(", t)
print("defs", defs)
if sum(1 for d in defs if d == "function fmtPct(") != 1:
    raise SystemExit("bad defs %s" % defs)
if "function fmtPctPoints(" not in t:
    raise SystemExit("missing points")

# account mode chinese if missing
if "通达信对照·单票独立资金" not in t:
    old = """        } else if (typeof v === "number") {
          v = Number.isInteger(v) ? v.toLocaleString("zh-CN") : v.toFixed(2);
        }
"""
    new = """        } else if (k === "account_mode") {
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
"""
    if old in t:
        t = t.replace(old, new, 1)
        print("account_mode format ok")
    else:
        print("account_mode skip")

t = t.replace('total_return: "总收益率（小数×100）"', 'total_return: "总收益率"')

p.write_text(t, encoding="utf-8")
print("written OK")
