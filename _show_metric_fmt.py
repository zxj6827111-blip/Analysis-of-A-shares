# -*- coding: utf-8 -*-
from pathlib import Path
t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
i = t.find("function metricLabel")
print(t[i : i + 900])
i = t.find('const keys = [')
print("==== keys")
print(t[i : i + 700])
i = t.find("k.includes(")
print("==== includes")
print(t[i - 80 : i + 350])
