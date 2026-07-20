# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/web/static/index.html")
t = p.read_text(encoding="utf-8")

# body: time / status / period cells
replacements = [
    (
        '"<td>" + fmtTime(r.created_at) + "</td>"',
        '"<td class=\'col-time\'>" + fmtTime(r.created_at) + "</td>"',
    ),
    (
        '"<td><span class=\'badge-status "',
        '"<td class=\'col-status\'><span class=\'badge-status "',
    ),
    (
        '"<td>" + (r.period_label || periodLabel(r.period)) + "</td>"',
        '"<td class=\'col-period\'>" + (r.period_label || periodLabel(r.period)) + "</td>"',
    ),
]
for old, new in replacements:
    if old not in t:
        print("MISSING", old[:50])
    elif new in t:
        print("already", new[:40])
    else:
        t = t.replace(old, new, 1)
        print("replaced", old[:40])

# widen period a bit more for 日线 / 60分钟
t = t.replace(
    """    .history-table th.col-period,
    .history-table td.col-period {
      white-space: nowrap;
      min-width: 4.5rem;
      width: 4.5rem;
      text-align: center;
      vertical-align: middle;
    }
""",
    """    .history-table th.col-period,
    .history-table td.col-period {
      white-space: nowrap !important;
      min-width: 5.5rem;
      width: 5.5rem;
      max-width: none;
      text-align: center;
      vertical-align: middle;
      word-break: keep-all;
    }
""",
)

p.write_text(t, encoding="utf-8")
# verify
t = p.read_text(encoding="utf-8")
assert "col-period" in t
assert "period_label || periodLabel" in t
i = t.find("period_label || periodLabel")
print(t[i - 50 : i + 70])
print("OK")
