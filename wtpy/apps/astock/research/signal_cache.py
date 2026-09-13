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


CACHE_SCHEMA = "signal_cache_v2"  # v2: 增存 signal_errors（信号计算错误）


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
        "execution_data_source": execution_data_source or "internal",
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
    """公开契约不变：返回事件列表；None = 无缓存 / 损坏 / 旧 schema。

    内部完整记录（含 signal_errors）经 _load_blob 读取，供错误回放使用。
    """
    blob = _load_blob(key, cfg=cfg)
    return records_to_events(blob.get("events") or []) if blob else None


def _load_blob(
    key: str,
    *,
    cfg: Optional[AStockConfig] = None,
) -> Optional[Dict[str, Any]]:
    path = _path_for(key, cfg)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 旧 schema（v1 无 signal_errors）：按无法验证处理（返回 None → 重算），
    # 不把「未记录错误」当作「确定没有错误」。
    if data.get("schema") != CACHE_SCHEMA:
        return None
    return data


def save_signal_cache(
    key: str,
    events: Sequence[SignalEvent],
    *,
    cfg: Optional[AStockConfig] = None,
    meta: Optional[Dict[str, Any]] = None,
    signal_errors: Optional[List[dict]] = None,
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
        "signal_errors": list(signal_errors or []),
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
    errors_ref: Optional[List[dict]] = None,
) -> tuple[List[SignalEvent], bool]:
    """Return (events, cache_hit). compute_fn() -> List[SignalEvent].

    errors_ref 可选（旧调用兼容）：传入时，缓存**只保存并回放本次信号计算
    新增的错误**（调用前后切片），不吞入行情加载/覆盖率等既有错误；命中时
    把缓存的 signal_errors 追加回 errors_ref 末尾（错误每次运行都可见）。
    """
    if use_cache:
        blob = _load_blob(key, cfg=cfg)
        if blob is not None:
            if errors_ref is not None:
                errors_ref.extend(list(blob.get("signal_errors") or []))
            return records_to_events(blob.get("events") or []), True
    before = len(errors_ref) if errors_ref is not None else 0
    events = list(compute_fn() or [])
    signal_errors = list(errors_ref[before:]) if errors_ref is not None else []
    if use_cache:
        try:
            save_signal_cache(key, events, cfg=cfg, meta=meta, signal_errors=signal_errors)
        except Exception:
            pass
    return events, False
