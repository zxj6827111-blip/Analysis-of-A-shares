# -*- coding: utf-8 -*-
"""Resolve A-share display names (code6 -> 股票名称).

Sources (first hit wins, cached):
1. Forecast weekly snapshot stocks.jsonl (if active week present)
2. TDX hq_cache/infoharbor_ex.code (GBK pipe file)
3. universe.json SymbolInfo.name (often empty)
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, Optional

from ..config import AStockConfig
from ..forecast.name_norm import normalize_stock_code

_lock = threading.Lock()
_cache: Dict[str, str] = {}
_loaded_for: Optional[str] = None  # cache key of sources used


def _clean_name(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    # TDX often pads full-width spaces, e.g. "万  科Ａ"
    s = re.sub(r"[\s\u3000]+", "", s)
    # normalize full-width A
    s = s.replace("Ａ", "A").replace("Ｂ", "B")
    return s


def _load_from_forecast_weekly(cfg: AStockConfig) -> Dict[str, str]:
    out: Dict[str, str] = {}
    weekly = getattr(cfg, "forecast_weekly_dir", None)
    froot = getattr(cfg, "forecast_root", None)
    root = Path(weekly or (Path(froot or "") / "weekly"))
    if not root.exists():
        return out
    index_path = root / "index.json"
    week_key = None
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            week_key = idx.get("active_week_key")
            if not week_key:
                weeks = idx.get("weeks") or {}
                if isinstance(weeks, dict) and weeks:
                    week_key = sorted(weeks.keys())[-1]
                elif isinstance(weeks, list) and weeks:
                    keys = [
                        str(w.get("week_key"))
                        for w in weeks
                        if isinstance(w, dict) and w.get("week_key")
                    ]
                    week_key = sorted(keys)[-1] if keys else None
        except Exception:
            week_key = None
    snap = root / "snapshots"
    candidates = []
    if week_key:
        candidates.append(snap / str(week_key) / "stocks.jsonl")
        candidates.append(snap / str(week_key) / "etfs.jsonl")
    if snap.exists():
        for d in sorted(snap.iterdir(), reverse=True):
            if d.is_dir():
                candidates.append(d / "stocks.jsonl")
                candidates.append(d / "etfs.jsonl")
    seen = set()
    for path in candidates:
        path = Path(path)
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    code = normalize_stock_code(row.get("code6") or row.get("code"))
                    name = _clean_name(row.get("name") or "")
                    if code and name and code not in out:
                        out[code] = name
        except Exception:
            continue
    return out


def _load_from_tdx_infoharbor(cfg: AStockConfig) -> Dict[str, str]:
    out: Dict[str, str] = {}
    tdx_root = getattr(cfg, "tdx_root", None)
    tdx = Path(tdx_root) if tdx_root else None
    if not tdx:
        return out
    candidates = [
        tdx / "T0002" / "hq_cache" / "infoharbor_ex.code",
        tdx / "hq_cache" / "infoharbor_ex.code",
        tdx / "T0002" / "hq_cache" / "infoharbor_ex.name",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="gbk", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            # formats:
            # 000001|平安银行|...
            # 0|001248|华润新能 (infoharbor_ex.name)
            if len(parts) >= 3 and re.fullmatch(r"\d", parts[0] or ""):
                code = normalize_stock_code(parts[1])
                name = _clean_name(parts[2])
            else:
                code = normalize_stock_code(parts[0])
                name = _clean_name(parts[1] if len(parts) > 1 else "")
            if code and name and code not in out:
                out[code] = name
        if out:
            break
    return out


def _load_from_universe(cfg: AStockConfig) -> Dict[str, str]:
    out: Dict[str, str] = {}
    path = getattr(cfg, "universe_path", None)
    if path is None:
        path = Path(cfg.storage_root) / "universe.json"
    path = Path(path)
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for s in data.get("symbols") or []:
        if not isinstance(s, dict):
            continue
        code = normalize_stock_code(s.get("code") or s.get("std_code") or s.get("raw"))
        name = _clean_name(s.get("name") or "")
        if code and name:
            out[code] = name
    return out


def _source_fingerprint(cfg: AStockConfig) -> str:
    parts = []
    weekly = getattr(cfg, "forecast_weekly_dir", None) or (
        Path(getattr(cfg, "forecast_root", "") or "") / "weekly"
    )
    tdx = getattr(cfg, "tdx_root", None) or ""
    storage = getattr(cfg, "storage_root", None) or ""
    for p in (
        Path(weekly or "") / "index.json",
        Path(tdx) / "T0002" / "hq_cache" / "infoharbor_ex.code",
        Path(storage) / "universe.json",
    ):
        try:
            if p.exists():
                st = p.stat()
                parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except Exception:
            continue
    return "|".join(parts) or "empty"


def ensure_name_cache(cfg: AStockConfig, *, force: bool = False) -> Dict[str, str]:
    global _cache, _loaded_for
    fp = _source_fingerprint(cfg)
    with _lock:
        if not force and _loaded_for == fp and _cache:
            return _cache
        merged: Dict[str, str] = {}
        # Prefer TDX full list, then overlay forecast (usually fresher names)
        for loader in (_load_from_tdx_infoharbor, _load_from_forecast_weekly, _load_from_universe):
            try:
                chunk = loader(cfg)
            except Exception:
                chunk = {}
            for k, v in chunk.items():
                if k and v:
                    merged[k] = v
        _cache = merged
        _loaded_for = fp
        return _cache


def resolve_stock_name(
    cfg: AStockConfig,
    code: str,
    *,
    std_code: str = "",
) -> str:
    """Return Chinese stock name for code, or empty string."""
    code6 = normalize_stock_code(code or std_code)
    if not code6 and std_code:
        code6 = normalize_stock_code(std_code.split(".")[-1])
    if not code6:
        return ""
    cache = ensure_name_cache(cfg)
    return cache.get(code6) or ""


def display_code_with_name(code: str, name: str) -> str:
    code = (code or "").strip()
    name = (name or "").strip()
    if code and name:
        return f"{code} {name}"
    return code or name or ""
