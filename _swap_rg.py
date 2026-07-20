# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/web/static/index.html")
t = p.read_text(encoding="utf-8")

old = """    .history-table td.num .pct-pos { color: #3dd68c; }
    .history-table td.num .pct-neg { color: #f07178; }
"""
new = """    /* A股习惯：红=盈利/胜率上涨，绿=亏损/回撤 */
    .history-table td.num .pct-pos { color: #f31260; }
    .history-table td.num .pct-neg { color: #3dd68c; }
    #metrics .v .pct-pos { color: #f31260; }
    #metrics .v .pct-neg { color: #3dd68c; }
"""
if old not in t:
    raise SystemExit("pct css block not found")
t = t.replace(old, new, 1)

old_ret = (
    '      const retCls = (ret != null && Number(ret) > 0) ? "fc-match-ok" : '
    '((ret != null && Number(ret) < 0) ? "fc-match-bad" : "");'
)
new_ret = (
    "      // 红涨绿跌\n"
    '      const retCls = (ret != null && Number(ret) > 0) ? "fc-match-bad" : '
    '((ret != null && Number(ret) < 0) ? "fc-match-ok" : "");'
)
if old_ret not in t:
    raise SystemExit("retCls not found: " + repr(t[t.find("const retCls") : t.find("const retCls") + 120]))
t = t.replace(old_ret, new_ret, 1)

p.write_text(t, encoding="utf-8")
print("OK red-up green-down")
