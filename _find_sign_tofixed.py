# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
# find the fc percent formatter
i = t.find('return sign + n.toFixed(2) + "%"')
print("at", i)
print(t[i - 400 : i + 100])

# showRunResult metrics path
i = t.find("function showRunResult")
print("=== showRunResult metrics usage ===")
chunk = t[i : i + 2500]
print(chunk)
j = chunk.find("showMetrics")
print("showMetrics call", j, chunk[j : j + 200] if j >= 0 else None)
