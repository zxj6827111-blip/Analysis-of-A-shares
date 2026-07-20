# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/web/static/index.html")
t = p.read_text(encoding="utf-8")
i = t.find("function fmtPct(v)")
if i < 0:
    raise SystemExit("second def not found")
j = t.find("function renderQuoteCard", i)
if j < 0:
    raise SystemExit("renderQuoteCard not found")
new = (
    "    function fmtPctPoints(v) {\n"
    '      if (v == null || v === "" || Number.isNaN(Number(v))) return "—";\n'
    "      const n = Number(v);\n"
    '      const sign = n > 0 ? "+" : "";\n'
    '      return sign + n.toFixed(2) + "%";\n'
    "    }\n"
    "    "
)
t2 = t[:i] + new + t[j:]
if t2.count("fmtPct(ret)") >= 1:
    t2 = t2.replace("fmtPct(ret)", "fmtPctPoints(ret)", 1)
if t2.count("function fmtPct") != 1:
    raise SystemExit("fmtPct count=%s" % t2.count("function fmtPct"))
if "function fmtPctPoints" not in t2:
    raise SystemExit("fmtPctPoints missing")
p.write_text(t2, encoding="utf-8")
print("OK fixed")
