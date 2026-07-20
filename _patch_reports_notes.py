# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/reports.py")
t = p.read_text(encoding="utf-8")

old = '        "「交易明细」：按 FIFO 将买入与卖出配对；信号日期取不晚于买入日的最近信号。",\n'
new = (
    '        "「交易明细」：按 FIFO 将买入与卖出配对；信号日期取不晚于买入日的最近信号。",\n'
    '        "启用八卦时默认按「最佳3爻」过滤：地雷复|初九、地风升|初六、地天泰|初九。",\n'
    '        "明细列「卦名/爻位/卦象简判」来自信号日 OHLC 标注（过滤后仅保留白名单卦爻）。",\n'
)
if "最佳3爻" not in t:
    if old not in t:
        raise SystemExit("note line missing")
    t = t.replace(old, new, 1)
    print("notes updated")
else:
    print("notes already")

oldb = (
    "            f\"run_id={result.run_id} | 区间 {repro.get('start') or ''}~{repro.get('end') or ''} | \"\n"
    "            f\"period={repro.get('period')} hold={repro.get('hold')} entry_lag={repro.get('entry_lag')} | \"\n"
    "            f\"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)}\"\n"
)
newb = (
    "            f\"run_id={result.run_id} | 区间 {repro.get('start') or ''}~{repro.get('end') or ''} | \"\n"
    "            f\"period={repro.get('period')} hold={repro.get('hold')} entry_lag={repro.get('entry_lag')} | \"\n"
    "            f\"{repro.get('bagua_filter_label') or '无八卦过滤'} | \"\n"
    "            f\"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)}\"\n"
)
if "bagua_filter_label" not in t.split("ws2.append", 1)[-1][:600]:
    if oldb not in t:
        raise SystemExit("banner missing")
    t = t.replace(oldb, newb, 1)
    print("banner updated")
else:
    print("banner already")

p.write_text(t, encoding="utf-8")
print("done")
