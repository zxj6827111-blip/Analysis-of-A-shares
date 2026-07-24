"""CLI entry for wtpy.apps.astock."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence

from .config import AStockConfig, get_default_config
from .data.calendar import TradeCalendar
from .data.data_store import DataStore, sha256_file, atomic_write_json
from .data.catalog import rebuild_catalog_from_storage, selected_universe_sha, file_sha_or_empty
from .indicators.tn6_importer import (
    load_source_map, save_source_map, file_sha256, prune_invalid_source_map,
    confirm_source_pair, resolve_formula_audit, scan_tn6_dir,
)
from .data.tdx_reader import TdxDayReader, DayBar
from .data.universe import AShareUniverse, is_ashare_code
from .data.adjustments import (
    build_factor_series,
    factor_manifest_sha,
    formal_adjustment_ready,
)
from .indicators.registry import IndicatorRegistry
from .indicators.tn6_importer import import_tn6_with_source, load_source_map, save_source_map
from .indicators.compiler import compile_formula
from .bagua.calculator import BaguaCalculator
from .study import (
    SignalEvent,
    attach_bagua,
    bagua_condition_study,
    bars_dict_from_day,
    bars_dict_from_period,
    build_period_bars,
    combine_signals,
    compute_indicator_signal,
    compute_v5_dwm_resonance,
    day_bars_to_adj,
    signal_dates,
    study_indicator_events,
)
from .strategy import PortfolioBacktester
from .reports import write_backtest_csv, write_signals_csv, write_stats_csv


def _astock_code_sha() -> str:
    """Deterministic SHA256 of wtpy/apps/astock source tree (relative paths + content)."""
    import hashlib
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    files = sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".json"} and "__pycache__" not in p.parts],
        key=lambda p: str(p.relative_to(root)).replace("\\", "/"),
    )
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()



def _cfg_from_args(args: argparse.Namespace) -> AStockConfig:
    overrides = {}
    if getattr(args, "tdx_root", None):
        overrides["tdx_root"] = Path(args.tdx_root)
    if getattr(args, "storage", None):
        overrides["storage_root"] = Path(args.storage)
    if getattr(args, "indicator_dir", None):
        overrides["indicator_dir"] = Path(args.indicator_dir)
    return get_default_config(**overrides)


def cmd_list_indicators(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    from .service.rules import RuleService

    rows = RuleService(cfg).list_rules(include_archived=False)
    # keep legacy shape fields
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "status": r["compile_status"],
                "output": r["output_type"],
                "backtestable": r["backtestable"],
                "aliases": r.get("aliases"),
                "package_sha256": (r.get("package_sha256") or "")[:16] if r.get("package_sha256") else "",
                "deps": r.get("dependencies"),
                "failure": r.get("failure_reason"),
                "source": r.get("source"),
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_import_indicator(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    mapping, spec = import_tn6_with_source(
        Path(args.tn6),
        Path(args.source),
        cfg.mapping_path,
        note=args.note or "explicit CLI pairing",
    )
    # refresh registry
    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    reg.register(spec)
    reg.save(cfg.registry_path)
    print(
        json.dumps(
            {"mapping": mapping, "spec": spec.to_dict()},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_validate_indicator(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    try:
        spec = reg.get(args.indicator_id)
    except KeyError:
        print(f"indicator not found: {args.indicator_id}", file=sys.stderr)
        return 1
    info = {
        "id": spec.id,
        "status": spec.compile_status,
        "backtestable": spec.backtestable,
        "failure": spec.failure_reason,
        "dependencies": spec.dependencies,
    }
    if spec.formula_text:
        cr = compile_formula(spec.formula_text, indicator_id=spec.id)
        info["compile_ok"] = cr.ok
        info["compile_error"] = cr.error
        if cr.compiled:
            info["outputs"] = cr.compiled.outputs
            info["functions"] = sorted(cr.compiled.used_functions)
            info["cross_period"] = [
                r.raw for r in cr.compiled.cross_period_refs
            ]
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("compile_ok") is False:
        return 1
    if spec.compile_status == "invalid":
        return 1
    if spec.compile_status == "ready":
        return 0
    # source_required / unsupported / other are not backtest-ready
    return 2


def cmd_inspect_data(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    reader = TdxDayReader(cfg.tdx_root)
    sh = list(cfg.sh_lday.glob("*.day")) if cfg.sh_lday.exists() else []
    sz = list(cfg.sz_lday.glob("*.day")) if cfg.sz_lday.exists() else []
    ash_sh = [p for p in sh if is_ashare_code(p.stem)]
    ash_sz = [p for p in sz if is_ashare_code(p.stem)]
    cal = None
    cal_info = {}
    try:
        cal = TradeCalendar.from_tdx(cfg.tdx_root)
        cal_info = {
            "count": len(cal),
            "first": cal.dates[0],
            "last": cal.dates[-1],
        }
    except Exception as e:  # noqa: BLE001
        cal_info = {"error": str(e)}

    samples = {}
    for code in ("sh600000", "sz000001"):
        try:
            bars, issues = reader.read(code)
            samples[code] = {
                "n": len(bars),
                "first": bars[0].to_dict() if bars else None,
                "last": bars[-1].to_dict() if bars else None,
                "n_issues": len(issues),
            }
        except Exception as e:  # noqa: BLE001
            samples[code] = {"error": str(e)}

    print(
        json.dumps(
            {
                "tdx_root": str(cfg.tdx_root),
                "sh_day_files": len(sh),
                "sz_day_files": len(sz),
                "ashare_sh": len(ash_sh),
                "ashare_sz": len(ash_sz),
                "calendar": cal_info,
                "samples": samples,
                "read_only_note": "D:\\通达信 is read-only for this tool",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_import_data(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    store = DataStore(cfg.storage_root)
    codes = None
    if args.codes:
        codes = set(args.codes.split(","))
    limit = args.limit
    items = []
    reader = TdxDayReader(cfg.tdx_root)
    count = 0
    for raw, path in reader.iter_files(include_bj=False):
        if not is_ashare_code(raw):
            continue
        if codes and raw not in codes and raw[2:] not in codes:
            continue
        items.append(store.import_day_file(path, write_dsb=not args.skip_dsb, fetch_factors=not getattr(args, 'skip_factors', False)))
        count += 1
        if limit and count >= limit:
            break
        if args.verbose and count % 100 == 0:
            print(f"imported {count}...", file=sys.stderr)
    # Global catalog isolation:
    # full import (no --codes/--limit) updates global manifest/universe;
    # selection imports never overwrite global metadata.
    is_full = codes is None and not limit
    out = {
        "imported": len(items),
        "ok": sum(1 for x in items if x.status == "ok"),
        "selection_only": (not is_full),
    }
    if is_full:
        uni = AShareUniverse.from_tdx_dirs(cfg.sh_lday, cfg.sz_lday)
        uni.save(cfg.universe_path)
        try:
            cal = TradeCalendar.from_tdx(cfg.tdx_root)
            cal.save(cfg.calendar_path)
        except Exception as e:  # noqa: BLE001
            print(f"calendar error: {e}", file=sys.stderr)
        man = store.save_manifest(items)
        cfg.save()
        out.update({
            "manifest": str(man),
            "universe": str(cfg.universe_path),
            "calendar": str(cfg.calendar_path),
            "global_manifest_sha": file_sha_or_empty(cfg.manifest_path),
            "global_universe_sha": file_sha_or_empty(cfg.universe_path),
            "global_universe_count": len(uni),
        })
    else:
        from .data.data_store import atomic_write_json
        sel_dir = Path(cfg.storage_root) / "selections"
        sel_dir.mkdir(parents=True, exist_ok=True)
        ok_codes = sorted({m.std_code for m in items if m.status == "ok"})
        sel_path = sel_dir / f"import_sel_{int(time.time())}.json"
        atomic_write_json(sel_path, {
            "codes": ok_codes,
            "count": len(ok_codes),
            "selected_universe_sha": selected_universe_sha(ok_codes),
            "items": [x.to_dict() for x in items],
        })
        out.update({
            "selection_path": str(sel_path),
            "selected_codes_count": len(ok_codes),
            "selected_universe_sha": selected_universe_sha(ok_codes),
            "global_manifest_sha": file_sha_or_empty(cfg.manifest_path),
            "global_universe_sha": file_sha_or_empty(cfg.universe_path),
            "note": "Selection import did not modify global manifest/universe.",
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _load_registry(cfg: AStockConfig) -> IndicatorRegistry:
    return IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)


def _select_universe(cfg: AStockConfig, codes: Optional[str]) -> List[str]:
    if codes:
        out = []
        for c in codes.split(","):
            c = c.strip()
            if not c:
                continue
            if c.startswith("SSE.") or c.startswith("SZSE."):
                out.append(c)
            elif c.startswith("sh") or c.startswith("sz"):
                from .data.universe import to_std_code

                out.append(to_std_code(c))
            else:
                from .data.universe import to_std_code

                out.append(to_std_code(c))
        return out
    if cfg.universe_path.exists():
        return AShareUniverse.load(cfg.universe_path).codes()
    # fallback small default
    return ["SSE.STK.600000", "SZSE.STK.000001"]


def cmd_build_signals(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    reg = _load_registry(cfg)
    store = DataStore(cfg.storage_root)
    period = (args.period or "DAY").upper()
    codes = _select_universe(cfg, args.codes)
    specs = [reg.get(iid) for iid in args.indicator]
    research_unadj = bool(getattr(args, "research_unadjusted", False))
    start = int(args.start) if getattr(args, "start", None) else None
    end = int(args.end) if getattr(args, "end", None) else None

    run_id = args.run_id or f"sig_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    out_dir = cfg.output_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    all_events: List[SignalEvent] = []
    errors = []
    period_bars_map = {}
    day_raw_map = {}
    day_adj_map = {}
    factor_series = []

    for code in codes:
        try:
            day_raw = store.load_symbol(code)
        except FileNotFoundError:
            reader = TdxDayReader(cfg.tdx_root)
            raw = ("sh" if code.startswith("SSE") else "sz") + code.split(".")[-1]
            try:
                day_raw, _ = reader.read(raw)
            except Exception as e:
                errors.append({"code": code, "error": str(e)})
                continue
        day_raw_map[code] = day_raw
        dates = [b.date for b in day_raw]
        series = build_factor_series(code, dates, adj_root=cfg.adj_root, prefer_baostock=True)
        factor_series.append(series)
        import numpy as np
        fac = np.array(series.factors, dtype=float)
        day_adj = day_bars_to_adj(day_raw, fac)  # PIT research
        # CLI study path: prefer standard_qfq for indicators when available
        try:
            from .study import day_bars_to_standard_qfq as _to_qfq
            day_qfq = _to_qfq(day_raw, fac)
        except Exception:
            day_qfq = day_adj
        day_adj_map[code] = day_adj
        # indicator bars: raw if research-unadjusted else adjusted
        day_for_ind = day_raw if research_unadj else day_qfq

        asof = day_raw[-1].date if day_raw else None
        # bagua always uses period raw OHLC
        if period == "DWM":
            trade_period = "DAY"
        else:
            trade_period = period
        p_bars_ind = build_period_bars(day_for_ind, trade_period, asof=asof, include_open=False)
        p_bars_raw = build_period_bars(day_raw, trade_period, asof=asof, include_open=False)
        period_bars_map[code] = p_bars_raw
        if trade_period == "DAY":
            bars = bars_dict_from_day(p_bars_ind)
        else:
            bars = bars_dict_from_period(p_bars_ind)
        dates_arr = bars["date"]

        per_ind_signals = []
        for spec in specs:
            if spec.id == "bagua_ohlc" or spec.output_type == "classification":
                continue
            sig, err = compute_indicator_signal(spec, bars)
            if err:
                errors.append({"code": code, "indicator": spec.id, "error": err})
                continue
            per_ind_signals.append((spec, sig))
            for d in signal_dates(dates_arr, sig):
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                all_events.append(SignalEvent(std_code=code, date=d, period=trade_period, indicator_id=spec.id))

        if args.combine and len(per_ind_signals) >= 2:
            combined = combine_signals([s for _, s in per_ind_signals], mode=args.combine)
            for d in signal_dates(dates_arr, combined):
                if start and d < start: continue
                if end and d > end: continue
                all_events.append(SignalEvent(std_code=code, date=d, period=trade_period, indicator_id=f"combine_{args.combine}"))

        if args.dwm or period == "DWM":
            base = None
            for spec in specs:
                if spec.compile_status == "ready" and spec.formula_text and "MIN60" not in (spec.dependencies or []):
                    base = spec
                    break
            if base:
                w_bars = build_period_bars(day_for_ind, "WEEK", asof=asof)
                m_bars = build_period_bars(day_for_ind, "MONTH", asof=asof)
                d_dict = bars_dict_from_day(day_for_ind)
                w_dict = bars_dict_from_period(w_bars)
                m_dict = bars_dict_from_period(m_bars)
                ds, e1 = compute_indicator_signal(base, d_dict)
                ws, e2 = compute_indicator_signal(base, w_dict)
                ms, e3 = compute_indicator_signal(base, m_dict)
                if ds is not None and ws is not None and ms is not None:
                    res = compute_v5_dwm_resonance(day_for_ind, ds, w_bars, ws, m_bars, ms)
                    for d in signal_dates(d_dict["date"], res):
                        if start and d < start: continue
                        if end and d > end: continue
                        all_events.append(SignalEvent(std_code=code, date=d, period="DWM", indicator_id=f"{base.id}_dwm", is_dwm=True))
                else:
                    errors.append({"code": code, "dwm_errors": [e1, e2, e3]})

    formal_ok, adj_msg = formal_adjustment_ready(factor_series)
    if not formal_ok and not research_unadj:
        meta = {
            "run_id": run_id,
            "status": "no_go",
            "reason": adj_msg,
            "note": "Signals may still be research-only; formal mode requires factors or --research-unadjusted",
            "n_events": len(all_events),
            "errors": errors[:50],
        }
        # still write events but mark
        if args.with_bagua:
            calc = BaguaCalculator.from_json(cfg.bagua_json)
            attach_bagua(all_events, period_bars_map, calc)
        sig_path = write_signals_csv(out_dir / "signals.csv", all_events)
        meta["signals_csv"] = str(sig_path)
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    if args.with_bagua:
        calc = BaguaCalculator.from_json(cfg.bagua_json)
        attach_bagua(all_events, period_bars_map, calc)

    sig_path = write_signals_csv(out_dir / "signals.csv", all_events)
    meta = {
        "run_id": run_id,
        "period": period,
        "indicators": [s.id for s in specs],
        "n_events": len(all_events),
        "n_codes": len(codes),
        "errors": errors[:50],
        "signals_csv": str(sig_path),
        "adjustment_status": adj_msg,
        "factor_manifest_sha": factor_manifest_sha(factor_series),
        "research_unadjusted": research_unadj,
        "start": start,
        "end": end,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0

def cmd_backtest(args: argparse.Namespace) -> int:
    """Backtest via service layer (shared with web API)."""
    from .service.backtest import BacktestRequest, BacktestService

    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    codes = None
    if getattr(args, "codes", None):
        codes = [c.strip() for c in str(args.codes).split(",") if c.strip()]
    period = (args.period or "DAY").upper()
    req = BacktestRequest(
        rule_ids=list(args.indicator),
        period=period,
        hold=int(args.hold or 1),
        entry_lag=int(getattr(args, "entry_lag", 1) or 1),
        signal_weekdays=getattr(args, "signal_weekdays", None),
        buy_on=getattr(args, "buy_on", "open"),
        sell_on=getattr(args, "sell_on", "open"),
        buy_weekday=getattr(args, "buy_weekday", None),
        exit_weekday=getattr(args, "exit_weekday", None),
        combine=getattr(args, "combine", None),
        codes=codes,
        start=int(args.start) if args.start else None,
        end=int(args.end) if args.end else None,
        dwm=bool(getattr(args, "dwm", False)),
        with_bagua=bool(getattr(args, "with_bagua", False)),
        bagua_filter_mode=getattr(args, "bagua_filter_mode", None),
        account_mode=getattr(args, "account_mode", None) or "portfolio",
        research_unadjusted=bool(getattr(args, "research_unadjusted", False)),
        research_unconfirmed_formula=bool(getattr(args, "research_unconfirmed_formula", False)),
        stop_loss=getattr(args, "stop_loss", None),
        take_profit=getattr(args, "take_profit", None),
        run_id=getattr(args, "run_id", None),
    )
    try:
        summary = BacktestService(cfg).run(req)
    except ValueError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    st = summary.get("status")
    if st == "no_go":
        return 3
    if st == "rejected_unconfirmed_formula":
        return 4
    if summary.get("error") and st not in ("ok", "research_unadjusted", "research_unconfirmed_formula"):
        return 2
    return 0


def cmd_bagua_study(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    calc = BaguaCalculator.from_json(cfg.bagua_json)
    store = DataStore(cfg.storage_root)
    codes = _select_universe(cfg, args.codes)
    period = (args.period or "DAY").upper()
    all_stats = []
    for code in codes:
        try:
            day_raw = store.load_symbol(code)
        except FileNotFoundError:
            reader = TdxDayReader(cfg.tdx_root)
            raw = ("sh" if code.startswith("SSE") else "sz") + code.split(".")[-1]
            day_raw, _ = reader.read(raw)
        # bagua uses RAW OHLC; week/month use aggregated OHLC
        asof = day_raw[-1].date if day_raw else None
        stats = bagua_condition_study(day_raw, calc, period=period, asof=asof)
        for s in stats:
            s.key = f"{code}|{s.key}"
            all_stats.append(s)

    run_id = args.run_id or f"bagua_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    out_dir = cfg.output_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = write_stats_csv(out_dir / "bagua_stats.csv", all_stats)
    meta = {
        "run_id": run_id,
        "period": period,
        "n_stats": len(all_stats),
        "path": str(path),
        "note": "Bagua condition study only; uses raw OHLC; week/month use aggregated bars; no trade signals from classical text.",
        "bagua_sha": __import__("hashlib").sha256(Path(cfg.bagua_json).read_bytes()).hexdigest(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0

def cmd_report(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    run_dir = cfg.output_root / args.run_id
    if not run_dir.exists():
        print(f"run not found: {run_dir}", file=sys.stderr)
        return 1
    files = sorted(str(p.relative_to(cfg.output_root)) for p in run_dir.rglob("*") if p.is_file())
    meta = {}
    for name in ("meta.json", "metrics.json", "run_meta.json"):
        p = run_dir / name
        if p.exists():
            meta[name] = json.loads(p.read_text(encoding="utf-8"))
    print(json.dumps({"run_id": args.run_id, "files": files, "meta": meta}, ensure_ascii=False, indent=2))
    return 0




def cmd_min60_status(args: argparse.Namespace) -> int:
    """Probe local TDX minute (.lc1) coverage; never enables formal MIN60 silently."""
    cfg = _cfg_from_args(args)
    sh = cfg.tdx_root / "vipdoc" / "sh" / "minline"
    sz = cfg.tdx_root / "vipdoc" / "sz" / "minline"
    def _probe(d: Path):
        if not d.exists():
            return {"exists": False, "n_files": 0}
        files = list(d.glob("*.lc1"))
        sizes = [f.stat().st_size for f in files[:50]]
        # 32-byte records typical
        recs = [s // 32 for s in sizes if s % 32 == 0]
        return {
            "exists": True,
            "n_files": len(files),
            "sample_records_max": max(recs) if recs else 0,
            "sample_records_min": min(recs) if recs else 0,
            "note": "Presence of .lc1 does not imply multi-year MIN60 history.",
        }
    payload = {
        "sh_minline": _probe(sh),
        "sz_minline": _probe(sz),
        "formal_min60": "No-Go",
        "reason": (
            "Plan forbids substituting WEEK/MONTH for MIN60. "
            "Formal long-horizon MIN60 remains No-Go until multi-year minute history "
            "is verified end-to-end; use list-indicators status=unsupported for MIN60 formulas."
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_pair_735(args: argparse.Namespace) -> int:
    """Explicit pair 735 package with human formula source via unique *735* globs.

    Does not reverse-engineer .tn6. Does not hardcode Chinese filenames.
    """
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    ind = Path(cfg.indicator_dir)
    # Resolve tn6
    if getattr(args, "tn6", None):
        tn6_list = [Path(args.tn6)]
    else:
        tn6_list = sorted(ind.glob("*735*.tn6"))
    if not tn6_list:
        print(json.dumps({"error": "no *735*.tn6 under indicator_dir", "indicator_dir": str(ind)}, ensure_ascii=False))
        return 1
    if len(tn6_list) > 1 and not getattr(args, "tn6", None):
        print(json.dumps({
            "error": "multiple *735*.tn6 candidates; pass --tn6 explicitly",
            "candidates": [str(p) for p in tn6_list],
        }, ensure_ascii=False, indent=2))
        return 1
    tn6 = tn6_list[0]
    if not tn6.exists():
        print(json.dumps({"error": f"tn6 missing: {tn6}"}, ensure_ascii=False))
        return 1

    # Resolve source txt
    if getattr(args, "source", None):
        src_list = [Path(args.source)]
    else:
        src_list = sorted(ind.glob("*735*.txt"))
    if not src_list:
        print(json.dumps({"error": "no *735*.txt under indicator_dir", "indicator_dir": str(ind)}, ensure_ascii=False))
        return 1
    if len(src_list) > 1 and not getattr(args, "source", None):
        print(json.dumps({
            "error": "multiple *735*.txt candidates; pass --source explicitly",
            "candidates": [str(p) for p in src_list],
        }, ensure_ascii=False, indent=2))
        return 1
    src = src_list[0]
    if not src.exists():
        print(json.dumps({"error": f"source missing: {src}"}, ensure_ascii=False))
        return 1

    note = args.note or (
        "explicit path pairing only; formula_provenance=user_confirmation_required "
        "until confirm-indicator-source is run; not reverse-engineered from tn6"
    )
    mapping, spec = import_tn6_with_source(tn6, src, cfg.mapping_path, note=note)
    mapping = dict(mapping)
    # keep unconfirmed defaults from pair_source; do not auto-confirm
    mapping["package_file"] = str(tn6.resolve())
    mapping["formula_provenance"] = "user_confirmation_required"
    mapping["source_pair_status"] = "paired_unconfirmed"
    mapping["formal_backtest_allowed"] = False
    mapping["research_backtest_allowed"] = True
    full = load_source_map(cfg.mapping_path)
    full[mapping["package_sha256"]] = mapping
    save_source_map(cfg.mapping_path, full)

    reg = IndicatorRegistry.bootstrap(cfg.indicator_dir, cfg.mapping_path)
    reg.save(cfg.registry_path)
    audit = resolve_formula_audit(mapping, package_sha256=mapping["package_sha256"])
    out = {
        "package_file": str(tn6.resolve()),
        "source_file": str(src.resolve()),
        "package_sha256": mapping.get("package_sha256") or file_sha256(tn6),
        "source_sha256": mapping.get("source_sha256") or file_sha256(src),
        "compile_status": spec.compile_status,
        "backtestable": False,  # formal not allowed until confirm
        "compile_ready": spec.compile_status == "ready",
        "indicator_id": spec.id,
        "formula_provenance": audit["formula_provenance"],
        "source_pair_status": audit["source_pair_status"],
        "formal_backtest_allowed": audit["formal_backtest_allowed"],
        "research_backtest_allowed": audit["research_backtest_allowed"],
        "note": note,
        "mapping_path": str(cfg.mapping_path),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if spec.compile_status == "ready" else 1





def cmd_rebuild_catalog(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    result = rebuild_catalog_from_storage(cfg.storage_root, tdx_root=cfg.tdx_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_confirm_indicator_source(args: argparse.Namespace) -> int:
    """Explicit user confirmation that a paired source is human-provided."""
    cfg = _cfg_from_args(args)
    if not getattr(args, "confirm_user_provided", False):
        print(json.dumps({"error": "must pass --confirm-user-provided"}, ensure_ascii=False))
        return 1
    tn6 = Path(args.tn6) if args.tn6 else None
    src = Path(args.source) if args.source else None
    if not tn6 or not src:
        print(json.dumps({"error": "--tn6 and --source required"}, ensure_ascii=False))
        return 1
    if not tn6.exists() or not src.exists():
        print(json.dumps({"error": "tn6 or source file missing"}, ensure_ascii=False))
        return 1
    pkg_sha = file_sha256(tn6)
    # ensure pair exists / refresh
    mapping, spec = import_tn6_with_source(
        tn6, src, cfg.mapping_path,
        note=args.note or "pair before confirm",
    )
    entry = confirm_source_pair(
        cfg.mapping_path,
        pkg_sha,
        confirmed_by=args.confirmed_by or "unspecified",
        note=args.note or "",
    )
    audit = resolve_formula_audit(entry, package_sha256=pkg_sha)
    print(json.dumps({"entry": entry, "audit": audit, "spec_id": spec.id}, ensure_ascii=False, indent=2))
    return 0 if audit.get("formal_backtest_allowed") else 1


def cmd_prune_source_map(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(args)
    mapping = load_source_map(cfg.mapping_path)
    before = len(mapping)
    # keep shas that match existing tn6 in indicator dir
    keep = {p.sha256 for p in scan_tn6_dir(cfg.indicator_dir)}
    cleaned = {}
    dropped = []
    for k, v in mapping.items():
        src = v.get("source_file")
        if not src or not Path(src).exists():
            dropped.append({"sha": k, "reason": "missing_source", "source_file": src})
            continue
        # drop pytest temp paths
        if "pytest" in str(src).replace("\\", "/").lower() or "Temp" in str(src):
            # only drop if package not in current indicator dir
            if k not in keep:
                dropped.append({"sha": k, "reason": "temp_or_orphan", "source_file": src})
                continue
        cleaned[k] = v
    save_source_map(cfg.mapping_path, cleaned)
    print(json.dumps({
        "before": before,
        "after": len(cleaned),
        "dropped": dropped,
        "mapping_path": str(cfg.mapping_path),
    }, ensure_ascii=False, indent=2))
    return 0


def _risk_pct_arg(value: str) -> float:
    """argparse type: require 0 < x < 1; never clamp."""
    try:
        v = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid float: {value!r}") from e
    if not (0.0 < v < 1.0):
        raise argparse.ArgumentTypeError(
            f"risk pct must satisfy 0 < value < 1, got {v}"
        )
    return v


def cmd_serve(args: argparse.Namespace) -> int:
    from .api import serve

    cfg = _cfg_from_args(args)
    serve(host=getattr(args, "host", "127.0.0.1"), port=int(getattr(args, "port", 8765) or 8765), cfg=cfg)
    return 0


def build_parser() -> argparse.ArgumentParser:

    p = argparse.ArgumentParser(prog="python -m wtpy.apps.astock")
    p.add_argument("--tdx-root", default=None)
    p.add_argument("--storage", default=None)
    p.add_argument("--indicator-dir", default=None, dest="indicator_dir")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list-indicators")
    sp.set_defaults(func=cmd_list_indicators)

    sp = sub.add_parser("import-indicator")
    sp.add_argument("tn6")
    sp.add_argument("--source", required=True)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_import_indicator)

    sp = sub.add_parser("validate-indicator")
    sp.add_argument("indicator_id")
    sp.set_defaults(func=cmd_validate_indicator)

    sp = sub.add_parser("inspect-data")
    sp.set_defaults(func=cmd_inspect_data)

    sp = sub.add_parser("import-data")
    sp.add_argument("--codes", default=None, help="comma-separated raw codes e.g. sh600000,sz000001")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--skip-dsb", action="store_true")
    sp.add_argument("--skip-factors", action="store_true", help="skip Baostock factor fetch (bulk import)")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_import_data)

    sp = sub.add_parser("build-signals")
    sp.add_argument("--indicator", action="append", required=True)
    sp.add_argument("--period", default="DAY")
    sp.add_argument("--codes", default=None)
    sp.add_argument("--combine", choices=["all", "any"], default=None)
    sp.add_argument("--dwm", action="store_true")
    sp.add_argument("--with-bagua", action="store_true")
    sp.add_argument(
        "--bagua-filter-mode",
        default=None,
        help="when --with-bagua: default best3 (最佳3爻)",
    )
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--research-unadjusted", action="store_true")
    sp.add_argument("--run-id", default=None)
    sp.set_defaults(func=cmd_build_signals)

    sp = sub.add_parser("backtest")
    sp.add_argument("--indicator", action="append", required=True)
    sp.add_argument("--period", default="DAY")
    sp.add_argument("--hold", type=int, default=1)
    sp.add_argument("--entry-lag", type=int, default=1, dest="entry_lag",
                    help="buy at open of N-th trading day after signal (default 1 = T+1)")
    sp.add_argument("--signal-weekdays", default=None, dest="signal_weekdays",
                    help="only trade signals on these weekdays: 1=Mon..7=Sun, e.g. 5 or fri,1,3,5")
    sp.add_argument("--buy-on", default="open", dest="buy_on",
                    choices=["open", "close", "开盘", "收盘"],
                    help="buy at open or close of entry day (default open)")
    sp.add_argument("--sell-on", default="open", dest="sell_on",
                    choices=["open", "close", "开盘", "收盘"],
                    help="sell at open or close of exit day (default open)")
    sp.add_argument("--buy-weekday", default=None, dest="buy_weekday",
                    help="buy on this weekday after signal: 1=Mon..7=Sun or fri (overrides --entry-lag)")
    sp.add_argument("--exit-weekday", default=None, dest="exit_weekday",
                    help="force flat on this weekday after entry (overrides --hold)")
    sp.add_argument("--codes", default=None)
    sp.add_argument("--combine", choices=["all", "any"], default=None)
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--dwm", action="store_true")
    sp.add_argument("--with-bagua", action="store_true")
    sp.add_argument(
        "--bagua-filter-mode",
        default=None,
        help="when --with-bagua: default best3 (最佳3爻)",
    )
    sp.add_argument("--research-unadjusted", action="store_true")
    sp.add_argument("--research-unconfirmed-formula", action="store_true",
                    help="allow research run for paired-but-unconfirmed tn6 formulas")
    sp.add_argument("--account-mode", default="portfolio",
                    choices=["portfolio", "per_symbol", "tdx"],
                    help="portfolio shared cash | per_symbol TDX-style")
    sp.add_argument("--stop-loss", type=_risk_pct_arg, default=None, help="stop loss fraction e.g. 0.03 (0<x<1)")
    sp.add_argument("--take-profit", type=_risk_pct_arg, default=None, help="take profit fraction e.g. 0.08 (0<x<1)")
    sp.add_argument("--run-id", default=None)
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("bagua-study")
    sp.add_argument("--period", default="DAY")
    sp.add_argument("--codes", default=None)
    sp.add_argument("--run-id", default=None)
    sp.set_defaults(func=cmd_bagua_study)

    sp = sub.add_parser("report")
    sp.add_argument("run_id")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("min60-status")
    sp.set_defaults(func=cmd_min60_status)

    sp = sub.add_parser("pair-735")
    sp.add_argument("--tn6", default=None)
    sp.add_argument("--source", default=None)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_pair_735)

    sp = sub.add_parser("rebuild-catalog")
    sp.set_defaults(func=cmd_rebuild_catalog)

    sp = sub.add_parser("confirm-indicator-source")
    sp.add_argument("--tn6", required=True)
    sp.add_argument("--source", required=True)
    sp.add_argument("--confirmed-by", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--confirm-user-provided", action="store_true")
    sp.set_defaults(func=cmd_confirm_indicator_source)

    sp = sub.add_parser("prune-source-map")
    sp.set_defaults(func=cmd_prune_source_map)

    sp = sub.add_parser("serve", help="start A-stock web console (FastAPI)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
