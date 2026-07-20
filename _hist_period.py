# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")
i = t.find(".history-table")
print(t[i : i + 1000])
print("==== head ====")
i = t.find("history-table")
# find thead in renderHistory
j = t.find("class='history-table'")
print(t[j : j + 700])
j = t.find("period_label")
print("==== period cell ====")
print(t[j - 100 : j + 250])
