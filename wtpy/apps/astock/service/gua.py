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


def preview_filter(
    gua_filter: dict,
    *,
    cfg: Optional[AStockConfig] = None,
) -> dict:
    """Lightweight hit preview against the 384 knowledge rows (not full market signals).

    Reports how many of the 384 states match the filter, plus empty-biangua warnings.
    Full historical signal counts require a backtest run; this stays O(384).
    """
    kb = load_kb(cfg)
    gf = GuaFilter.from_dict(gua_filter)
    # synthetic events as simple objects
    class _Ev:
        def __init__(self, bagua):
            self.bagua = bagua

    matched = []
    for e in kb.get("entries") or []:
        pub = _entry_public(e)
        # bagua-shaped dict for matcher
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
    empty_bg_matched = sum(1 for m in matched if not (m.get("biangua") or m.get("changed_hexagram_name")))
    names = hexagram_name_map(cfg)
    warnings: List[str] = []
    if gf.is_active() and n_match == 0:
        warnings.append("当前指标条件与卦象条件组合后没有历史信号，请减少筛选条件。")
    elif gf.is_active() and n_match < 12:
        warnings.append(f"当前条件仅命中{n_match}条爻象状态，样本数量较少，回测结果可能缺乏统计意义。")
    if empty_bg_matched:
        warnings.append("部分卦象记录缺少变卦信息，但仍可按主卦和爻位匹配。")

    mains = set(int(m["main_hexagram_id"]) for m in matched)
    acts = {}
    for m in matched:
        a = m.get("action_signal") or ""
        if a:
            acts[a] = acts.get(a, 0) + 1

    return {
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
        "note": "预览基于 384 爻象规则表匹配，完整「历史信号命中」以回测结果中的原始/过滤后信号数为准。",
        "matched_state_ids_sample": [m["state_id"] for m in matched[:20]],
    }


def reimport_excel(xlsx_path: Path | str, cfg: Optional[AStockConfig] = None) -> dict:
    from ..bagua.rebuild_from_excel import rebuild_knowledge_from_excel, validate_knowledge

    cfg = cfg or get_default_config()
    out = _kb_path(cfg)
    kb = rebuild_knowledge_from_excel(xlsx_path, out)
    report = validate_knowledge(kb)
    invalidate_kb_cache()
    return report
