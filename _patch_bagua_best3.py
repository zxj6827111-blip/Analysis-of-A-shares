# -*- coding: utf-8 -*-
"""Patch backtest/reports/api for bagua best3 filter + richer Excel."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def patch_backtest() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "service" / "backtest.py"
    t = p.read_text(encoding="utf-8")

    needle = "from ..bagua.calculator import BaguaCalculator\n"
    insert = (
        "from ..bagua.calculator import BaguaCalculator\n"
        "from ..bagua.filter_rules import (\n"
        "    DEFAULT_BAGUA_FILTER_MODE,\n"
        "    best3_display_pairs,\n"
        "    filter_events_by_bagua_mode,\n"
        "    mode_label as bagua_mode_label,\n"
        ")\n"
    )
    if "filter_events_by_bagua_mode" not in t:
        if needle not in t:
            raise SystemExit("import needle missing")
        t = t.replace(needle, insert, 1)

    old_field = "    with_bagua: bool = False\n"
    new_field = (
        "    with_bagua: bool = False\n"
        "    # When with_bagua / bagua_ohlc is on: default best3 (最佳3爻) filter.\n"
        "    bagua_filter_mode: Optional[str] = None\n"
    )
    if "bagua_filter_mode" not in t:
        t = t.replace(old_field, new_field, 1)

    old_bg = """    if any(s.id == \"bagua_ohlc\" for s in specs) or req.with_bagua:
        calc = BaguaCalculator.from_json(cfg.bagua_json)
        attach_bagua(events, period_raw_map, calc)

    try:
        cal = TradeCalendar.load(cfg.calendar_path)
"""

    new_bg = """    bagua_enabled = any(s.id == \"bagua_ohlc\" for s in specs) or bool(req.with_bagua)
    bagua_filter_mode = None
    bagua_n_before = len(events)
    bagua_n_after = len(events)
    if bagua_enabled:
        calc = BaguaCalculator.from_json(cfg.bagua_json)
        attach_bagua(events, period_raw_map, calc)
        # Product policy: 八卦 is not label-only — apply 最佳3爻 (or explicit mode).
        bagua_filter_mode = (req.bagua_filter_mode or DEFAULT_BAGUA_FILTER_MODE).strip()
        bagua_n_before = len(events)
        events = filter_events_by_bagua_mode(events, bagua_filter_mode)
        bagua_n_after = len(events)
        _progress({
            \"phase\": \"bagua_filter\",
            \"pct\": 88.0,
            \"current\": n_codes,
            \"total\": n_codes,
            \"message\": (
                f\"八卦过滤·{bagua_mode_label(bagua_filter_mode)}：\"
                f\"{bagua_n_before} → {bagua_n_after} 条信号\"
            ),
            \"code\": None,
            \"n_signals\": bagua_n_after,
            \"n_signals_before_bagua\": bagua_n_before,
        })

    try:
        cal = TradeCalendar.load(cfg.calendar_path)
"""

    if old_bg not in t:
        raise SystemExit("bagua attach block not found")
    t = t.replace(old_bg, new_bg, 1)

    old_title = """    rule_names = [s.name for s in trade_specs]
    period_label = {
        \"DAY\": \"日线\",
        \"WEEK\": \"周线\",
        \"MONTH\": \"月线\",
        \"DWM\": \"日周月共振\",
        \"MIN60\": \"60分钟\",
    }.get(period, period)
    title = \"、\".join(rule_names) if rule_names else \"回测\"
    title = f\"{title} · {period_label} · 持有{hold}\"
    if start or end:
        title += f\" · {start or ''}~{end or ''}\"
"""

    new_title = """    rule_names = [s.name for s in trade_specs]
    period_label = {
        \"DAY\": \"日线\",
        \"WEEK\": \"周线\",
        \"MONTH\": \"月线\",
        \"DWM\": \"日周月共振\",
        \"MIN60\": \"60分钟\",
    }.get(period, period)
    title = \"、\".join(rule_names) if rule_names else \"回测\"
    if bagua_enabled and bagua_filter_mode:
        title = f\"{title} + {bagua_mode_label(bagua_filter_mode)}\"
    elif bagua_enabled:
        title = f\"{title} + 八卦\"
    title = f\"{title} · {period_label} · 持有{hold}\"
    if start or end:
        title += f\" · {start or ''}~{end or ''}\"
"""

    if old_title not in t:
        raise SystemExit("title block not found exact")
    t = t.replace(old_title, new_title, 1)

    # Enrich repro after bagua_sha key
    old_sha = '        "bagua_sha": bagua_sha,\n'
    new_sha = (
        '        "bagua_sha": bagua_sha,\n'
        '        "with_bagua": bagua_enabled,\n'
        '        "bagua_filter_mode": bagua_filter_mode,\n'
        '        "bagua_filter_label": (\n'
        "            bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None\n"
        "        ),\n"
        '        "bagua_allowlist": (\n'
        "            best3_display_pairs() if bagua_filter_mode else None\n"
        "        ),\n"
        '        "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,\n'
        '        "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,\n'
    )
    if '"bagua_filter_mode"' not in t:
        if old_sha not in t:
            raise SystemExit("bagua_sha repro line not found")
        t = t.replace(old_sha, new_sha, 1)

    old_idx = """                \"selected_codes_count\": len(codes),
                \"metrics\": result.metrics,
            },
"""
    new_idx = """                \"selected_codes_count\": len(codes),
                \"metrics\": result.metrics,
                \"with_bagua\": bagua_enabled,
                \"bagua_filter_mode\": bagua_filter_mode,
                \"bagua_filter_label\": (
                    bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None
                ),
                \"n_signals_before_bagua\": bagua_n_before if bagua_enabled else None,
                \"n_signals_after_bagua\": bagua_n_after if bagua_enabled else None,
            },
"""
    if '"bagua_filter_label"' not in t or "n_signals_before_bagua" not in t:
        if old_idx not in t:
            raise SystemExit("append_run_index block not found")
        t = t.replace(old_idx, new_idx, 1)

    # summary extras
    old_sum = """        \"n_events\": len(events),
        \"n_fills\": len(result.fills),
"""
    new_sum = """        \"n_events\": len(events),
        \"with_bagua\": bagua_enabled,
        \"bagua_filter_mode\": bagua_filter_mode,
        \"bagua_filter_label\": (
            bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None
        ),
        \"n_signals_before_bagua\": bagua_n_before if bagua_enabled else None,
        \"n_signals_after_bagua\": bagua_n_after if bagua_enabled else None,
        \"n_fills\": len(result.fills),
"""
    if '"n_signals_before_bagua": bagua_n_before if bagua_enabled else None,\n        "n_fills"' not in t:
        if old_sum not in t:
            raise SystemExit("summary n_events block not found")
        t = t.replace(old_sum, new_sum, 1)

    p.write_text(t, encoding="utf-8")
    print("OK backtest.py")


def patch_reports() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "reports.py"
    t = p.read_text(encoding="utf-8")

    old_sig = """    sig_by_code: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    if events:
        for e in events:
            try:
                sig_by_code[e.std_code].append((int(e.date), str(e.indicator_id or \"\")))
            except Exception:
                continue
        for k in sig_by_code:
            sig_by_code[k].sort(key=lambda x: x[0])

    def find_signal(code: str, buy_date: int) -> Tuple[Optional[int], str]:
        arr = sig_by_code.get(code) or []
        best: Optional[Tuple[int, str]] = None
        for d, ind in arr:
            if d <= buy_date:
                best = (d, ind)
            else:
                break
        if best:
            return best[0], best[1]
        return None, \"\"
"""

    new_sig = """    # (date, indicator_id, bagua_full_name, bagua_yao_name, bagua_judgement)
    sig_by_code: Dict[str, List[Tuple[int, str, str, str, str]]] = defaultdict(list)
    if events:
        for e in events:
            try:
                bg = e.bagua or {}
                if not isinstance(bg, dict):
                    bg = {}
                sig_by_code[e.std_code].append(
                    (
                        int(e.date),
                        str(e.indicator_id or \"\"),
                        str(bg.get(\"full_name\") or bg.get(\"gua_name\") or \"\"),
                        str(bg.get(\"yao_name\") or \"\"),
                        str(bg.get(\"market_judgement\") or \"\").replace(\"\\n\", \" | \"),
                    )
                )
            except Exception:
                continue
        for k in sig_by_code:
            sig_by_code[k].sort(key=lambda x: x[0])

    def find_signal(code: str, buy_date: int) -> Tuple[Optional[int], str, str, str, str]:
        arr = sig_by_code.get(code) or []
        best: Optional[Tuple[int, str, str, str, str]] = None
        for row in arr:
            d = row[0]
            if d <= buy_date:
                best = row
            else:
                break
        if best:
            return best[0], best[1], best[2], best[3], best[4]
        return None, \"\", \"\", \"\", \"\"
"""

    if "bagua_full_name" not in t[t.find("def pair_round_trips") : t.find("def pair_round_trips") + 800]:
        if old_sig not in t:
            raise SystemExit("pair_round_trips signal index not found")
        t = t.replace(old_sig, new_sig, 1)

    # Replace find_signal usages that unpack 2 values
    # Pattern: sig_d, ind = find_signal(...)
    if "sig_d, ind, gua, yao, judge = find_signal" not in t:
        t = t.replace(
            "sig_d, ind = find_signal(code, buy_date)",
            "sig_d, ind, gua, yao, judge = find_signal(code, buy_date)",
        )

    # Enrich trip dicts that have "指标": ind
    # closed trip block
    old_closed = """                    \"信号日期\": sig_d if sig_d is not None else \"\",
                    \"指标\": ind,
                    \"买入日期\": buy_date,
"""
    new_closed = """                    \"信号日期\": sig_d if sig_d is not None else \"\",
                    \"指标\": ind,
                    \"卦名\": gua,
                    \"爻位\": yao,
                    \"卦象简判\": judge,
                    \"买入日期\": buy_date,
"""
    if '"卦名": gua' not in t:
        if old_closed not in t:
            raise SystemExit("closed trip fields not found")
        # may appear twice (closed + open) — replace all
        t = t.replace(old_closed, new_closed)

    # open trip may already have been replaced if same block; check orphan open without 卦名 near 未平仓
    # trade_fields
    old_tf = """trade_fields = [
        \"序号\",
        \"证券代码\",
        \"代码\",
        \"信号日期\",
        \"指标\",
        \"买入日期\",
"""
    new_tf = """trade_fields = [
        \"序号\",
        \"证券代码\",
        \"代码\",
        \"信号日期\",
        \"指标\",
        \"卦名\",
        \"爻位\",
        \"卦象简判\",
        \"买入日期\",
"""
    if '"卦名",' not in t[t.find("trade_fields") : t.find("trade_fields") + 400]:
        if old_tf not in t:
            raise SystemExit("trade_fields not found")
        t = t.replace(old_tf, new_tf, 1)

    # excel headers
    old_h = """headers = [
        \"序号\",
        \"证券代码\",
        \"代码\",
        \"信号日期\",
        \"指标\",
        \"买入日期\",
"""
    new_h = """headers = [
        \"序号\",
        \"证券代码\",
        \"代码\",
        \"信号日期\",
        \"指标\",
        \"卦名\",
        \"爻位\",
        \"卦象简判\",
        \"买入日期\",
"""
    if old_h in t and '"卦名",' not in t[t.find("headers = [") : t.find("headers = [") + 300]:
        t = t.replace(old_h, new_h, 1)

    # excel row building
    old_row = """        row = [
            t.get(\"序号\"),
            t.get(\"证券代码\"),
            t.get(\"代码\"),
            _fmt_date(t.get(\"信号日期\")),
            t.get(\"指标\"),
            _fmt_date(t.get(\"买入日期\")),
"""
    new_row = """        row = [
            t.get(\"序号\"),
            t.get(\"证券代码\"),
            t.get(\"代码\"),
            _fmt_date(t.get(\"信号日期\")),
            t.get(\"指标\"),
            t.get(\"卦名\"),
            t.get(\"爻位\"),
            t.get(\"卦象简判\"),
            _fmt_date(t.get(\"买入日期\")),
"""
    if old_row in t:
        t = t.replace(old_row, new_row, 1)

    # widths for extra cols
    old_w = "    widths = [6, 16, 10, 12, 18, 12, 10, 12, 10, 8, 12, 12, 12, 14, 10, 10, 10, 10, 8, 16, 10]\n"
    new_w = "    widths = [6, 16, 10, 12, 18, 14, 10, 36, 12, 10, 12, 10, 8, 12, 12, 12, 14, 10, 10, 10, 10, 8, 16, 10]\n"
    if old_w in t:
        t = t.replace(old_w, new_w, 1)

    # summary rows: bagua filter info
    old_sum_row = '        ("指标", ",".join(repro.get("indicator_ids") or [])),\n'
    new_sum_row = (
        '        ("指标", ",".join(repro.get("indicator_ids") or [])),\n'
        '        ("八卦过滤", repro.get("bagua_filter_label") or ("否" if not repro.get("with_bagua") else "是")),\n'
        '        ("八卦白名单", ",".join(repro.get("bagua_allowlist") or []) if repro.get("bagua_allowlist") else ""),\n'
        '        ("过滤前信号数", repro.get("n_signals_before_bagua") if repro.get("n_signals_before_bagua") is not None else ""),\n'
        '        ("过滤后信号数", repro.get("n_signals_after_bagua") if repro.get("n_signals_after_bagua") is not None else ""),\n'
    )
    if '"八卦过滤"' not in t:
        if old_sum_row not in t:
            raise SystemExit("summary indicator row not found")
        t = t.replace(old_sum_row, new_sum_row, 1)

    # notes lines
    old_note = '        "交易明细：按 FIFO 撮合买卖（多仓兼容）；信号日期取买入日或之前最近信号。",\n'
    new_note = (
        '        "交易明细：按 FIFO 撮合买卖（多仓兼容）；信号日期取买入日或之前最近信号。",\n'
        '        "启用八卦时默认按「最佳3爻」过滤信号：地雷复|初九、地风升|初六、地天泰|初九。",\n'
        '        "卦名/爻位/卦象简判 来自信号日 OHLC 标注；过滤后仅保留白名单卦爻。",\n'
    )
    if "最佳3爻" not in t:
        if old_note not in t:
            # try alternate
            pass
        else:
            t = t.replace(old_note, new_note, 1)

    p.write_text(t, encoding="utf-8")
    print("OK reports.py")


def patch_api() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "api.py"
    t = p.read_text(encoding="utf-8")
    if "bagua_filter_mode" not in t:
        t = t.replace(
            "    with_bagua: bool = False\n",
            "    with_bagua: bool = False\n"
            "    bagua_filter_mode: Optional[str] = None  # default best3 when with_bagua\n",
            1,
        )
        t = t.replace(
            "            with_bagua=payload.with_bagua,\n",
            "            with_bagua=payload.with_bagua,\n"
            "            bagua_filter_mode=payload.bagua_filter_mode,\n",
            1,
        )
        p.write_text(t, encoding="utf-8")
        print("OK api.py")
    else:
        print("api.py already has bagua_filter_mode")


def patch_cli() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "cli.py"
    t = p.read_text(encoding="utf-8")
    changed = False
    if "bagua_filter_mode" not in t:
        # add arg near --with-bagua
        if 'sp.add_argument("--with-bagua", action="store_true")' in t:
            t = t.replace(
                'sp.add_argument("--with-bagua", action="store_true")',
                'sp.add_argument("--with-bagua", action="store_true")\n'
                '    sp.add_argument(\n'
                '        "--bagua-filter-mode",\n'
                '        default=None,\n'
                '        help="when --with-bagua: default best3 (最佳3爻)",\n'
                "    )",
            )
            changed = True
        # BacktestRequest construction
        if "with_bagua=bool(getattr(args, \"with_bagua\", False))," in t and "bagua_filter_mode" not in t:
            t = t.replace(
                'with_bagua=bool(getattr(args, "with_bagua", False)),',
                'with_bagua=bool(getattr(args, "with_bagua", False)),\n'
                '        bagua_filter_mode=getattr(args, "bagua_filter_mode", None),',
            )
            changed = True
        if changed:
            p.write_text(t, encoding="utf-8")
            print("OK cli.py")
        else:
            print("cli.py: no change (patterns differ)")
    else:
        print("cli.py already patched")


def patch_ui() -> None:
    p = ROOT / "wtpy" / "apps" / "astock" / "web" / "static" / "index.html"
    t = p.read_text(encoding="utf-8")
    # Ensure body includes bagua_filter_mode when hasBagua
    old = "          with_bagua: hasBagua,\n"
    new = "          with_bagua: hasBagua,\n          bagua_filter_mode: hasBagua ? \"best3\" : null,\n"
    if "bagua_filter_mode" not in t:
        if old not in t:
            # try different spacing
            if "with_bagua: hasBagua," in t:
                t = t.replace(
                    "with_bagua: hasBagua,",
                    'with_bagua: hasBagua,\n          bagua_filter_mode: hasBagua ? "best3" : null,',
                    1,
                )
            else:
                raise SystemExit("UI with_bagua not found")
        else:
            t = t.replace(old, new, 1)
        p.write_text(t, encoding="utf-8")
        print("OK index.html")
    else:
        print("index.html already has bagua_filter_mode")


def main() -> None:
    patch_backtest()
    patch_reports()
    patch_api()
    patch_cli()
    patch_ui()
    print("all patches done")


if __name__ == "__main__":
    main()
