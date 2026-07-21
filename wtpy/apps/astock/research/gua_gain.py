# -*- coding: utf-8 -*-
"""Gua (hexagram filter) incremental gain pairing (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def _tech_key(row: Dict[str, Any], tech_keys: Sequence[str]) -> Tuple[Any, ...]:
    return tuple(row.get(k) for k in tech_keys)


def pair_gua_gain(
    rows: Sequence[Dict[str, Any]],
    *,
    tech_keys: Optional[Sequence[str]] = None,
    gua_key: str = "gua_key",
    baseline_gua: str = "none",
    contrast_gua: str = "best3",
    metric_keys: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Pair trials with same tech params: gua none vs best3 (or configured).

    Returns list of deltas: return / drawdown / win_rate / trade_count.
    """
    if tech_keys is None:
        tech_keys = (
            "exit_weekday",
            "sell_on",
            "hold",
            "rule_ids",
            "entry_weekday",
            "tech_params",
            "param_hash",
        )
    if metric_keys is None:
        metric_keys = (
            "total_return",
            "max_drawdown",
            "win_rate",
            "trade_count",
            "n_trades",
        )

    # index by tech fingerprint + gua
    by_tech: Dict[Tuple[Any, ...], Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        # skip keys not present in row when building fingerprint — use only present tech keys
        present = [k for k in tech_keys if k in r]
        if not present and "params" in r and isinstance(r["params"], dict):
            base = dict(r["params"])
            base.update({k: r[k] for k in r if k != "params"})
            r_eff = base
            present = [k for k in tech_keys if k in r_eff]
        else:
            r_eff = r
        tk = tuple(r_eff.get(k) for k in present) if present else tuple(
            sorted((k, str(v)) for k, v in r_eff.items() if k not in (gua_key, "metrics", "id"))
        )
        # prefer stable tech identity
        if "tech_id" in r_eff:
            tk = ("tech_id", r_eff.get("tech_id"))
        elif all(k in r_eff for k in ("exit_weekday", "sell_on")):
            hold = r_eff.get("hold")
            tk = (r_eff.get("exit_weekday"), r_eff.get("sell_on"), hold, r_eff.get("rule_ids"))

        gk = str(r_eff.get(gua_key) if r_eff.get(gua_key) is not None else r.get(gua_key) or "none")
        by_tech.setdefault(tk, {})[gk] = r

    out: List[Dict[str, Any]] = []
    for tk, gua_map in by_tech.items():
        base = gua_map.get(str(baseline_gua))
        cont = gua_map.get(str(contrast_gua))
        if base is None or cont is None:
            continue

        def _metric(row: Dict[str, Any], key: str) -> float:
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    pass
            m = row.get("metrics") or {}
            if key in m and m[key] is not None:
                try:
                    return float(m[key])
                except (TypeError, ValueError):
                    pass
            # aliases
            aliases = {
                "trade_count": ("n_trades", "trades"),
                "n_trades": ("trade_count", "trades"),
                "total_return": ("return",),
                "max_drawdown": ("drawdown",),
            }
            for a in aliases.get(key, ()):
                if a in row and row[a] is not None:
                    return float(row[a])
                if a in m and m[a] is not None:
                    return float(m[a])
            return 0.0

        delta: Dict[str, Any] = {
            "tech_key": tk,
            "baseline_gua": baseline_gua,
            "contrast_gua": contrast_gua,
            "baseline": base,
            "contrast": cont,
        }
        # standard deltas
        for mk, label in (
            ("total_return", "delta_return"),
            ("max_drawdown", "delta_drawdown"),
            ("win_rate", "delta_win_rate"),
            ("trade_count", "delta_trade_count"),
        ):
            b = _metric(base, mk)
            c = _metric(cont, mk)
            if mk == "trade_count" and b == 0.0 and c == 0.0:
                b = _metric(base, "n_trades")
                c = _metric(cont, "n_trades")
            delta[label] = c - b
            delta[f"baseline_{mk}"] = b
            delta[f"contrast_{mk}"] = c

        out.append(delta)
    return out


__all__ = ["pair_gua_gain"]
