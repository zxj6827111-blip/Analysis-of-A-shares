# -*- coding: utf-8 -*-
"""Layer-3 execution result cache (params+engine fingerprint → metrics)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AStockConfig, get_default_config
from .fingerprint import short_fingerprint

# v3: factor event-anchored snap changes CA-gated results (phantom Tushare
# adj_factor micro-drift no longer blocks trades) — v2 entries are stale.
CACHE_SCHEMA = "execution_cache_v3"


def default_execution_cache_dir(cfg: Optional[AStockConfig] = None) -> Path:
    cfg = cfg or get_default_config()
    root = Path(cfg.storage_root) / "cache" / "execution"
    root.mkdir(parents=True, exist_ok=True)
    return root


def execution_cache_key(payload: Dict[str, Any]) -> str:
    body = {"schema": CACHE_SCHEMA, **(payload or {})}
    return short_fingerprint(body, n=32)


def _path_for(key: str, cfg: Optional[AStockConfig] = None) -> Path:
    return default_execution_cache_dir(cfg) / f"{key}.json"


def load_execution_cache(
    key: str, *, cfg: Optional[AStockConfig] = None
) -> Optional[Dict[str, Any]]:
    path = _path_for(key, cfg)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != CACHE_SCHEMA:
            return None
        return data
    except Exception:
        return None


def save_execution_cache(
    key: str,
    *,
    metrics: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    cfg: Optional[AStockConfig] = None,
) -> Path:
    path = _path_for(key, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {
        "schema": CACHE_SCHEMA,
        "key": key,
        "saved_at": int(time.time()),
        "metrics": metrics or {},
        "meta": meta or {},
    }
    path.write_text(json.dumps(blob, ensure_ascii=False, default=str), encoding="utf-8")
    return path
