# -*- coding: utf-8 -*-
"""Layer-2 filtered signal cache (weekday / gua / other filters)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import AStockConfig, get_default_config
from ..study import SignalEvent
from .fingerprint import short_fingerprint
from .signal_cache import events_to_records, records_to_events

CACHE_SCHEMA = "filter_cache_v1"


def default_filter_cache_dir(cfg: Optional[AStockConfig] = None) -> Path:
    cfg = cfg or get_default_config()
    root = Path(cfg.storage_root) / "cache" / "filtered_signals"
    root.mkdir(parents=True, exist_ok=True)
    return root


def filter_cache_key(
    *,
    signal_cache_key: str,
    signal_weekdays: Optional[Sequence[int]] = None,
    gua_rule_version: Optional[str] = None,
    gua_filter: Optional[Dict[str, Any]] = None,
    with_bagua: Optional[bool] = None,
    bagua_filter_mode: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    payload = {
        "schema": CACHE_SCHEMA,
        "signal_cache_key": signal_cache_key,
        "signal_weekdays": list(signal_weekdays) if signal_weekdays is not None else None,
        "gua_rule_version": gua_rule_version,
        "gua_filter": gua_filter,
        "with_bagua": with_bagua,
        "bagua_filter_mode": bagua_filter_mode,
        "extra": extra or {},
    }
    return short_fingerprint(payload, n=32)


def _path_for(key: str, cfg: Optional[AStockConfig] = None) -> Path:
    return default_filter_cache_dir(cfg) / f"{key}.json"


def load_filter_cache(
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


def save_filter_cache(
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


def get_or_compute_filtered(
    key: str,
    compute_fn,
    *,
    cfg: Optional[AStockConfig] = None,
    meta: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
) -> tuple[List[SignalEvent], bool]:
    if use_cache:
        hit = load_filter_cache(key, cfg=cfg)
        if hit is not None:
            return hit, True
    events = list(compute_fn() or [])
    if use_cache:
        try:
            save_filter_cache(key, events, cfg=cfg, meta=meta)
        except Exception:
            pass
    return events, False
