# -*- coding: utf-8 -*-
"""Layer-1 indicator signal cache (disk, fingerprint-keyed)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import AStockConfig, get_default_config
from ..study import SignalEvent
from .fingerprint import short_fingerprint


CACHE_SCHEMA = "signal_cache_v1"


def default_signal_cache_dir(cfg: Optional[AStockConfig] = None) -> Path:
    cfg = cfg or get_default_config()
    root = Path(cfg.storage_root) / "cache" / "signals"
    root.mkdir(parents=True, exist_ok=True)
    return root


def signal_cache_key(
    *,
    indicator_ids: Sequence[str],
    indicator_source_hash: Optional[str] = None,
    period: str,
    start: Optional[int],
    end: Optional[int],
    universe_hash: Optional[str],
    adjust_mode: str,
    factor_manifest_sha: Optional[str] = None,
    market_data_version: Optional[str] = None,
    calendar_version: Optional[str] = None,
    combine: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    data_source: Optional[str] = None,
    adjustment: Optional[str] = None,
    dataset_id: Optional[str] = None,
    weekly_bar_mode: Optional[str] = None,
    anchor_date: Optional[int] = None,
    execution_data_source: Optional[str] = None,
    execution_dataset_id: Optional[str] = None,
    universe_version: Optional[str] = None,
    raw_parent_dataset_id: Optional[str] = None,
    factor_parent_dataset_id: Optional[str] = None,
    formula_version: Optional[str] = None,
    anchor_policy: Optional[str] = None,
) -> str:
    # factor_manifest_sha isolates cache when cumulative factors change (CA).
    # data_source/dataset_id isolate cache across different market data sources.
    # execution_dataset_id isolates cache across different L2 execution datasets.
    payload = {
        "schema": CACHE_SCHEMA,
        "indicator_ids": list(indicator_ids or []),
        "indicator_source_hash": indicator_source_hash,
        "period": (period or "DAY").upper(),
        "start": start,
        "end": end,
        "universe_hash": universe_hash,
        "adjust_mode": adjust_mode,
        "factor_manifest_sha": factor_manifest_sha or "",
        "market_data_version": market_data_version,
        "calendar_version": calendar_version,
        "combine": combine,
        "extra": extra or {},
        "data_source": data_source or "",
        "adjustment": adjustment or "",
        "dataset_id": dataset_id or "",
        "weekly_bar_mode": weekly_bar_mode or "local_aggregate",
        "anchor_date": anchor_date,
        "execution_data_source": execution_data_source or "local_vendor",
        "execution_dataset_id": execution_dataset_id or "",
        "universe_version": universe_version or "",
        # Gate C: derived-signal lineage isolates cache when the factor parent
        # (or derivation formula/anchor) changes even if dataset naming aligns.
        "raw_parent_dataset_id": raw_parent_dataset_id or "",
        "factor_parent_dataset_id": factor_parent_dataset_id or "",
        "formula_version": formula_version or "",
        "anchor_policy": anchor_policy or "",
    }
    return short_fingerprint(payload, n=32)


def _path_for(key: str, cfg: Optional[AStockConfig] = None) -> Path:
    return default_signal_cache_dir(cfg) / f"{key}.json"


def events_to_records(events: Sequence[SignalEvent]) -> List[dict]:
    out: List[dict] = []
    for e in events:
        if hasattr(e, "to_dict"):
            out.append(e.to_dict())
        else:
            out.append(
                {
                    "std_code": e.std_code,
                    "date": e.date,
                    "period": e.period,
                    "source": getattr(e, "source", None),
                    "is_dwm": bool(getattr(e, "is_dwm", False)),
                    "bagua": getattr(e, "bagua", None),
                }
            )
    return out


def records_to_events(rows: Sequence[dict]) -> List[SignalEvent]:
    events: List[SignalEvent] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        raw_date = r.get("date")
        if raw_date is None:
            continue
        try:
            date_i = int(raw_date)
        except (TypeError, ValueError):
            continue
        ev = SignalEvent(
            std_code=str(r.get("std_code") or r.get("code") or ""),
            date=date_i,
            period=str(r.get("period") or "DAY"),
            indicator_id=str(
                r.get("indicator_id") or r.get("source") or r.get("indicator") or ""
            ),
            value=int(r.get("value") or 1),
            bagua=r.get("bagua"),
            is_dwm=bool(r.get("is_dwm")),
        )
        events.append(ev)
    return events



def load_signal_cache(
    key: str,
    *,
    cfg: Optional[AStockConfig] = None,
) -> Optional[List[SignalEvent]]:
    path = _path_for(key, cfg)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != CACHE_SCHEMA:
            return None
        return records_to_events(data.get("events") or [])
    except Exception:
        return None


def save_signal_cache(
    key: str,
    events: Sequence[SignalEvent],
    *,
    cfg: Optional[AStockConfig] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Path:
    path = _path_for(key, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "schema": CACHE_SCHEMA,
        "key": key,
        "saved_at": int(time.time()),
        "n_events": len(list(events)),
        "meta": meta or {},
        "events": events_to_records(events),
    }
    path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    return path


def get_or_compute_signals(
    key: str,
    compute_fn,
    *,
    cfg: Optional[AStockConfig] = None,
    meta: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
) -> tuple[List[SignalEvent], bool]:
    """Return (events, cache_hit). compute_fn() -> List[SignalEvent]."""
    if use_cache:
        hit = load_signal_cache(key, cfg=cfg)
        if hit is not None:
            return hit, True
    events = list(compute_fn() or [])
    if use_cache:
        try:
            save_signal_cache(key, events, cfg=cfg, meta=meta)
        except Exception:
            pass
    return events, False
