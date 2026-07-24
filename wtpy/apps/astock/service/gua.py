# -*- coding: utf-8 -*-
"""Gua (hexagram) catalogue service: list/search/preview without full backtest."""
from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..bagua.filter_rules import (
    DEFAULT_RULE_VERSION,
    GuaFilter,
    event_matches_gua_filter,
    gua_filter_natural_language,
)
from ..bagua.calculator import BaguaKnowledge
from ..config import AStockConfig, get_default_config


def _kb_path(cfg: Optional[AStockConfig] = None) -> Path:
    cfg = cfg or get_default_config()
    p = cfg.bagua_json
    if p is None:
        p = Path(__file__).resolve().parents[1] / "bagua" / "bagua_384.json"
    return Path(p)


@lru_cache(maxsize=4)
def _load_raw(path_str: str, mtime: float) -> dict:
    import json

    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def load_kb(cfg: Optional[AStockConfig] = None) -> dict:
    path = _kb_path(cfg)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return _load_raw(str(path.resolve()), mtime)


def invalidate_kb_cache() -> None:
    _load_raw.cache_clear()


def rule_version(cfg: Optional[AStockConfig] = None) -> str:
    kb = load_kb(cfg)
    return str(kb.get("rule_version") or DEFAULT_RULE_VERSION)


def hexagram_name_map(cfg: Optional[AStockConfig] = None) -> Dict[int, str]:
    kb = load_kb(cfg)
    out: Dict[int, str] = {}
    for e in kb.get("entries") or []:
        go = int(e["gua_order"])
        if go not in out:
            out[go] = e.get("gua_name") or e.get("main_hexagram_name") or f"第{go:02d}卦"
    return out


def state_label_map(cfg: Optional[AStockConfig] = None) -> Dict[str, str]:
    kb = load_kb(cfg)
    out: Dict[str, str] = {}
    for e in kb.get("entries") or []:
        sid = e.get("state_id")
        if not sid:
            continue
        sym = e.get("gua_symbol") or e.get("hexagram_symbol") or ""
        name = e.get("gua_name") or ""
        yao = e.get("yao_name") or e.get("line_name") or ""
        out[str(sid)] = f"{sym} {name}·{yao}".strip()
    return out


def _entry_public(e: dict) -> dict:
    bg = e.get("biangua") or e.get("changed_hexagram_name") or ""
    return {
        "state_id": e.get("state_id") or f"{int(e['gua_order']):02d}-{int(e['yao_order'])}",
        "main_hexagram_id": int(e.get("main_hexagram_id") or e["gua_order"]),
        "main_hexagram_name": e.get("main_hexagram_name") or e.get("gua_name") or "",
        "hexagram_symbol": e.get("hexagram_symbol") or e.get("gua_symbol") or "",
        "gua_order": int(e["gua_order"]),
        "gua_name": e.get("gua_name") or "",
        "gua_symbol": e.get("gua_symbol") or "",
        "full_name": e.get("full_name") or "",
        "core_gang": e.get("core_gang") or "",
        "gua_ci": e.get("gua_ci") or "",
        "line_index": int(e.get("line_index") or e.get("yao_order") or 0),
        "yao_order": int(e.get("yao_order") or 0),
        "line_name": e.get("line_name") or e.get("yao_name") or "",
        "yao_name": e.get("yao_name") or "",
        "line_text": e.get("line_text") or e.get("yao_ci") or "",
        "yao_ci": e.get("yao_ci") or "",
        "changed_hexagram_id": e.get("changed_hexagram_id"),
        "changed_hexagram_name": bg or None,
        "biangua": bg or None,
        "market_summary": e.get("market_summary") or e.get("market_judgement") or "",
        "market_judgement": e.get("market_judgement") or e.get("market_summary") or "",
        "action_signal": e.get("action_signal") or "",
        "note": e.get("note") or "",
        "upper": e.get("upper"),
        "lower": e.get("lower"),
    }


def list_hexagrams(cfg: Optional[AStockConfig] = None) -> List[dict]:
    kb = load_kb(cfg)
    by: Dict[int, dict] = {}
    for e in kb.get("entries") or []:
        go = int(e["gua_order"])
        if go not in by:
            by[go] = {
                "main_hexagram_id": go,
                "gua_order": go,
                "hexagram_symbol": e.get("gua_symbol") or "",
                "main_hexagram_name": e.get("gua_name") or "",
                "full_name": e.get("full_name") or "",
                "core_gang": e.get("core_gang") or "",
                "gua_ci": e.get("gua_ci") or "",
                "lines": [],
                "selected_hint": "0/6",
            }
        by[go]["lines"].append(
            {
                "state_id": e.get("state_id"),
                "line_index": int(e.get("yao_order") or 0),
                "line_name": e.get("yao_name") or "",
                "line_text": e.get("yao_ci") or "",
                "action_signal": e.get("action_signal") or "",
                "biangua": e.get("biangua") or None,
                "market_summary": e.get("market_judgement") or "",
            }
        )
    out = [by[k] for k in sorted(by.keys())]
    for h in out:
        h["lines"] = sorted(h["lines"], key=lambda x: x["line_index"])
    return out


def _match_search(e: dict, q: str) -> bool:
    if not q:
        return True
    q = q.strip().lower()
    if not q:
        return True
    # ordinal patterns: 1, 01, 第一卦
    go = int(e.get("gua_order") or 0)
    blobs = [
        str(e.get("state_id") or ""),
        str(e.get("gua_name") or ""),
        str(e.get("full_name") or ""),
        str(e.get("gua_symbol") or ""),
        str(e.get("yao_name") or ""),
        str(e.get("yao_ci") or ""),
        str(e.get("biangua") or ""),
        str(e.get("market_judgement") or ""),
        str(e.get("action_signal") or ""),
        str(go),
        f"{go:02d}",
        f"第{go}卦",
        f"第{go:02d}卦",
    ]
    hay = " ".join(blobs).lower()
    if q in hay:
        return True
    # strip spaces
    if q.replace(" ", "") in hay.replace(" ", ""):
        return True
    return False


def list_states(
    cfg: Optional[AStockConfig] = None,
    *,
    search: Optional[str] = None,
    main_hexagram_id: Optional[int] = None,
    action_signal: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    kb = load_kb(cfg)
    items = [_entry_public(e) for e in (kb.get("entries") or [])]
    if main_hexagram_id is not None:
        mid = int(main_hexagram_id)
        items = [x for x in items if int(x["main_hexagram_id"]) == mid]
    if action_signal:
        act = str(action_signal).strip()
        items = [x for x in items if (x.get("action_signal") or "") == act]
    if search:
        items = [x for x in items if _match_search(x, search)]
    total = len(items)
    page = max(1, int(page or 1))
    page_size = max(1, min(384, int(page_size or 50)))
    start = (page - 1) * page_size
    chunk = items[start : start + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": chunk,
        "rule_version": kb.get("rule_version") or DEFAULT_RULE_VERSION,
        "empty_biangua_count": kb.get("empty_biangua_count"),
        "action_signal_counts": kb.get("action_signal_counts") or {},
    }


def reimport_excel(
    xlsx_path: Path | str,
    cfg: Optional[AStockConfig] = None,
    *,
    rule_version: Optional[str] = None,
    archive_previous: bool = True,
) -> dict:
    """Rebuild knowledge from Excel; archive previous active JSON by rule_version."""
    import json
    import shutil
    from datetime import datetime

    from ..bagua.rebuild_from_excel import (
        DEFAULT_RULE_VERSION as _DEF_VER,
        rebuild_knowledge_from_excel,
        validate_knowledge,
    )

    cfg = cfg or get_default_config()
    out = _kb_path(cfg)
    archived_as = None
    previous_version = None
    if archive_previous and out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            previous_version = str(prev.get("rule_version") or "unknown")
        except Exception:
            previous_version = "unknown"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_ver = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_"
            for ch in (previous_version or "unknown")
        )
        archive_path = out.with_name(f"{out.stem}.{safe_ver}.{stamp}{out.suffix}")
        shutil.copy2(out, archive_path)
        archived_as = str(archive_path)

    ver = rule_version or _DEF_VER
    if rule_version is None and previous_version and previous_version == ver:
        ver = f"{ver}_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    kb = rebuild_knowledge_from_excel(xlsx_path, out, rule_version=ver)
    report = validate_knowledge(kb)
    report["archived_previous"] = archived_as
    report["previous_rule_version"] = previous_version
    report["active_path"] = str(out)
    report["rule_version"] = kb.get("rule_version")
    invalidate_kb_cache()
    return report


def collect_indicator_signals_with_bagua(
    cfg: AStockConfig,
    *,
    rule_ids: Sequence[str],
    codes: Optional[Sequence[str]] = None,
    period: str = "DAY",
    start: Optional[int] = None,
    end: Optional[int] = None,
    max_codes: int = 30,
    research_unadjusted: bool = True,
    progress_cb=None,
) -> dict:
    """Generate technical signals on a limited pool and attach bagua labels."""
    from ..bagua.calculator import BaguaCalculator
    from ..data.adjustments import build_factor_series
    from ..data.data_store import DataStore
    from ..data.tdx_reader import TdxDayReader
    from ..study import (
        SignalEvent,
        attach_bagua,
        bars_dict_from_day,
        bars_dict_from_period,
        build_period_bars,
        compute_indicator_signal,
        day_bars_for_signals,
    )
    from .backtest import DEMO_CODES, select_universe
    from .rules import RuleService

    def _progress(payload: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except Exception:
            pass

    period = (period or "DAY").upper()
    if period not in ("DAY", "WEEK", "MONTH"):
        period = "DAY"

    if codes is None or (isinstance(codes, (list, tuple)) and len(codes) == 0):
        code_list = list(DEMO_CODES)
    else:
        code_list = select_universe(cfg, codes)
    if max_codes and len(code_list) > int(max_codes):
        code_list = code_list[: int(max_codes)]

    reg = RuleService(cfg).load_full_registry()
    try:
        specs = [reg.get(i) for i in rule_ids]
    except KeyError as e:
        raise ValueError(f"unknown rule: {e}") from e
    trade_specs = [
        s for s in specs if s.id != "bagua_ohlc" and s.output_type == "signal"
    ]
    if not trade_specs:
        raise ValueError("No tradeable signal indicators selected.")

    store = DataStore(cfg.storage_root)
    events: List[SignalEvent] = []
    period_raw_map: Dict[str, Any] = {}
    errors: List[dict] = []
    n_codes = len(code_list)

    for idx, code in enumerate(code_list):
        _progress(
            {"phase": "signals", "current": idx + 1, "total": n_codes, "code": code}
        )
        try:
            day_raw = store.load_symbol(code)
        except FileNotFoundError:
            try:
                reader = TdxDayReader(cfg.tdx_root)
                raw = ("sh" if code.startswith("SSE") else "sz") + code.split(".")[-1]
                day_raw, _ = reader.read(raw)
            except Exception as e:
                errors.append({"code": code, "error": str(e)})
                continue
        if not day_raw:
            errors.append({"code": code, "error": "empty bars"})
            continue
        dates = [b.date for b in day_raw]
        if research_unadjusted:
            day_for_ind = day_raw
        else:
            try:
                series = build_factor_series(
                    code, dates, adj_root=cfg.adj_root, prefer_baostock=True
                )
                import numpy as np

                fac = np.array(series.factors, dtype=float)
                day_for_ind = day_bars_for_signals(
                    day_raw, fac, research_unadjusted=False
                )
            except Exception:
                day_for_ind = day_raw
        asof = day_raw[-1].date if day_raw else None
        try:
            if period == "DAY":
                p_bars_raw = day_raw
                bars = bars_dict_from_day(day_for_ind)
            else:
                p_bars_raw = build_period_bars(day_raw, period, asof=asof)
                p_bars_ind = build_period_bars(day_for_ind, period, asof=asof)
                bars = bars_dict_from_period(p_bars_ind)
            period_raw_map[code] = p_bars_raw
        except Exception as e:
            errors.append({"code": code, "error": f"period bars: {e}"})
            continue

        for spec in trade_specs:
            sig, err = compute_indicator_signal(spec, bars)
            if err:
                errors.append({"code": code, "indicator": spec.id, "error": err})
                continue
            date_arr = bars["date"]
            for i, d in enumerate(date_arr):
                try:
                    on = int(sig[i]) != 0
                except Exception:
                    on = bool(sig[i])
                if not on:
                    continue
                d_out = int(d)
                if start and d_out < int(start):
                    continue
                if end and d_out > int(end):
                    continue
                events.append(SignalEvent(code, d_out, period, spec.id))

    calc = BaguaCalculator.from_json(cfg.bagua_json)
    attach_bagua(events, period_raw_map, calc)
    n_with_bagua = sum(1 for e in events if getattr(e, "bagua", None))
    return {
        "events": events,
        "n_codes": n_codes,
        "codes": code_list,
        "period": period,
        "start": start,
        "end": end,
        "rule_ids": [s.id for s in trade_specs],
        "n_signals": len(events),
        "n_with_bagua": n_with_bagua,
        "errors_sample": errors[:20],
        "n_errors": len(errors),
        "research_unadjusted": research_unadjusted,
    }


def preview_filter(
    gua_filter: dict,
    *,
    cfg: Optional[AStockConfig] = None,
    signal_preview: bool = False,
    rule_ids: Optional[Sequence[str]] = None,
    codes: Optional[Sequence[str]] = None,
    period: str = "DAY",
    start: Optional[int] = None,
    end: Optional[int] = None,
    max_codes: int = 20,
    min_sample: int = 30,
) -> dict:
    """Preview filter vs 384 knowledge rows; optionally count real signals on a sample pool."""
    from ..bagua.filter_rules import compute_bagua_metrics

    cfg = cfg or get_default_config()
    kb = load_kb(cfg)
    gf = GuaFilter.from_dict(gua_filter)

    class _Ev:
        def __init__(self, bagua):
            self.bagua = bagua

    matched = []
    for e in kb.get("entries") or []:
        pub = _entry_public(e)
        bg = {
            "state_id": pub["state_id"],
            "gua_order": pub["gua_order"],
            "main_hexagram_id": pub["main_hexagram_id"],
            "yao_order": pub["yao_order"],
            "yao_name": pub["yao_name"],
            "action_signal": pub["action_signal"],
            "full_name": pub["full_name"],
            "gua_name": pub["gua_name"],
            "biangua": pub.get("biangua") or "",
        }
        if not gf.is_active() or event_matches_gua_filter(_Ev(bg), gf):
            matched.append(pub)

    n_states = len(kb.get("entries") or [])
    n_match = len(matched) if gf.is_active() else n_states
    empty_bg_matched = sum(
        1 for m in matched if not (m.get("biangua") or m.get("changed_hexagram_name"))
    )
    names = hexagram_name_map(cfg)
    warnings: List[str] = []
    if gf.is_active() and n_match == 0:
        warnings.append("当前条件在 384 爻规则表上无命中状态。")
    elif gf.is_active() and n_match < 12:
        warnings.append(f"规则表仅命中 {n_match} 条爻象状态，历史信号样本可能偏少。")
    if empty_bg_matched:
        warnings.append("部分命中状态缺少变卦信息（空变卦仍可按主卦/爻位匹配）。")

    mains = set(int(m["main_hexagram_id"]) for m in matched)
    acts: Dict[str, int] = {}
    for m in matched:
        a = m.get("action_signal") or ""
        if a:
            acts[a] = acts.get(a, 0) + 1

    out: Dict[str, Any] = {
        "enabled": gf.is_active(),
        "selection_mode": gf.selection_mode,
        "rule_version": gf.rule_version or rule_version(cfg),
        "selected_main_hexagram_count": len(gf.selected_main_hexagram_ids),
        "selected_exact_line_count": len(gf.selected_state_ids),
        "selected_action_signal_count": len(gf.selected_action_signals),
        "matched_state_count": n_match if gf.is_active() else n_states,
        "total_state_count": n_states,
        "matched_main_hexagram_count": len(mains),
        "action_signal_breakdown": acts,
        "empty_biangua_in_match": empty_bg_matched,
        "state_coverage_ratio": (n_match / n_states) if n_states else 0.0,
        "natural_language": gua_filter_natural_language(gf, hexagram_names=names),
        "warnings": warnings,
        "note": "规则表预览：匹配 384 爻知识库状态（非历史信号）。",
        "matched_state_ids_sample": [m["state_id"] for m in matched[:20]],
        "signal_preview": None,
    }

    if signal_preview:
        if not rule_ids:
            out["signal_preview"] = {
                "ok": False,
                "error": "signal_preview requires rule_ids",
            }
            out["warnings"] = list(out["warnings"]) + [
                "未提供指标 rule_ids，跳过真实信号预览。"
            ]
        else:
            try:
                sig = collect_indicator_signals_with_bagua(
                    cfg,
                    rule_ids=list(rule_ids),
                    codes=codes,
                    period=period,
                    start=start,
                    end=end,
                    max_codes=max_codes,
                    research_unadjusted=True,
                )
                metrics = compute_bagua_metrics(
                    sig["events"], gf, min_sample=min_sample
                )
                out["signal_preview"] = {
                    "ok": True,
                    "n_codes": sig["n_codes"],
                    "codes": sig["codes"],
                    "period": sig["period"],
                    "start": sig["start"],
                    "end": sig["end"],
                    "rule_ids": sig["rule_ids"],
                    "n_signals": sig["n_signals"],
                    "n_with_bagua": sig["n_with_bagua"],
                    "n_errors": sig["n_errors"],
                    "errors_sample": sig["errors_sample"],
                    "metrics": metrics,
                }
                for w in metrics.get("warnings") or []:
                    if w not in out["warnings"]:
                        out["warnings"].append(w)
                out["note"] = (
                    "规则表预览 + 真实信号抽样："
                    f"{sig['n_codes']} 只股票 · {sig['n_signals']} 条技术信号 · "
                    f"过滤后 {metrics.get('n_signals_after')} 条。"
                )
            except Exception as e:
                out["signal_preview"] = {"ok": False, "error": str(e)}
                out["warnings"].append(f"真实信号预览失败：{e}")

    return out


def run_bagua_metrics_for_run(cfg: AStockConfig, run_id: str) -> dict:
    """Compute bagua metrics for a finished run from signals.csv or meta."""
    import csv
    import json
    from types import SimpleNamespace

    from ..bagua.filter_rules import compute_bagua_metrics

    out_dir = Path(cfg.output_root) / run_id
    if not out_dir.exists():
        raise FileNotFoundError(run_id)

    meta: dict = {}
    for name in ("run_meta.json", "meta.json"):
        p = out_dir / name
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
            break
    repro = meta.get("repro") if isinstance(meta.get("repro"), dict) else {}
    gf_raw = meta.get("gua_filter")
    if gf_raw is None:
        gf_raw = repro.get("gua_filter")
    gf = GuaFilter.from_dict(gf_raw if isinstance(gf_raw, dict) else None)

    events = []
    signals_path = out_dir / "signals.csv"
    if signals_path.exists():
        with open(signals_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bg: Dict[str, Any] = {}
                mapping = {
                    "bagua_state_id": "state_id",
                    "bagua_action_signal": "action_signal",
                    "bagua_gua_order": "gua_order",
                    "bagua_full_name": "full_name",
                    "bagua_yao_name": "yao_name",
                    "bagua_biangua": "biangua",
                    "bagua_judgement": "market_judgement",
                    "bagua_core_gang": "core_gang",
                }
                for src, dst in mapping.items():
                    if row.get(src) not in (None, ""):
                        bg[dst] = row.get(src)
                for key in (
                    "state_id",
                    "gua_order",
                    "main_hexagram_id",
                    "yao_order",
                    "yao_name",
                    "action_signal",
                    "full_name",
                    "gua_name",
                    "biangua",
                ):
                    if row.get(key) not in (None, ""):
                        bg[key] = row.get(key)
                if bg.get("gua_order") not in (None, ""):
                    try:
                        bg["gua_order"] = int(float(bg["gua_order"]))
                        bg.setdefault("main_hexagram_id", bg["gua_order"])
                    except (TypeError, ValueError):
                        pass
                if bg.get("full_name") and not bg.get("gua_name"):
                    bg["gua_name"] = bg["full_name"]
                events.append(
                    SimpleNamespace(
                        std_code=row.get("std_code") or row.get("code"),
                        date=row.get("date"),
                        bagua=bg or None,
                    )
                )

    if events:
        metrics = compute_bagua_metrics(events, gf if gf.is_active() else None)
        metrics["source"] = "signals.csv"
    else:
        before = meta.get("n_signals_before_bagua")
        if before is None and isinstance(gf_raw, dict):
            before = gf_raw.get("n_signals_before")
        after = meta.get("n_signals_after_bagua")
        if after is None and isinstance(gf_raw, dict):
            after = gf_raw.get("n_signals_after")
        metrics = {
            "n_signals_before": before,
            "n_signals_after": after,
            "retention_rate": (
                (float(after) / float(before))
                if before not in (None, 0, "0") and after is not None
                else None
            ),
            "filter_active": gf.is_active(),
            "selection_mode": gf.selection_mode,
            "sample_sufficient": isinstance(after, (int, float)) and int(after) >= 30,
            "before": None,
            "after": None,
            "warnings": [
                "未找到带卦象明细的 signals.csv，仅返回回测 meta 中的过滤前后数量。"
            ],
            "source": "meta_only",
        }

    metrics["run_id"] = run_id
    metrics["gua_filter"] = gf.to_dict() if gf else None
    metrics["natural_language"] = gua_filter_natural_language(
        gf, hexagram_names=hexagram_name_map(cfg)
    )
    return metrics

