# -*- coding: utf-8 -*-
"""Yao (卦爻) rule manifest + experiment helpers for 爻辞回测 first rounds.

Does not change bagua OHLC calculation. Filtering uses state_id / exact_line.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

# Fixed hold templates (trading days) used by 爻辞 plan T3..T120
HOLD_TEMPLATE_DAYS: List[int] = [3, 5, 7, 10, 20, 40, 60, 120]

# Demo pool used by experiment center smoke runs (canonical warehouse format)
DEMO_CODES: List[str] = ["SSE.STK.600000", "SZSE.STK.000001"]

# Default manifest path (repo-relative); overridable via AStockConfig later if needed
DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "research" / "data" / "yao_rules_manifest.json"
)


def hold_sell_options(
    holds: Optional[Sequence[int]] = None,
    *,
    sell_on: Union[str, Sequence[str]] = "close",
) -> List[Dict[str, Any]]:
    """Build sell_options list for free-axes: hold N trading days × open/close."""
    days = list(holds) if holds is not None else list(HOLD_TEMPLATE_DAYS)
    if isinstance(sell_on, str):
        sessions = [sell_on]
    else:
        sessions = list(sell_on) or ["close"]
    out: List[Dict[str, Any]] = []
    for h in days:
        hi = int(h)
        if hi < 1:
            continue
        for so in sessions:
            s = str(so or "close").lower()
            if s not in ("open", "close"):
                s = "close"
            out.append({"hold": hi, "sell_on": s, "exit_weekday": None})
    return out


def state_id_gua_filter(state_ids: Sequence[str], *, label: str = "") -> Dict[str, Any]:
    """Inline gua_filter payload for exact_line multi/single state_id."""
    sids = [str(x).strip() for x in state_ids if str(x).strip()]
    return {
        "enabled": bool(sids),
        "selection_mode": "exact_line" if sids else "none",
        "selected_main_hexagram_ids": [],
        "selected_state_ids": sids,
        "selected_action_signals": [],
        "label": label or ("+".join(sids) if sids else "none"),
    }


def single_state_gua_option(state_id: str, *, label: str = "") -> Dict[str, Any]:
    sid = str(state_id).strip()
    return state_id_gua_filter([sid], label=label or sid)


def load_yao_manifest(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_MANIFEST_PATH
    if not p.exists():
        return {
            "version": "empty",
            "rules": [],
            "path": str(p),
            "exists": False,
        }
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("yao manifest must be a JSON object")
    data["path"] = str(p)
    data["exists"] = True
    return data


def manifest_rules(
    *,
    path: Optional[Union[str, Path]] = None,
    status: Optional[Sequence[str]] = None,
    groups: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    man = load_yao_manifest(path)
    rules = list(man.get("rules") or [])
    if status:
        allow = {str(s) for s in status}
        rules = [r for r in rules if str(r.get("status") or "") in allow]
    if groups:
        allow_g = {str(g) for g in groups}
        rules = [r for r in rules if str(r.get("group") or "") in allow_g]
    return rules


def gua_options_from_manifest(
    *,
    path: Optional[Union[str, Path]] = None,
    status: Optional[Sequence[str]] = None,
    groups: Optional[Sequence[str]] = None,
    include_none: bool = True,
) -> List[Any]:
    """List of gua options for ParameterSpace: preset 'none' and/or inline filters."""
    opts: List[Any] = []
    if include_none:
        opts.append("none")
    for r in manifest_rules(path=path, status=status, groups=groups):
        sid = str(r.get("state_id") or "").strip()
        if not sid:
            continue
        label = str(r.get("name") or r.get("rule_id") or sid)
        gf = single_state_gua_option(sid, label=label)
        gf["rule_id"] = r.get("rule_id")
        gf["manifest_name"] = label
        opts.append(gf)
    return opts


def resolve_universe_codes(
    cfg,
    *,
    universe: Optional[str] = None,
    codes: Optional[Sequence[str]] = None,
) -> List[str]:
    """Resolve experiment stock universe.

    universe:
      - demo | None + no codes → DEMO_CODES
      - full | all | market → entire universe.json
      - custom → require codes
    Explicit non-empty ``codes`` always used (custom/demo subset).
    """
    from .backtest import select_universe

    uni = (universe or "").strip().lower() if universe else ""
    # Explicit codes: prefer them unless universe forces full market
    if codes and uni not in ("full", "all", "market", "全a", "全市场", "alla"):
        resolved = select_universe(cfg, list(codes))
        if resolved:
            return resolved
        return [str(c) for c in codes]
    if uni in ("full", "all", "market", "全a", "全市场", "alla"):
        try:
            resolved = select_universe(cfg, None)
            if resolved:
                return resolved
        except Exception:
            pass
        raise ValueError("universe=full but universe.json is empty or missing")
    if uni in ("custom",):
        if not codes:
            raise ValueError("universe=custom requires non-empty codes")
        resolved = select_universe(cfg, list(codes))
        return resolved or [str(c) for c in codes]
    # demo default
    if codes:
        resolved = select_universe(cfg, list(codes))
        return resolved or [str(c) for c in codes]
    return list(DEMO_CODES)


def normalize_periods(period: Optional[str] = None, periods: Optional[Sequence[str]] = None) -> List[str]:
    """Return list of periods (DAY/WEEK/...)."""
    if periods:
        out = []
        for p in periods:
            s = str(p or "").strip().upper()
            if s and s not in out:
                out.append(s)
        if out:
            return out
    s = str(period or "DAY").strip().upper() or "DAY"
    return [s]
