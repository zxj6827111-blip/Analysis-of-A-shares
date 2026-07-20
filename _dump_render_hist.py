# -*- coding: utf-8 -*-
from pathlib import Path
import re

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")

# full renderHistory
i = t.find("function renderHistory")
j = t.find("\n    function ", i + 10)
# find next top-level function after renderHistory
for name in ["function updateHistSelInfo", "function loadRuns", "async function loadRuns", "function bindHistory"]:
    k = t.find(name, i)
    if k > i:
        j = k
        break
print("renderHistory span", i, j, j - i)
print(t[i:j][:3500])
print("---END---")
print(t[j:j+400])
