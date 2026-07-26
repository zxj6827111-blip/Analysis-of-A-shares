# -*- coding: utf-8 -*-
"""Post-engine artifact writing and summary assembly for backtests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


def build_repro_meta(
    *,
    cfg: Any,
    req: Any,
    events: Sequence[Any],
    trade_specs: Sequence[Any],
    codes: Sequence[str],
    formula_audits: Dict[str, Any],
    factor_series: Any,
    adj_msg: Any,
    research_unadj: bool,
    formal_ok: bool,
    unconfirmed_run: bool,
    price_mode: Any,
    signal_price_mode: Any,
    execution_price_mode: Any,
    valuation_price_mode: Any,
    corporate_action_policy: Any,
    engine_result_version: Any,
    entry_lag: int,
    signal_weekdays: Any,
    buy_on: str,
    sell_on: str,
    buy_weekday: Any,
    exit_weekday: Any,
    period: str,
    hold: int,
    combine: Any,
    start: Optional[int],
    end: Optional[int],
    bagua_enabled: bool,
    bagua_filter_mode: Any,
    gua_filter_meta: Any,
    gf: Any,
    bagua_n_before: Any,
    bagua_n_after: Any,
    signal_cache_hit: bool,
    filter_cache_hit: bool,
    execution_cache_hit: bool,
    use_signal_cache: bool,
) -> tuple:
    """Build run_meta repro dict and fingerprint fields. Returns (repro, _fp_fields)."""
    from ..bagua.filter_rules import best3_display_pairs, mode_label as bagua_mode_label
    from ..data.adjustments import factor_manifest_sha
    from ..data.catalog import file_sha_or_empty, selected_universe_sha
    from ..data.universe import AShareUniverse
    from .backtest_universe import _astock_code_sha
    from .backtest_request import research_fingerprint_fields_from_request
    from ..price_planes import (
        PRICE_MODE_NOTE_V3,
        THREE_PLANE_SUMMARY_ZH,
        three_plane_repro_fields,
    )

    bagua_sha = ""
    try:
        bagua_sha = __import__("hashlib").sha256(Path(cfg.bagua_json).read_bytes()).hexdigest()
    except Exception:
        pass
    global_manifest_sha = file_sha_or_empty(cfg.manifest_path)
    global_universe_sha = file_sha_or_empty(cfg.universe_path)
    global_universe_count = 0
    if cfg.universe_path.exists():
        try:
            global_universe_count = len(AShareUniverse.load(cfg.universe_path))
        except Exception:
            global_universe_count = 0
    sel_sha = selected_universe_sha(codes)
    primary_audit: Dict[str, Any] = {}
    for s in trade_specs:
        primary_audit = formula_audits.get(s.id, {})
        break
    repro = {
        "price_mode": price_mode,
        "signal_price_mode": signal_price_mode,
        "execution_price_mode": execution_price_mode,
        "valuation_price_mode": valuation_price_mode,
        "corporate_action_policy": corporate_action_policy,
        "engine_result_version": engine_result_version,
        "price_mode_note": PRICE_MODE_NOTE_V3,
        "research_price_mode": "point_in_time_adjusted",
        "display_price_mode": "standard_qfq",
        "result_semantics_tag": "asof_qfq_signal_raw_execution_v3",
        "risk_conflict_policy": "stop_first",
        "risk_trigger_policy": "daily_high_low",
        "risk_execution_policy": "next_trading_day_open",
        "entry_lag": entry_lag,
        "signal_weekdays": signal_weekdays,
        "buy_on": buy_on,
        "sell_on": sell_on,
        "buy_weekday": buy_weekday,
        "exit_weekday": exit_weekday,
        "schedule_mode": (
            "weekday" if (buy_weekday is not None or exit_weekday is not None) else "tn"
        ),
        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
        "stop_loss_pct": req.stop_loss,
        "take_profit_pct": req.take_profit,
        "formula_provenance": primary_audit.get("formula_provenance"),
        "source_pair_status": primary_audit.get("source_pair_status"),
        "formal_backtest_allowed": primary_audit.get("formal_backtest_allowed"),
        "confirmed_by": primary_audit.get("confirmed_by"),
        "confirmed_at": primary_audit.get("confirmed_at"),
        "confirmation_method": primary_audit.get("confirmation_method"),
        "confirmation_note": primary_audit.get("confirmation_note"),
        "package_file": primary_audit.get("package_file"),
        "package_sha256": primary_audit.get("package_sha256"),
        "source_file": primary_audit.get("source_file"),
        "source_sha256": primary_audit.get("source_sha256"),
        "formula_audits": formula_audits,
        "indicator_ids": [s.id for s in trade_specs],
        "indicator_source_sha": {s.id: s.source_sha256 for s in trade_specs},
        "indicator_package_sha": {s.id: s.package_sha256 for s in trade_specs},
        "package_sha_note": (
            "package_sha is null for pure .txt indicators (no .tn6 package); "
            "only tn6_* entries carry package_sha256."
        ),
        "period": period,
        "hold": hold,
        "combine": combine,
        "selected_codes": codes,
        "selected_codes_count": len(codes),
        "selected_universe_sha": sel_sha,
        "global_manifest_sha": global_manifest_sha,
        "global_universe_sha": global_universe_sha,
        "global_universe_count": global_universe_count,
        "start": start,
        "end": end,
        "config": cfg.to_dict(),
        "factor_manifest_sha": factor_manifest_sha(factor_series),
        "adjustment_status": adj_msg,
        "research_unadjusted": research_unadj or not formal_ok,
        "research_unconfirmed_formula": unconfirmed_run,
        "bagua_sha": bagua_sha,
        "with_bagua": bagua_enabled,
        "bagua_filter_mode": bagua_filter_mode,
        "bagua_filter_label": (
            (gua_filter_meta or {}).get("natural_language")
            if gua_filter_meta
            else (bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None)
        ),
        "bagua_allowlist": (
            best3_display_pairs()
            if bagua_filter_mode and not gua_filter_meta
            else None
        ),
        "gua_filter": gua_filter_meta or (gf.to_dict() if gf else None),
        "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
        "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
        "code_version": "astock-0.5.0",
        "astock_code_sha": _astock_code_sha(),
        "n_signals": len(events),
        "signal_cache_hit": signal_cache_hit,
        "filter_cache_hit": filter_cache_hit,
        "execution_cache_hit": execution_cache_hit,
        "use_signal_cache": use_signal_cache,
        "request": req.to_dict(),
    }
    try:
        _ind_src = None
        if trade_specs:
            _ind_src = "|".join(
                sorted(
                    "%s:%s" % (s.id, getattr(s, "source_sha256", None) or "")
                    for s in trade_specs
                )
            )
        _fp_fields = research_fingerprint_fields_from_request(
            req,
            universe_hash=sel_sha,
            indicator_source_hash=_ind_src,
            bagua_json_hash=bagua_sha if bagua_enabled else None,
        )
        repro.update(_fp_fields)
    except Exception:
        _fp_fields = {}
    
    repro.update(
        three_plane_repro_fields(
            signal_price_mode=str(signal_price_mode or ""),
            execution_price_mode=str(execution_price_mode or "raw"),
            valuation_price_mode=str(valuation_price_mode or "raw"),
            corporate_action_policy=str(corporate_action_policy or "fail_closed"),
        )
    )
    repro["three_plane_summary_zh"] = THREE_PLANE_SUMMARY_ZH

    return repro, _fp_fields


def finalize_run_outputs(
    *,
    cfg: Any,
    req: Any,
    result: Any,
    events: Sequence[Any],
    trade_specs: Sequence[Any],
    out_dir: Path,
    repro: Dict[str, Any],
    art_flags: Dict[str, Any],
    artifact_level: str,
    progress: Callable[[dict], None],
    n_codes: int,
    run_id: str,
    period: str,
    hold: int,
    entry_lag: int,
    start: Optional[int],
    end: Optional[int],
    buy_on: str,
    sell_on: str,
    buy_weekday: Any,
    exit_weekday: Any,
    signal_weekdays: Any,
    codes: Sequence[str],
    engine: str,
    holiday_policy: str,
    bagua_enabled: bool,
    bagua_filter_mode: Any,
    gua_filter_meta: Any,
    gf: Any,
    bagua_n_before: Any,
    bagua_n_after: Any,
    n_events_raw_signals: int,
    n_events_after_weekday: int,
    errors: List[dict],
    signal_cache_hit: bool,
    filter_cache_hit: bool,
    execution_cache_hit: bool,
    use_signal_cache: bool,
    _fp_fields: Any,
) -> Dict[str, Any]:
    """Write artifacts, index the run, and return the service summary dict."""
    from ..bagua.filter_rules import mode_label as bagua_mode_label
    from ..reports import write_backtest_csv, write_signals_csv
    from ..strategy import format_signal_weekdays, format_single_weekday, session_label_cn

    progress(
        {
            "phase": "writing",
            "pct": 96.0,
            "current": n_codes,
            "total": n_codes,
            "message": f"写入结果（信号 {len(events)} / 成交 {len(getattr(result, 'fills', []) or [])}）…",
            "code": None,
            "run_id": run_id,
        }
    )

    if art_flags.get("write_signals", True):
        write_signals_csv(out_dir / "signals.csv", events)
        try:
            from ..research.parquet_io import write_events_parquet

            write_events_parquet(out_dir / "signals.parquet", events)
        except Exception:
            pass

    progress(
        {
            "phase": "writing_excel",
            "pct": 97.0,
            "current": n_codes,
            "total": n_codes,
            "message": (
                "写入摘要…"
                if not art_flags.get("write_excel", True)
                else "写入 CSV / Excel 汇总（大明细可能仍需片刻）…"
            ),
            "code": None,
            "run_id": run_id,
            "n_fills": len(getattr(result, "fills", []) or []),
        }
    )

    rule_names = [s.name for s in trade_specs]
    period_label = {
        "DAY": "日线",
        "WEEK": "周线",
        "MONTH": "月线",
        "DWM": "日周月共振",
        "MIN60": "60分钟",
    }.get(period, period)
    title = "、".join(rule_names) if rule_names else "回测"
    if gua_filter_meta:
        hs = (gua_filter_meta.get("history_summary") or {}).get("short") or "卦象过滤"
        try:
            _sids = sorted(str(x) for x in (gua_filter_meta.get("selected_state_ids") or []))
            if _sids == ["11-1", "24-1", "46-1"]:
                hs = "卦象·最佳3爻"
            elif gua_filter_meta.get("selection_mode") == "action_signal":
                acts = list(gua_filter_meta.get("selected_action_signals") or [])
                if set(acts) == {"新开仓", "加仓"} or set(acts) == {"加仓", "新开仓"}:
                    hs = "卦象·偏多信号"
        except Exception:
            pass
        title = f"{title} + {hs}"
    elif bagua_enabled and bagua_filter_mode:
        title = f"{title} + {bagua_mode_label(bagua_filter_mode)}"
    elif bagua_enabled:
        title = f"{title} + 八卦"
    am = (getattr(req, "account_mode", None) or "portfolio").strip().lower()
    if am in ("tdx", "per_stock", "independent", "通达信", "单票"):
        am = "per_symbol"
    if am == "per_symbol":
        title = f"{title} · 通达信对照(单票独立资金)"
    else:
        title = f"{title} · 组合账户"
    schedule_mode = (
        "weekday" if (buy_weekday is not None or exit_weekday is not None) else "tn"
    )
    title = f"{title} · {period_label}"
    if schedule_mode == "tn":
        title = f"{title} · 持有{hold}"
    if signal_weekdays:
        title = f"{title} · 仅{format_signal_weekdays(signal_weekdays)}信号"
    title = f"{title} · {session_label_cn(buy_on)}买/{session_label_cn(sell_on)}卖"
    if buy_weekday is not None:
        title = f"{title} · {format_single_weekday(buy_weekday)}买"
    if exit_weekday is not None:
        title = f"{title} · {format_single_weekday(exit_weekday)}平"
    if start or end:
        title += f" · {start or ''}~{end or ''}"

    repro["title"] = title
    repro["schedule_mode"] = schedule_mode
    repro["indicator_names"] = rule_names
    try:
        from dataclasses import asdict as _asdict_costs

        repro["costs"] = _asdict_costs(cfg.costs)
        if isinstance(repro.get("config"), dict):
            repro["config"].setdefault("costs", repro["costs"])
    except Exception:
        pass
    if gua_filter_meta is not None:
        repro["gua_filter"] = gua_filter_meta
    elif "gua_filter" not in repro:
        repro["gua_filter"] = gf.to_dict() if gf else None

    repro["engine"] = engine if engine in ("fast", "quick", "research_fast") else "full"
    if repro["engine"] != "full":
        repro["engine"] = "fast"
    repro["artifact_level"] = artifact_level
    repro["holiday_policy"] = holiday_policy

    out_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    # Critical artifacts: fail visibly (do not swallow). Optional CSV/parquet stay best-effort.
    try:
        (out_dir / "run_meta.json").write_text(
            _json.dumps(repro, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (out_dir / "metrics.json").write_text(
            _json.dumps(result.metrics or {}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        note = f"critical_artifact_write_failed: {e}"
        try:
            notes = list(getattr(result, "notes", None) or [])
            notes.append(note)
            result.notes = notes
        except Exception:
            pass
        raise
    paths: dict = {
        "run_meta": out_dir / "run_meta.json",
        "metrics": out_dir / "metrics.json",
    }
    if art_flags.get("write_fills", True) or art_flags.get("write_excel", True) or art_flags.get(
        "write_equity", True
    ):
        if artifact_level != "summary":
            paths.update(
                write_backtest_csv(
                    out_dir,
                    result,
                    meta=repro,
                    events=events if art_flags.get("write_signals") else None,
                )
            )

    try:
        from .runs import append_run_index

        append_run_index(
            cfg,
            {
                "run_id": run_id,
                "title": title,
                "status": result.status,
                "created_at": int(time.time()),
                "indicator_ids": [s.id for s in trade_specs],
                "indicator_names": rule_names,
                "hold": hold,
                "entry_lag": entry_lag,
                "buy_weekday": buy_weekday,
                "exit_weekday": exit_weekday,
                "buy_on": buy_on,
                "sell_on": sell_on,
                "signal_weekdays": signal_weekdays,
                "schedule_mode": schedule_mode,
                "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
                "gua_filter": gua_filter_meta,
                "with_bagua": bagua_enabled,
                "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
                "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
                "period": period,
                "period_label": period_label,
                "start": start,
                "end": end,
                "selected_codes_count": len(codes),
                "metrics": result.metrics,
                "signal_data_source": getattr(req, "signal_data_source", None),
                "signal_adjustment": getattr(req, "signal_adjustment", None),
                "dataset_id": getattr(req, "dataset_id", None),
                "weekly_bar_mode": getattr(req, "weekly_bar_mode", None) or "local_aggregate",
                "execution_data_source": getattr(req, "execution_data_source", None) or "tdx_local",
                "execution_dataset_id": getattr(req, "execution_dataset_id", None),
                **(_fp_fields if isinstance(_fp_fields, dict) else {}),
            },
        )
    except Exception:
        pass

    progress(
        {
            "phase": "done",
            "pct": 100.0,
            "current": n_codes,
            "total": n_codes,
            "message": "完成",
            "code": None,
            "run_id": run_id,
        }
    )

    _costs = None
    if isinstance(getattr(result, "config", None), dict):
        _costs = result.config.get("costs")
    if _costs is None:
        try:
            from dataclasses import asdict as _asdict

            _costs = _asdict(cfg.costs)
        except Exception:
            _costs = None
    if isinstance(repro.get("config"), dict) and isinstance(repro["config"].get("costs"), dict):
        _costs = repro["config"]["costs"]
    elif _costs is not None:
        repro.setdefault("costs", _costs)
        if isinstance(repro.get("config"), dict):
            repro["config"].setdefault("costs", _costs)

    return {
        "run_id": run_id,
        "title": title,
        "status": result.status,
        "metrics": result.metrics,
        "costs": _costs,
        "n_events": len(events),
        "n_events_raw_signals": int(n_events_raw_signals),
        "n_events_after_weekday": int(n_events_after_weekday),
        "with_bagua": bagua_enabled,
        "bagua_filter_mode": bagua_filter_mode,
        "bagua_filter_label": (
            (gua_filter_meta or {}).get("natural_language")
            if gua_filter_meta
            else (bagua_mode_label(bagua_filter_mode) if bagua_filter_mode else None)
        ),
        "gua_filter": gua_filter_meta or (gf.to_dict() if gf else None),
        "n_signals_before_bagua": bagua_n_before if bagua_enabled else None,
        "n_signals_after_bagua": bagua_n_after if bagua_enabled else None,
        "n_fills": len(getattr(result, "fills", None) or []),
        "n_buys": result.metrics.get("n_buys"),
        "n_sells": result.metrics.get("n_sells"),
        "n_round_trips": result.metrics.get("n_round_trips"),
        "paths": {k: str(v) for k, v in paths.items()},
        "errors_sample": errors[:20],
        "notes": result.notes,
        "entry_lag": entry_lag,
        "signal_weekdays": signal_weekdays,
        "buy_on": buy_on,
        "sell_on": sell_on,
        "buy_weekday": buy_weekday,
        "exit_weekday": exit_weekday,
        "schedule_mode": schedule_mode,
        "hold": hold,
        "account_mode": (getattr(req, "account_mode", None) or "portfolio"),
        "period": period,
        "engine": repro.get("engine") or "full",
        "artifact_level": artifact_level,
        "holiday_policy": holiday_policy,
        "signal_cache_hit": signal_cache_hit,
        "filter_cache_hit": filter_cache_hit,
        "execution_cache_hit": execution_cache_hit,
        "use_signal_cache": use_signal_cache,
        "repro": {
            "factor_manifest_sha": repro["factor_manifest_sha"],
            "entry_lag": entry_lag,
            "hold": hold,
            "period": period,
            "signal_weekdays": signal_weekdays,
            "buy_on": buy_on,
            "sell_on": sell_on,
            "buy_weekday": buy_weekday,
            "exit_weekday": exit_weekday,
            "schedule_mode": schedule_mode,
            "indicator_ids": repro["indicator_ids"],
            "astock_code_sha": repro.get("astock_code_sha"),
            "research_fingerprint": repro.get("research_fingerprint"),
            "signal_fp": repro.get("signal_fp"),
            "filter_fp": repro.get("filter_fp"),
            "execution_fp": repro.get("execution_fp"),
        },
        "research_fingerprint": repro.get("research_fingerprint"),
        "signal_fp": repro.get("signal_fp"),
        "filter_fp": repro.get("filter_fp"),
        "execution_fp": repro.get("execution_fp"),
    }
