# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/web/static/index.html")
t = p.read_text(encoding="utf-8")

# CSS: period / status / time single line + wider
old_css = """    .history-wrap { overflow-x: auto; max-width: 100%; }
"""
new_css = """    .history-wrap { overflow-x: auto; max-width: 100%; }
    .history-table th.col-period,
    .history-table td.col-period {
      white-space: nowrap;
      min-width: 4.5rem;
      width: 4.5rem;
      text-align: center;
      vertical-align: middle;
    }
    .history-table th.col-status,
    .history-table td.col-status {
      white-space: nowrap;
      min-width: 3.5rem;
      vertical-align: middle;
    }
    .history-table th.col-time,
    .history-table td.col-time {
      white-space: nowrap;
      min-width: 9rem;
      vertical-align: middle;
    }
"""
if "col-period" not in t:
    if old_css not in t:
        raise SystemExit("history-wrap css not found")
    t = t.replace(old_css, new_css, 1)
else:
    print("css already has col-period")

# thead
old_h = (
    "+ \"<th>回测内容</th><th>时间</th><th>状态</th><th>周期</th>\"\n"
)
new_h = (
    "+ \"<th>回测内容</th>"
    "<th class='col-time'>时间</th>"
    "<th class='col-status'>状态</th>"
    "<th class='col-period'>周期</th>\"\n"
)
if "col-period'>周期" not in t and "col-period\">周期" not in t:
    if old_h not in t:
        # alternate quote style
        old_h2 = '+ "<th>回测内容</th><th>时间</th><th>状态</th><th>周期</th>"\n'
        if old_h2 in t:
            t = t.replace(old_h2, new_h.replace("'+ \"", '+ "').replace("\"\n", '"\n') if False else
                '+ "<th>回测内容</th><th class=\'col-time\'>时间</th><th class=\'col-status\'>状态</th><th class=\'col-period\'>周期</th>"\n', 1)
            print("head ok alt")
        else:
            raise SystemExit("head not found: " + repr(t[t.find("回测内容")-20:t.find("回测内容")+80]))
    else:
        t = t.replace(old_h, new_h, 1)
        print("head ok")
else:
    print("head already")

# body cells for time, status, period
old_cells = (
    '+ "<td>" + fmtTime(r.created_at) + "</td>"\n'
    '          + "<td><span class=\'badge-status " + statusClass(r.status) + "\'>" + (r.status_label || statusLabel(r.status)) + "</span></td>"\n'
    '          + "<td>" + (r.period_label || periodLabel(r.period)) + "</td>"\n'
)
new_cells = (
    '+ "<td class=\'col-time\'>" + fmtTime(r.created_at) + "</td>"\n'
    '          + "<td class=\'col-status\'><span class=\'badge-status " + statusClass(r.status) + "\'>" + (r.status_label || statusLabel(r.status)) + "</span></td>"\n'
    '          + "<td class=\'col-period\'>" + (r.period_label || periodLabel(r.period)) + "</td>"\n'
)
if "col-period'>" not in t and 'col-period">' not in t.split("renderHistory")[1][:2000]:
    if old_cells not in t:
        # dump actual
        i = t.find("fmtTime(r.created_at)")
        print(repr(t[i - 30 : i + 350]))
        raise SystemExit("body cells not found")
    t = t.replace(old_cells, new_cells, 1)
    print("body ok")
else:
    print("body already")

p.write_text(t, encoding="utf-8")
print("done")
