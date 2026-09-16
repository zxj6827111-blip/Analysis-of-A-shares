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
from .data.dataset_store import DatasetStore
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
    day_bars_for_signals,
    day_bars_for_signals_affine,
    day_bars_to_standard_qfq,
    day_bars_to_point_in_time_adjusted,
    signal_dates,
    study_indicator_events,
)
from .strategy import PortfolioBacktester
from .reports import write_backtest_csv, write_signals_csv, write_stats_csv
# 阶段 1/2 契约模块：track-weekly 与 review-weekly 快照层共用枚举/原语，
# 不得在调用方自造同义词（docs/plans/auto-screen-track/contract.md）
from .service import screen_contract as sc
from .service import screen_snapshots as ss
from .service import screen_tracking as tracksvc


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
    period_raw_bars_map = {}
    day_raw_map = {}
    day_adj_map = {}
    factor_series = []

    # NAMELIKE 名称快照：批量一次解析（任一规则用到才解析）
    from .forecast.name_norm import normalize_stock_code as _nsc
    from .service.stock_names import ensure_stock_names_for

    trade_specs_cli = [
        s for s in specs
        if s.id != "bagua_ohlc" and s.output_type == "signal"
    ]
    stock_name_map, _name_snapshot_id = ensure_stock_names_for(cfg, codes, trade_specs_cli)

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
        series = build_factor_series(
            code, dates, adj_root=cfg.adj_root, prefer_baostock=True,
            store=DatasetStore(cfg.market_data_root),
        )
        factor_series.append(series)
        import numpy as np
        fac = np.array(series.factors, dtype=float)
        # PIT research map (audit only); signals use day_bars_for_signals (standard_qfq|raw)
        day_adj = day_bars_to_point_in_time_adjusted(day_raw, fac)
        day_adj_map[code] = day_adj

        from .data.affine_adjust import build_affine_series
        affine = build_affine_series(code, dates, adj_root=cfg.adj_root)
        if affine.quality == "complete" and not affine.is_identity:
            day_for_ind = day_bars_for_signals_affine(
                day_raw,
                affine,
                research_unadjusted=research_unadj,
                signal_adjust="asof_forward_qfq",
                asof_date=end if end else (day_raw[-1].date if day_raw else None),
            )
        else:
            day_for_ind = day_bars_for_signals(
                day_raw,
                fac,
                research_unadjusted=research_unadj,
                signal_adjust="asof_forward_qfq",
                asof_date=end if end else (day_raw[-1].date if day_raw else None),
                dates=dates,
            )

        asof = day_raw[-1].date if day_raw else None
        # bagua uses L1 signal period OHLC (same as indicators), not L2 raw
        if period == "DWM":
            trade_period = "DAY"
        else:
            trade_period = period
        p_bars_ind = build_period_bars(day_for_ind, trade_period, asof=asof, include_open=False)
        p_bars_raw = build_period_bars(day_raw, trade_period, asof=asof, include_open=False)
        period_bars_map[code] = p_bars_ind
        period_raw_bars_map[code] = p_bars_raw
        if trade_period == "DAY":
            bars = bars_dict_from_day(p_bars_ind)
        else:
            bars = bars_dict_from_period(p_bars_ind)
        dates_arr = bars["date"]

        per_ind_signals = []
        failed_in_loop = []
        for spec in specs:
            if spec.id == "bagua_ohlc" or spec.output_type == "classification":
                continue
            sig, err = compute_indicator_signal(
                spec, bars, stock_name=stock_name_map.get(_nsc(code), "")
            )
            if err:
                errors.append({"code": code, "indicator": spec.id, "error": err})
                failed_in_loop.append(spec.id)
                continue
            per_ind_signals.append((spec, sig))
            for d in signal_dates(dates_arr, sig):
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                all_events.append(SignalEvent(std_code=code, date=d, period=trade_period, indicator_id=spec.id))

        # 组合兜底（与 backtest._events_for_code 同口径）：任一参与规则失败
        # → 该票不产生组合信号（失败≠False，组合语义不可靠），记录错误。
        if args.combine and not failed_in_loop and len(per_ind_signals) >= 2:
            combined = combine_signals([s for _, s in per_ind_signals], mode=args.combine)
            for d in signal_dates(dates_arr, combined):
                if start and d < start: continue
                if end and d > end: continue
                all_events.append(SignalEvent(std_code=code, date=d, period=trade_period, indicator_id=f"combine_{args.combine}"))
        elif args.combine and failed_in_loop:
            errors.append({
                "code": code,
                "indicator": f"combine_{args.combine}",
                "error": f"组合信号未产生：参与规则 {failed_in_loop} 计算失败（失败≠False，该票组合语义不可靠）",
            })

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
                ds, e1 = compute_indicator_signal(base, d_dict, stock_name=stock_name_map.get(_nsc(code), ""))
                ws, e2 = compute_indicator_signal(base, w_dict, stock_name=stock_name_map.get(_nsc(code), ""))
                ms, e3 = compute_indicator_signal(base, m_dict, stock_name=stock_name_map.get(_nsc(code), ""))
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
            attach_bagua(all_events, period_raw_bars_map or period_bars_map, calc, bagua_period="WEEK", price_plane="raw")
        sig_path = write_signals_csv(out_dir / "signals.csv", all_events)
        meta["signals_csv"] = str(sig_path)
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    if args.with_bagua:
        calc = BaguaCalculator.from_json(cfg.bagua_json)
        attach_bagua(all_events, period_raw_bars_map or period_bars_map, calc, bagua_period="WEEK", price_plane="raw")

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
        "bagua_ohlc_plane": "L2_trade_price",
        "bagua_ohlc_source": "week_bars_from_raw_days",
        "bagua_period": "WEEK",
        "bagua_price_plane": "raw",
        "start": start,
        "end": end,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def cmd_review_weekly(args: argparse.Namespace) -> int:
    """周五链全市场指标复核（735 / 5日外），结果 JSON 供导出侧读取。

    --rules all 时对全部可执行规则跑一次全市场扫描，并同时产出不可变
    快照 + 发布指针（契约见 docs/plans/auto-screen-track/contract.md）。

    --publish-scope subset（2026-09-16「指定规则补算」）：只对 --rules
    指定的几条规则跑全市场扫描并产出**子集快照**（rules_scope=subset）。
    全市场扫描成本与规则数近似线性（实测单规则约为全量的 1/6~1/10），
    用于"验证某条规则在过去某周选出了什么"。两条硬约束：
    - persist=False：绝不写 ``review_{asof}.json``（那是周五链/导出共享的
      全规则结果，被子集覆盖会让该周导出直接错数据）；
    - 发布走契约的子集护栏（只允许补早于最近发布周的历史空周）。
    """
    import logging

    from .service.indicator_review import run_weekly_review

    # 周五链把本命令 stdout/stderr 重定向到 indicator_review_*.log：
    # 不配 logging 就只剩结尾 JSON，5200 票的进度全丢
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    asof = int(args.asof) if getattr(args, "asof", None) else None
    rules_arg = str(getattr(args, "rules", None) or "").strip()
    scope_req = str(getattr(args, "publish_scope", None) or "auto").strip().lower()
    if scope_req not in ("auto", "all", "subset"):
        print(f"[REVIEW] --publish-scope 非法：{scope_req!r}（auto/all/subset）")
        return 2
    # --publish-scope all 只能配 --rules all：子集规则被当全量发布的话，
    # 读取方（跟踪列表/导出）无法分辨「部分名单」与「全部名单」。
    if scope_req == "all" and rules_arg.lower() != "all":
        print("[REVIEW] --publish-scope all 必须与 --rules all 同时使用")
        return 2
    subset_snapshot = scope_req == "subset"
    rule_ids = (
        [r.strip() for r in rules_arg.split(",") if r.strip()]
        if rules_arg and rules_arg.lower() != "all"
        else None
    )
    if subset_snapshot and not rule_ids:
        print(
            "[REVIEW] --publish-scope subset 必须显式指定 --rules"
            "（逗号分隔规则 ID；--rules all 请改用默认全量快照）"
        )
        return 2
    snapshot_all = rules_arg.lower() == "all"
    snapshot_mode = snapshot_all or subset_snapshot
    if snapshot_all:
        # --rules all：全部可执行规则（含预置两条），供快照层预筛
        from .service.screen_snapshots import list_screenable_rule_ids

        rule_ids = list_screenable_rule_ids(cfg)
        if not rule_ids:
            print("没有可执行的筛选规则（指标目录为空？），退出")
            return 2
    elif subset_snapshot:
        # 规则 ID 白名单校验（fail-closed）：拼错的规则名绝不能静默产出一份
        # "什么都没有"的子集快照。注册表不可用（allowed 为空）时不预判，
        # 交给 run_weekly_review 的 reg.get 抛错（不静默）。
        from .service.screen_snapshots import list_screenable_rule_ids

        allowed = {str(r) for r in list_screenable_rule_ids(cfg)}
        unknown = [r for r in rule_ids or [] if allowed and r not in allowed]
        if unknown:
            print(
                "[REVIEW] 规则不可执行或不存在："
                + ", ".join(unknown)
                + "（--publish-scope subset 只接受可筛选规则）"
            )
            return 2
    codes = args.codes or None
    from .service import heavy_job as _hj

    def _run_review() -> Dict[str, Any]:
        return run_weekly_review(
            cfg,
            asof=asof,
            rule_ids=rule_ids,
            codes=codes,
            force=bool(getattr(args, "force", False)),
            # 子集快照绝不写 review_{asof}.json（全规则共享数据源）
            persist=not subset_snapshot,
        )

    if snapshot_mode:
        # --rules all 是全市场重任务（契约 §7）：持 heavy-job 全局锁执行，
        # 与手动 CLI/网页现算互斥（9/13 OOM 教训：重任务绝不并发）。
        # 锁同线程可重入 → backfill 循环内进程内调用本函数不会自我阻塞。
        _snapshot_asof = asof or 0
        # 子集补算单独记待办键：它的重跑命令与全量不同，待办映射里不猜
        # 命令（api._heavy_job_command 对它返回 None → 标欠账不再自动重试）。
        # 这符合子集补算的定位：用户交互式发起的一次性验证任务，被锁挡住时
        # 如实告知重跑即可，不需要进自动重试队列。
        _lock_key = (
            f"review_subset_{_snapshot_asof}" if subset_snapshot
            else f"review_all_{_snapshot_asof}"
        )
        _locked = _hj.run_with_heavy_lock(cfg, _lock_key, fn=_run_review)
        if _locked.get("skipped_locked"):
            print(
                "[REVIEW] 另一个重任务正在运行（heavy-job 锁被占用），"
                f"本次跳过并记入待办（attempts="
                f"{(_locked.get('pending') or {}).get('attempts')}）"
            )
            if subset_snapshot:
                print(
                    "[REVIEW] 子集补算不进自动重试队列（命令含规则清单，"
                    "待办映射不猜命令）：请在重任务结束后手动重跑本命令。"
                )
            print(json.dumps(
                {"status": "skipped_locked", "reason": "heavy_job_lock_held",
                 "asof": _snapshot_asof},
                ensure_ascii=False, indent=2))
            return 3  # 可重试：待办会由服务运行期退避重试/手动补跑
        summary = _locked.get("value") or {}
    else:
        summary = _run_review()
    # 快照层：全量/子集的复核结果写不可变快照并按契约发布
    # （后台重试/已有指针不自动替换；no_go 不产快照）
    if snapshot_mode and summary.get("status") == "ok":
        from .service import screen_contract as _sc
        from .service.screen_snapshots import build_snapshot_payload, write_and_publish_snapshot

        run_kind = str(getattr(args, "run_kind", None) or "") or (
            "backfill" if subset_snapshot else "weekly_chain"
        )
        payload = build_snapshot_payload(
            cfg, summary, rule_ids=rule_ids or [],
            run_kind=run_kind,
            rules_scope=(
                _sc.RULES_SCOPE_SUBSET if subset_snapshot else _sc.RULES_SCOPE_ALL
            ),
        )
        try:
            pub = write_and_publish_snapshot(cfg, payload)
            summary["snapshot"] = pub
            print("[SNAPSHOT] " + json.dumps(pub, ensure_ascii=False))
        except FileExistsError:
            # 理论不可达（snapshot_id 毫秒+pid+随机）；即便撞上也不失败
            summary["snapshot"] = {"published": False, "reason": "id_collision"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # no_go 是合法结论（复权未就绪），退出码仍为 0；异常路径自然非零
    return 0


# track-weekly 退出码映射（契约 §1）：
#   0 = complete（含合法排除样本）或 no_trading_week
#   3 = pending / blocked_benchmark / data_version_changed（自动重试一次）
#   2 = 配置/快照缺失类（可读错误，不是计算事故）
#   1 = 计算异常（fail-closed，不吞栈）
_TRACK_EXIT_OK = 0
_TRACK_EXIT_CONFIG = 2
_TRACK_EXIT_RETRYABLE = 3
_TRACK_EXIT_FAILED = 1


def track_exit_code_for_completion(completion: str) -> int:
    """服务返回 completion → CLI 退出码（契约 §1，供测试直接断言映射）。"""
    if completion == sc.TRACK_COMPLETE or completion == sc.TRACK_NO_TRADING_WEEK:
        return _TRACK_EXIT_OK
    if completion in (
        sc.TRACK_PENDING,
        sc.TRACK_BLOCKED_BENCHMARK,
        sc.TRACK_DATA_VERSION_CHANGED,
    ):
        return _TRACK_EXIT_RETRYABLE
    if completion == "no_snapshot":
        return _TRACK_EXIT_CONFIG
    return _TRACK_EXIT_FAILED


def _resolve_track_week(
    cfg, args, *, calendar=None, default_previous: bool = False
) -> int:
    """--week 解析。

    显式 --week：8 位数字直用（用法错误由服务层 natural_week_window 的
    anomaly fail-closed 兜住）。
    缺省 + default_previous=False（手动跑）：最近发布快照周。
    缺省 + default_previous=True（周五链/链尾语义）：最近发布快照周的
    **上一信号周**——周五晚刚发布本周新名单，要结算的是上周（本周新
    名单「待下周结算」，契约 §2；审查 B-2：锚 latest 会把窗口未结束
    的本周跑成恒 pending）。
    """
    raw = str(getattr(args, "week", None) or "").strip().replace("-", "")
    if raw:
        if not raw.isdigit() or len(raw) != 8:
            raise ValueError(f"--week 格式无效：{raw!r}（需要 YYYYMMDD）")
        return int(raw)
    snap = ss.latest_published_snapshot(cfg)
    if snap is None:
        raise ValueError("没有已发布快照：先跑 review-weekly --rules all，或显式传 --week")
    week = int(snap.get("week_id") or snap.get("asof") or 0)
    if default_previous:
        prev = tracksvc.previous_signal_week(calendar, week)
        if prev is None:
            raise ValueError("无法定位上一信号周（日历不可用或耗尽），请显式传 --week")
        return prev
    return week


def _record_retryable_pending(cfg, week_id: int, out: Dict[str, Any]) -> None:
    """可重试结果（exit 3）按真实 completion 记待办（唯一记账方 = 本进程）。

    此前由服务端重试循环对 rc=3 一律补记 reason="skipped_locked"：一是与
    子进程内 run_with_heavy_lock 的抢锁记账**重复**（一次失败 attempts +2、
    退避跳档、重试预算减半）；二是 pending/blocked_benchmark/
    data_version_changed 根本不是锁占用，待办原因会误导排查。改由持锁方按
    真实 completion 记账，服务端 runner 对 rc=3 不再补记。
    """
    from .service import screen_contract as _sc

    completion = str(out.get("completion") or "")
    reason = str(out.get("reason") or completion) or "retryable"
    try:
        _sc.record_pending_job(
            Path(cfg.storage_root),
            tracksvc.week_task_key(int(week_id)),
            reason=reason,
        )
        print(
            f"[TRACK] {week_id}: 可重试（{completion}/{reason}），已记入待办"
        )
    except Exception as e:  # noqa: BLE001 — 记账失败不改变退出码语义
        print(f"[TRACK] {week_id}: 待办记账失败（{e}）")


def _run_one_track_week(
    cfg, week_id: int, args, *, hold_lock: bool = True
) -> Tuple[int, Dict[str, Any]]:
    """单周跟踪 + data_version_changed 自动重试一次（契约 §4）。

    重试用同一 cfg：第一次失败说明读版本期间数据面在变，第二次启动时
    捕获的就是新版本；连读两次都不一致才认输返回 3。

    契约 §7：整个计算在 heavy-job 全局锁内执行（与网页现算/其他 CLI
    互斥）；抢锁失败 → 记持久化待办 + 返回 3（可重试），不静默丢弃。
    """
    kwargs = dict(
        run_kind=str(getattr(args, "run_kind", None) or "weekly_chain"),
        force=bool(getattr(args, "force", False)),
    )
    from .service import heavy_job as _hj

    task_key = tracksvc.week_task_key(week_id)

    def _compute() -> Dict[str, Any]:
        out = tracksvc.compute_weekly_tracking(cfg, week_id, **kwargs)
        if out.get("completion") == sc.TRACK_DATA_VERSION_CHANGED:
            print("[TRACK] 数据版本变化，自动重试一次…")
            out = tracksvc.compute_weekly_tracking(cfg, week_id, **kwargs)
        return out

    if not hold_lock:
        # 调用方（backfill）已持锁：同线程重入放行，直接算
        out = _compute()
        rc = track_exit_code_for_completion(str(out.get("completion")))
        if rc == _TRACK_EXIT_RETRYABLE:
            _record_retryable_pending(cfg, week_id, out)
        return rc, out
    locked = _hj.run_with_heavy_lock(cfg, task_key, fn=_compute)
    if locked.get("skipped_locked"):
        # 锁被占用：本周任务不丢——待办已由 run_with_heavy_lock 记账
        # （reason=skipped_locked，单一记账方），服务运行期按 5/15/30 分钟
        # 有界退避重试（或手动重跑），退出码 3 = 可重试
        print(
            f"[TRACK] {week_id}: 另一个重任务正在运行（heavy-job 锁被占用），"
            f"已记入待办（attempts={locked.get('pending', {}).get('attempts')}）"
        )
        return _TRACK_EXIT_RETRYABLE, {
            "completion": "skipped_locked",
            "week_id": int(week_id),
            "task_key": task_key,
            "reason": "heavy_job_lock_held",
            "holder": locked.get("holder"),
        }
    out = locked.get("value") or {}
    rc = track_exit_code_for_completion(str(out.get("completion")))
    if rc == _TRACK_EXIT_RETRYABLE:
        _record_retryable_pending(cfg, week_id, out)
    return rc, out


def _resolve_backfill_rules(cfg, args) -> Optional[List[str]]:
    """``--backfill`` 的规则范围：None = 全部规则（现状）或指定规则 ID 列表。

    校验 fail-closed：拼错/不可执行的规则名绝不放行——否则会静默产出一份
    "该周什么都没有"的子集快照，用户还以为规则当周确实无入选。
    注册表不可用（allowed 为空）时不预判，交由 review 层抛错（不静默）。
    """
    raw = str(getattr(args, "rules", None) or "").strip()
    if not raw:
        return None
    if int(getattr(args, "backfill", 0) or 0) <= 0:
        raise ValueError(
            "--rules 仅与 --backfill 同用（单周跟踪只读已发布快照，"
            "规则范围由该周快照决定）"
        )
    ids: List[str] = []
    for r in (x.strip() for x in raw.split(",")):
        if r and r not in ids:  # 去重保序（同一规则写两遍不该跑两遍）
            ids.append(r)
    if not ids:
        raise ValueError("--rules 为空")
    from .service.screen_snapshots import list_screenable_rule_ids

    allowed = {str(r) for r in list_screenable_rule_ids(cfg)}
    unknown = [r for r in ids if allowed and r not in allowed]
    if unknown:
        raise ValueError("规则不可执行或不存在：" + ", ".join(unknown))
    return ids


def _latest_signal_week(cfg, cal) -> int:
    """数据面最新可得交易日所属的信号周（子集补算护栏；实现见服务层）。

    包装一层只为在 CLI 侧固定参数顺序（日历由调用方一次性加载复用）。
    """
    return tracksvc.latest_signal_week(cfg, cal)


def cmd_track_weekly(args: argparse.Namespace) -> int:
    """入选股票次周跟踪（契约 docs/plans/auto-screen-track/contract.md）。

    stdout 打 JSON 摘要；退出码语义见 track_exit_code_for_completion。
    --backfill N 对过去 N 个周五逐周补：先 review-weekly 补快照
    （run_kind=backfill，仅无指针周），再结算跟踪；已有快照+track 的周
    跳过（断点续跑）。

    --backfill N --rules A,B（2026-09-16）：只补指定规则（子集快照），
    用于"验证某条规则在过去某周选出的股票与表现"；全市场扫描成本与规则数
    近似线性，单规则约全量的 1/6~1/10。硬约束：
    - 只能补**早于数据面最新信号周**且**完全没有发布指针**的周（护栏，
      不抢周五链的地盘）；
    - 子集快照不写 ``review_{asof}.json``（那是全规则共享数据源，被子集
      覆盖会让该周导出直接错数据）。
    """
    import logging as _logging

    # 周五链把 stdout/stderr 重定向到日志文件：不配 logging 全程静默
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .service.screen_snapshots import latest_published_snapshot as _latest_snap

    cfg = _cfg_from_args(args)
    cfg.ensure_dirs()
    backfill = int(getattr(args, "backfill", 0) or 0)
    try:
        # 规则范围（子集补算）解析先于日历：用法错误要立刻失败，不白跑
        subset_rules = _resolve_backfill_rules(cfg, args)
    except ValueError as e:
        print(f"[TRACK] 配置错误: {e}")
        return _TRACK_EXIT_CONFIG

    from .service import heavy_job as _hj

    try:
        # 日历：服务层统一加载（内含 calendar.json 过期检查——数据面
        # 已到 X 但 calendar.json 只到 Y → 自动从数据集推导并并入 delta
        # 交易日；外层若只读 calendar.json 会拿陈旧日历，回填锚点周
        # 不在日历内 → past_signal_weeks 返回空 → "无法枚举回填周"）
        cal = tracksvc._load_calendar_or_none(cfg)

        if backfill > 0:
            # 整轮回填持锁（契约 §7）：一次持锁覆盖 N 周，避免与周五链/
            # 其他 CLI 穿插（锁同线程可重入，循环内的进程内 review-weekly
            # 调用与 _run_one_track_week(hold_lock=False) 不会自我阻塞）
            _lock_key = (
                f"backfill_subset_{backfill}" if subset_rules
                else f"backfill_{backfill}"
            )
            _bf_lock = _hj.HeavyJobLock(Path(cfg.storage_root), task_key=_lock_key)
            try:
                _bf_lock.acquire()
            except _hj.HeavyJobLockHeld as _e:
                _info = _hj.record_lock_skip(Path(cfg.storage_root), _lock_key)
                print(
                    "[TRACK] 另一个重任务正在运行（heavy-job 锁被占用），"
                    f"回填已记入待办（attempts={_info.get('attempts')}）"
                )
                return _TRACK_EXIT_RETRYABLE
            try:
                overall = _cmd_track_backfill(
                    args, cfg, cal, backfill, subset_rules=subset_rules
                )
                if overall == _TRACK_EXIT_RETRYABLE:
                    # 整轮回填未完成（存在 pending 周等）：伞键 re-arm，
                    # 否则它的 recorded_at 仍是旧的 → 立即又到期 → 服务端
                    # 重试循环每 120s 重触发一次整轮回填（含全市场读）。
                    # 子集伞键不做 re-arm：它的重跑命令含规则清单，待办
                    # 映射不猜命令（自动重试不适用），re-arm 只会空转。
                    if not subset_rules:
                        from .service import screen_contract as _sc

                        _info = _sc.record_pending_job(
                            Path(cfg.storage_root), f"backfill_{backfill}",
                            reason="backfill_incomplete_retryable",
                        )
                        print(
                            f"[TRACK] 回填未完成（可重试），伞待办 re-arm"
                            f"（attempts={_info.get('attempts')}，"
                            f"下次 {(_info.get('next_retry_in_minutes') or '耗尽')} 分钟后）"
                        )
                return overall
            finally:
                _bf_lock.release()

        # 单周模式（周五链/链尾调用走 default_previous=True 锚上一信号周；
        # 手动单跑缺省 --week 保持"最近发布快照周"便于即时查看）
        try:
            week_id = _resolve_track_week(
                cfg, args, calendar=cal,
                default_previous=bool(getattr(args, "previous_week", False)),
            )
        except ValueError as e:
            print(f"[TRACK] {e}")
            return _TRACK_EXIT_CONFIG
        rc, out = _run_one_track_week(cfg, week_id, args)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return rc
    except ValueError as e:
        # 配置/日历/日期类可读错误：2（fail-closed 但不冒充异常）
        print(f"[TRACK] 配置错误: {e}")
        return _TRACK_EXIT_CONFIG
    except Exception as e:  # noqa: BLE001 — 计算事故如实记栈后退出 1
        import traceback

        traceback.print_exc()
        print(f"[TRACK] 计算异常: {e}")
        return _TRACK_EXIT_FAILED


def _cmd_track_backfill(
    args, cfg, cal, backfill: int, *, subset_rules: Optional[List[str]] = None
) -> int:
    """回填主体（外层已持 heavy-job 锁；锁同线程可重入）。

    逐周：无发布快照 → 先 review-weekly 补快照（run_kind=backfill）再结算
    跟踪；已有快照+track → 跳过（断点续跑）；单周失败不中断后续周。

    ``subset_rules`` 非空 = 子集补算（只跑这些规则，产出 rules_scope=subset
    的快照），逐周加一道历史周护栏（见 _latest_signal_week）。
    """
    # 锚定周：显式 --week 或最近发布快照周
    try:
        anchor = _resolve_track_week(cfg, args)
    except ValueError:
        anchor = None
    if anchor is None:
        # 一个快照都没有：从数据面最新交易日回退（backfill 冷启动）。
        # 审查 A-1：surface_max_date 是票级 min 水位（周二/停牌日都
        # 可能），必须先归一到"其 ISO 周内最后交易日"再入周枚举——
        # 否则该周产出的 week_id 与将来正式周五链发布的周身份不一致
        # （同周双产物、统计双计）。
        raw_anchor = tracksvc.surface_max_date(cfg)
        anchor = 0
        if raw_anchor and raw_anchor > 0:
            cal_dates = list(cal.dates) if cal else []
            week_of = tracksvc.past_signal_weeks(cal, int(raw_anchor), 1)
            anchor = int(week_of[0]) if week_of else 0
    if anchor <= 0:
        print("无法确定回填锚点（无快照且数据面不可用）")
        return _TRACK_EXIT_CONFIG
    weeks = tracksvc.past_signal_weeks(cal, anchor, backfill)
    if not weeks:
        print("日历不可用，无法枚举回填周")
        return _TRACK_EXIT_CONFIG
    # 子集护栏依据只算一次（surface_max_date 要读 manifest，逐周重复无谓）
    latest_signal_week = _latest_signal_week(cfg, cal) if subset_rules else 0
    if subset_rules:
        print(
            f"[TRACK] 指定规则补算：{len(subset_rules)} 条规则 "
            f"({', '.join(subset_rules)})；历史周护栏上限={latest_signal_week or '未知'}"
            "（该周及之后归周五链）"
        )
    results = []
    overall = _TRACK_EXIT_OK
    for wk in weeks:
        if subset_rules and latest_signal_week and int(wk) >= latest_signal_week:
            # 子集快照是部分名单：占住链条会发布的周 = 该周永久只剩这几条
            # 规则的数据（全量快照被"已有指针不替换"挡住），必须拒绝
            print(
                f"[TRACK] {wk}: 跳过（子集补算只支持历史周；数据面最新信号周 "
                f"{latest_signal_week} 及之后归周五链，请用整周补算）"
            )
            results.append({
                "week": wk, "completion": "skipped_subset_scope",
                "reason": "subset_scope_not_historical_week",
                "latest_signal_week": latest_signal_week,
            })
            continue
        # --force 跳过断点续跑短路（审查 🟡-7：此前 force 是死参，
        # 用户以为强制重算实际什么都不做）；同 revision 产物仍不覆盖
        force_flag = bool(getattr(args, "force", False))
        if force_flag:
            need, reason = True, "forced"
        else:
            # 此断点续跑判定本身处理 data_version 捕获，轻量可行
            need, reason = tracksvc.should_recompute(
                cfg, wk, calendar=cal,
                algo_version=tracksvc.TRACKING_ALGO_VERSION,
            )
        if not need and reason == "no_published_snapshot":
            # 无快照 → 先补快照（run_kind=backfill 仅补无指针周）
            if subset_rules:
                print(
                    f"[TRACK] {wk}: 无发布快照，先跑 review-weekly 指定规则"
                    f"（{len(subset_rules)} 条，persist=False）…"
                )
            else:
                print(f"[TRACK] {wk}: 无发布快照，先跑 review-weekly 全规则…")
            rc_snap = cmd_review_weekly(
                argparse.Namespace(
                    asof=str(wk),
                    rules=(",".join(subset_rules) if subset_rules else "all"),
                    publish_scope=("subset" if subset_rules else "auto"),
                    codes=None, force=False,
                    run_kind="backfill",
                    tdx_root=getattr(args, "tdx_root", None),
                    storage=getattr(args, "storage", None),
                    indicator_dir=getattr(args, "indicator_dir", None),
                )
            )
            if rc_snap != 0:
                results.append(
                    {"week": wk, "completion": "review_failed",
                     "review_exit_code": rc_snap}
                )
                overall = _TRACK_EXIT_RETRYABLE
                continue
            # 复核退出 0 不等于"已发布"：发布门槛（partial/error 规则、
            # no_data 超阈）与契约护栏（已有指针/子集历史周）都可能拒绝。
            # 不校验的话该周会被下一行的 should_recompute 报成
            # no_published_snapshot → "skipped"，用户看不到真实原因。
            if ss.load_published_snapshot_for_week(cfg, wk) is None:
                print(
                    f"[TRACK] {wk}: 复核完成但未发布快照"
                    "（门槛/护栏拒绝，见上面 [SNAPSHOT] 行的 reason）"
                )
                results.append({
                    "week": wk, "completion": "review_not_published",
                    "reason": "no_published_snapshot_after_review",
                })
                overall = _TRACK_EXIT_RETRYABLE
                continue
            need, reason = tracksvc.should_recompute(
                cfg, wk, calendar=cal,
                algo_version=tracksvc.TRACKING_ALGO_VERSION,
            )
        if not need:
            results.append({"week": wk, "completion": "skipped",
                            "reason": reason})
            print(f"[TRACK] {wk}: 跳过（{reason}）")
            continue
        rc, out = _run_one_track_week(cfg, wk, args, hold_lock=False)
        results.append({"week": wk, **out})
        if rc != _TRACK_EXIT_OK:
            # 3（pending/blocked/版本变化）不中断后续周：每周期独立。
            # 聚合优先级（审查 A-12）：1（计算事故，需人看栈）必须
            # 压过 3（可自动重试）——否则失败被 retryable 掩盖，
            # 调用方只会徒劳重试。1 出现即 overall=1。
            if rc == _TRACK_EXIT_FAILED:
                overall = _TRACK_EXIT_FAILED
            elif overall == _TRACK_EXIT_OK:
                overall = rc
        print(f"[TRACK] {wk}: {out.get('completion')}（exit={rc}）")
    print(json.dumps(
        {"completion": "backfill_done", "weeks": results,
         "overall_exit_code": overall},
        ensure_ascii=False, indent=2))
    return overall

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

    sp = sub.add_parser(
        "review-weekly",
        help="周五链全市场指标复核（735/5日外），产出 review_{asof}.json 供导出读取",
    )
    sp.add_argument("--asof", default=None, help="YYYYMMDD，默认取数据面最新可得交易日")
    sp.add_argument("--rules", default=None, help="逗号分隔规则 ID；all=全部可执行规则（产出不可变快照+发布指针）")
    sp.add_argument(
        "--publish-scope", default="auto", dest="publish_scope",
        choices=["auto", "all", "subset"],
        help="快照规则范围：auto=按 --rules 推断（现状）；all=--rules all 全量快照；"
             "subset=只对 --rules 指定规则产出子集快照（历史周单规则补算，"
             "不写 review_{asof}.json）",
    )
    sp.add_argument("--codes", default=None, help="逗号分隔代码（默认 universe.json 全市场）")
    sp.add_argument("--force", action="store_true", help="忽略已有结果强制重算")
    sp.add_argument(
        "--run-kind", default=None, dest="run_kind",
        choices=["weekly_chain", "backfill", "recompute"],
        help="快照来源标识（快照模式下生效；缺省全量=weekly_chain、子集=backfill；"
             "recompute 永不自动发布）",
    )
    sp.set_defaults(func=cmd_review_weekly)

    sp = sub.add_parser(
        "track-weekly",
        help="入选股票次周跟踪（双口径收益/胜率/覆盖率/沪深300超额），产物不可变",
    )
    sp.add_argument("--week", default=None,
                    help="YYYYMMDD 信号日，缺省=最近发布快照周")
    sp.add_argument("--previous-week", action="store_true", dest="previous_week",
                    help="缺省锚定改为上一信号周（周五链语义：结算上周名单）")
    sp.add_argument("--force", action="store_true",
                    help="忽略断点续跑短路强制重算（同 revision 产物仍不覆盖；"
                         "backfill 场景即跳过 should_recompute 直接算）")
    sp.add_argument("--backfill", type=int, default=0, dest="backfill",
                    help="对过去 N 个周五逐周补：先 review-weekly 全规则补快照再结算跟踪")
    sp.add_argument(
        "--rules", default=None,
        help="仅与 --backfill 同用：逗号分隔规则 ID，只补这些规则（子集快照）。"
             "缺省=全部可执行规则。单规则约为全量的 1/6~1/10 耗时，"
             "用于验证某条规则在过去某周选出的股票与表现。",
    )
    sp.add_argument(
        "--run-kind", default="weekly_chain",
        choices=["weekly_chain", "backfill", "recompute"],
        help="跟踪运行来源标识（写入产物，与快照 run_kind 对齐）",
    )
    sp.set_defaults(func=cmd_track_weekly)

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
