# -*- coding: utf-8 -*-
import json
import urllib.request
from pathlib import Path

html = urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5).read().decode("utf-8")
print("fracToPctNumber", "fracToPctNumber" in html)
print("historyReturnDisplay", "historyReturnDisplay" in html)
print("eq note", "等权单票" in html)
print("old_fmtPct", 'toFixed(2) + "%"' in html and "fracToPctNumber" not in html)

# extract actual fmtPct from page
i = html.find("function fmtPct")
print(html[i : i + 600] if i >= 0 else "no fmtPct")
i = html.find("function historyReturnDisplay")
print("--- histRet ---")
print(html[i : i + 700] if i >= 0 else "no")

raw = urllib.request.urlopen("http://127.0.0.1:8765/api/v1/runs?limit=8", timeout=20).read().decode(
    "utf-8"
)
rows = json.loads(raw)
for r in rows[:6]:
    m = dict(r.get("metrics_brief") or {})
    m.update(r.get("metrics") or {})
    tr = m.get("total_return")
    mr = m.get("mean_symbol_return")
    wr = m.get("win_rate")
    mdd = m.get("max_drawdown")
    nb = m.get("n_buys")
    mode = m.get("account_mode")
    print("====", r.get("run_id"))
    print(" mode", mode)
    print(" total_return", tr, "as_pct", None if tr is None else round(tr * 100, 4))
    print(" mean_symbol", mr, "as_pct", None if mr is None else round(mr * 100, 4))
    print(" max_dd", mdd, "as_pct", None if mdd is None else round(mdd * 100, 4))
    print(" win_rate", wr, "as_pct", None if wr is None else round(wr * 100, 2))
    print(" n_buys", nb)
    # what UI would show
    if mode in ("per_symbol", "tdx") and mr is not None:
        show = mr * 100
        tag = "等权单票"
    else:
        show = (tr * 100) if tr is not None else None
        tag = "总收益"
    if show is not None:
        absv = abs(show)
        dig = 4 if 0 < absv < 0.1 else (3 if 0 < absv < 1 else 2)
        print(" UI_SHOW", f"{show:+.{dig}f}%", tag)
