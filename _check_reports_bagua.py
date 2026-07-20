# -*- coding: utf-8 -*-
from pathlib import Path
import re

r = Path("wtpy/apps/astock/reports.py").read_text(encoding="utf-8")
for m in re.finditer(r'"指标": ind', r):
    print("at", m.start())
    print(r[m.start() - 150 : m.start() + 280])
    print("====")
print("find_signal calls:")
for m in re.finditer(r"find_signal\(", r):
    print(r[max(0, m.start() - 80) : m.start() + 120])
    print("---")

# unmatched open trips without 卦名 after 指标
if re.search(r'"指标": ind,\n\s*"买入日期"', r):
    print("STILL MISSING GUA after 指标")
else:
    print("all 指标 blocks look expanded or different")
