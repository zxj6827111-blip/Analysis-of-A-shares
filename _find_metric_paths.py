# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
for name in ["function fillMetrics", "function renderMetrics", "function showRunResult", "function showMetrics"]:
    i = t.find(name)
    print(name, i)
    if i >= 0:
        print(t[i : i + 400])
        print("---")

# any other * 100 or percent formatting
idx = 0
while True:
    j = t.find("toFixed", idx)
    if j < 0:
        break
    ctx = t[max(0, j - 60) : j + 40]
    if "100" in ctx or "pct" in ctx.lower() or "return" in ctx or "rate" in ctx:
        print("toFixed ctx:", ctx.replace("\n", " | "))
    idx = j + 1
