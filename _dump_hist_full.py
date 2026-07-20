# -*- coding: utf-8 -*-
from pathlib import Path

t = Path("wtpy/apps/astock/web/static/index.html").read_text(encoding="utf-8")

# history toolbar HTML
i = t.find('id="history"')
print("history box", i)
print(t[i - 800 : i + 100])
print("==== renderHistory full start ====")
i = t.find("function renderHistory")
print(t[i : i + 2200])
print("==== hist CSS ====")
i = t.find(".history-table")
print(t[i - 100 : i + 500])
print("==== hist buttons handlers ====")
for key in ["btnClearHistory", "btnDeleteSelected", "histCheckAll", "delete_run", "loadHistory"]:
    j = t.find(key)
    print(key, j)
