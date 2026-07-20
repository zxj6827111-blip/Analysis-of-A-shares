# -*- coding: utf-8 -*-
"""Speed up post-backtest writing: slim meta JSON + capped Excel detail rows."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "wtpy" / "apps" / "astock" / "reports.py"

# Full trade list stays in trades.csv; Excel only embeds a preview when huge.
EXCEL_MAX_DETAIL_ROWS = 3000


def main() -> None:
    t = REPORTS.read_text(encoding="utf-8")

    # 1) Cap trips written into Excel workbook
    old = """    green = PatternFill("solid", fgColor="D5F5E3")
    red = PatternFill("solid", fgColor="FADBD8")
    gray = PatternFill("solid", fgColor="F2F3F4")

    for t in trips:
        row = [
"""
    new = f"""    green = PatternFill("solid", fgColor="D5F5E3")
    red = PatternFill("solid", fgColor="FADBD8")
    gray = PatternFill("solid", fgColor="F2F3F4")

    # openpyxl is very slow for tens of thousands of styled rows (UI stuck at 96%).
    # Full FIFO list is always in trades.csv; Excel keeps a preview + summary.
    excel_cap = {EXCEL_MAX_DETAIL_ROWS}
    trips_for_excel = list(trips)
    excel_truncated = False
    if len(trips_for_excel) > excel_cap:
        excel_truncated = True
        trips_for_excel = trips_for_excel[:excel_cap]

    for t in trips_for_excel:
        row = [
"""
    if "trips_for_excel" not in t:
        if old not in t:
            raise SystemExit("excel loop block not found")
        t = t.replace(old, new, 1)
        print("capped excel loop")
    else:
        print("excel loop already capped")

    # Update banner to mention truncation
    old_banner_end = (
        '            f"{repro.get(\'bagua_filter_label\') or \'无八卦过滤\'} | "\n'
        '            f"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)}"\n'
    )
    new_banner_end = (
        '            f"{repro.get(\'bagua_filter_label\') or \'无八卦过滤\'} | "\n'
        '            f"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)} | "\n'
        '            f"明细行{len(trips)}"\n'
        '            + (f"（Excel仅预览前{excel_cap}行，完整见 trades.csv）" if excel_truncated else "")\n'
    )
    if "Excel仅预览" not in t:
        if old_banner_end not in t:
            # fallback without bagua line
            old2 = (
                '            f"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)}"\n'
            )
            new2 = (
                '            f"已平{len(closed)} 盈{win_n} 亏{loss_n} 未平{open_n} | 合计净利润≈{_fmt_num(net_sum, 2)} | "\n'
                '            f"明细行{len(trips)}"\n'
                '            + (f"（Excel仅预览前{excel_cap}行，完整见 trades.csv）" if excel_truncated else "")\n'
            )
            if old2 not in t:
                print("WARN banner not patched", repr(t[t.find("已平") : t.find("已平") + 200] if "已平" in t else "no"))
            else:
                t = t.replace(old2, new2, 1)
                print("banner patched (fallback)")
        else:
            t = t.replace(old_banner_end, new_banner_end, 1)
            print("banner patched")
    else:
        print("banner already")

    # notes
    note = '        "明细列「卦名/爻位/卦象简判」来自信号日 OHLC 标注（过滤后仅保留白名单卦爻）。",\n'
    note_add = (
        note
        + f'        "交易明细超过 {EXCEL_MAX_DETAIL_ROWS} 行时，Excel 仅预览前 {EXCEL_MAX_DETAIL_ROWS} 行；完整明细见同目录 trades.csv。",\n'
    )
    if "Excel 仅预览" not in t and note in t:
        t = t.replace(note, note_add, 1)
        print("notes patched")

    # 2) Slim run_meta: avoid dumping full fills list (was ~28MB and slows I/O)
    old_meta = """    full_meta = result.to_dict()
    if meta:
        full_meta["repro"] = meta
    full_meta["n_trade_rows"] = len(trips)
    atomic_write_json(meta_path, full_meta)
"""
    new_meta = """    # Do not embed full fills/equity in run_meta.json (can be tens of MB and block UI at 96%).
    full_meta = {
        "run_id": result.run_id,
        "status": result.status,
        "metrics": result.metrics,
        "notes": result.notes,
        "n_fills": len(result.fills),
        "n_equity_points": len(result.equity_curve),
        "n_trade_rows": len(trips),
        "fills_path": "fills.csv",
        "equity_path": "equity.csv",
        "trades_path": "trades.csv",
        "signals_path": "signals.csv",
    }
    if meta:
        full_meta["repro"] = meta
    # keep a tiny sample for debugging only
    full_meta["fills_sample"] = [f.to_dict() if hasattr(f, "to_dict") else getattr(f, "__dict__", str(f)) for f in result.fills[:20]]
    atomic_write_json(meta_path, full_meta)
"""
    if "fills_path" not in t or "Do not embed full fills" not in t:
        if old_meta not in t:
            raise SystemExit("meta write block not found")
        t = t.replace(old_meta, new_meta, 1)
        print("slim meta patched")
    else:
        print("meta already slim")

    REPORTS.write_text(t, encoding="utf-8")

    # syntax check
    import ast

    ast.parse(t)
    print("OK reports.py syntax")


if __name__ == "__main__":
    main()
