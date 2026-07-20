# -*- coding: utf-8 -*-
from pathlib import Path

p = Path("wtpy/apps/astock/service/backtest.py")
t = p.read_text(encoding="utf-8")
old = """    _progress({
        "phase": "writing",
        "pct": 96.0,
        "current": n_codes,
        "total": n_codes,
        "message": "写入结果文件",
        "code": None,
        "run_id": run_id,
    })

    write_signals_csv(out_dir / "signals.csv", events)
    paths = write_backtest_csv(out_dir, result, meta=repro, events=events)
"""
new = """    _progress({
        "phase": "writing",
        "pct": 96.0,
        "current": n_codes,
        "total": n_codes,
        "message": f"写入结果（信号 {len(events)} / 成交 {len(result.fills)}）…",
        "code": None,
        "run_id": run_id,
    })

    write_signals_csv(out_dir / "signals.csv", events)
    _progress({
        "phase": "writing_excel",
        "pct": 97.0,
        "current": n_codes,
        "total": n_codes,
        "message": "写入 CSV / Excel 汇总（大明细可能仍需片刻）…",
        "code": None,
        "run_id": run_id,
        "n_fills": len(result.fills),
    })
    paths = write_backtest_csv(out_dir, result, meta=repro, events=events)
"""
if "writing_excel" not in t:
    if old not in t:
        raise SystemExit("writing block not found")
    p.write_text(t.replace(old, new, 1), encoding="utf-8")
    print("progress split ok")
else:
    print("already")
