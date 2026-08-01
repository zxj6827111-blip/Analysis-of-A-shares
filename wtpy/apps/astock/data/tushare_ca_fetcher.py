# -*- coding: utf-8 -*-
"""Tushare corporate-action event loading and local cache helpers.

The backtest core can read cached events without importing Tushare. Network
access is lazy and is used only by explicit synchronization/fetch operations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from wtpy.apps.astock.ca_ledger import (
    CA_CASH_DIVIDEND,
    CA_SHARE_RATIO,
    CorporateActionEvent,
)

DIVIDEND_FIELDS = (
    "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
    "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
    "imp_ann_date,base_date,base_share"
)

TUSHARE_CA_CACHE: Dict[str, List[CorporateActionEvent]] = {}

_IMPLEMENTED_DIVIDEND_MARKERS = (
    "\u5b9e\u65bd",  # Tushare Chinese value: implemented
    "implemented",
    "implementation",
)


def _is_implemented_dividend(value: Any) -> bool:
    proc = str(value or "").strip().lower()
    return any(marker in proc for marker in _IMPLEMENTED_DIVIDEND_MARKERS)


def default_ca_root() -> Path:
    return Path(os.getenv("MARKET_DATA_ROOT", r"E:\AStockData")) / "ca_events"


def standard_to_tushare_code(code: str) -> str:
    """Convert SSE.STK.600000 / SZSE.STK.000001 / BSE.STK.430047 to TS code."""
    raw = str(code or "").strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")) and raw.count(".") == 1:
        return raw
    parts = raw.split(".")
    if len(parts) == 3:
        exchange, _asset, symbol = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange)
        if suffix:
            return f"{symbol}.{suffix}"
    return raw


def tushare_to_standard_code(code: str) -> str:
    """Convert TS code to the canonical code used by the backtest engine."""
    raw = str(code or "").strip().upper()
    if raw.count(".") == 2:
        return raw
    if "." not in raw:
        return raw
    symbol, suffix = raw.rsplit(".", 1)
    exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix)
    return f"{exchange}.STK.{symbol}" if exchange else raw


def _as_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _as_date(value: Any) -> Optional[int]:
    raw = str(value or "").strip().replace("-", "")
    if not raw or raw.lower() in ("nan", "none", "nat"):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _records(rows: Any) -> Iterable[Mapping[str, Any]]:
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):
        return rows.to_dict(orient="records")
    return rows


def _in_range(date: int, start_date: Optional[int], end_date: Optional[int]) -> bool:
    if start_date is not None and int(date) < int(start_date):
        return False
    if end_date is not None and int(date) > int(end_date):
        return False
    return True


def dividend_rows_to_events(
    rows: Any,
    *,
    default_code: Optional[str] = None,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
) -> List[CorporateActionEvent]:
    """Convert Tushare ``dividend`` rows into explicit economic events.

    Only implemented rows with an ex-date are used. ``stk_div`` is already the
    per-share total送转 ratio, so the new-share multiplier is ``1 + stk_div``.
    ``stk_bo_rate + stk_co_rate`` is used only when ``stk_div`` is absent/zero.
    """
    events: List[CorporateActionEvent] = []
    seen = set()
    for row in _records(rows):
        proc = str(row.get("div_proc") or "").strip()
        if not _is_implemented_dividend(proc):
            continue
        ex_date = _as_date(row.get("ex_date"))
        if ex_date is None or not _in_range(ex_date, start_date, end_date):
            continue
        code = tushare_to_standard_code(row.get("ts_code") or default_code or "")
        if not code:
            continue

        cash_div = _as_float(row.get("cash_div"))
        cash_div_tax = _as_float(row.get("cash_div_tax"))
        if cash_div > 0:
            key = (code, ex_date, CA_CASH_DIVIDEND, round(cash_div, 12))
            if key not in seen:
                seen.add(key)
                events.append(
                    CorporateActionEvent(
                        std_code=code,
                        date=ex_date,
                        event_type=CA_CASH_DIVIDEND,
                        cash_per_share=cash_div,
                        note=f"tushare_cash_div {cash_div:.12g}",
                        source="tushare_dividend",
                        meta={
                            "div_proc": proc,
                            "cash_div": cash_div,
                            "cash_div_tax": cash_div_tax,
                        },
                    )
                )

        stk_div = _as_float(row.get("stk_div"))
        if stk_div <= 0:
            stk_div = _as_float(row.get("stk_bo_rate")) + _as_float(
                row.get("stk_co_rate")
            )
        if stk_div > 0:
            multiplier = 1.0 + stk_div
            key = (code, ex_date, CA_SHARE_RATIO, round(multiplier, 12))
            if key not in seen:
                seen.add(key)
                events.append(
                    CorporateActionEvent(
                        std_code=code,
                        date=ex_date,
                        event_type=CA_SHARE_RATIO,
                        share_multiplier=multiplier,
                        note=f"tushare_share_multiplier {multiplier:.12g}",
                        source="tushare_dividend",
                        meta={
                            "div_proc": proc,
                            "stk_div": _as_float(row.get("stk_div")),
                            "stk_bo_rate": _as_float(row.get("stk_bo_rate")),
                            "stk_co_rate": _as_float(row.get("stk_co_rate")),
                        },
                    )
                )

    return sorted(events, key=lambda e: (int(e.date), e.std_code, e.event_type))


def _cache_path(code: str, root: Optional[Path] = None) -> Path:
    ca_root = Path(root) if root is not None else default_ca_root()
    return ca_root / f"{standard_to_tushare_code(code)}.json"


def _filter_events(
    events: Iterable[CorporateActionEvent],
    start_date: Optional[int],
    end_date: Optional[int],
) -> List[CorporateActionEvent]:
    return [
        event
        for event in events
        if _in_range(int(event.date), start_date, end_date)
    ]


def load_cached_dividend_events(
    code: str,
    *,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    root: Optional[Path] = None,
) -> List[CorporateActionEvent]:
    """Load one symbol's cached events, normalizing legacy TS-code payloads."""
    path = _cache_path(code, root)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    normalized: List[CorporateActionEvent] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        data = dict(item)
        data["std_code"] = tushare_to_standard_code(
            data.get("std_code") or code
        )
        try:
            normalized.append(CorporateActionEvent(**data))
        except (TypeError, ValueError):
            continue
    return _filter_events(normalized, start_date, end_date)


def load_cached_events_for_universe(
    codes: Iterable[str],
    *,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    root: Optional[Path] = None,
) -> Dict[str, List[CorporateActionEvent]]:
    result: Dict[str, List[CorporateActionEvent]] = {}
    for code in codes:
        events = load_cached_dividend_events(
            code,
            start_date=start_date,
            end_date=end_date,
            root=root,
        )
        if events:
            result[tushare_to_standard_code(code)] = events
    return result


def cached_events_metadata(
    events_by_code: Mapping[str, List[CorporateActionEvent]],
    *,
    root: Optional[Path] = None,
    requested_codes: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Build reproducible lineage for the selected cached event ledger."""
    from wtpy.apps.astock.ca_ledger import ledger_manifest_sha

    ca_root = Path(root) if root is not None else default_ca_root()
    meta_path = ca_root / "_meta.json"
    sync_meta: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                sync_meta = raw
        except (OSError, ValueError, TypeError):
            sync_meta = {"read_error": "invalid_ca_meta_json"}
    meta_sha = ""
    try:
        meta_sha = hashlib.sha256(meta_path.read_bytes()).hexdigest()
    except OSError:
        pass
    requested = list(requested_codes or [])
    event_count = sum(len(items or []) for items in events_by_code.values())
    return {
        "cache_root": str(ca_root),
        "cache_meta_path": str(meta_path),
        "cache_meta_sha256": meta_sha,
        "event_manifest_sha256": ledger_manifest_sha(dict(events_by_code)),
        "requested_symbol_count": len(requested),
        "event_symbol_count": len(events_by_code),
        "event_count": int(event_count),
        "last_sync_at": sync_meta.get("last_sync_at"),
        "last_sync_mode": sync_meta.get("last_sync_mode"),
        "sync_as_of_date": sync_meta.get("as_of_date"),
        "sync_ex_date_range": (
            sync_meta.get("ex_date_range") or sync_meta.get("sync_range")
        ),
        "sync_success": sync_meta.get("success"),
        "sync_failed": sync_meta.get("failed"),
        "sync_nonempty_symbols": sync_meta.get("nonempty_symbols"),
        "sync_event_count": sync_meta.get("event_count"),
    }


def _get_pro() -> Any:
    try:
        import tushare as ts
    except ImportError as exc:
        raise RuntimeError("tushare is required for online CA synchronization") from exc
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN") or ts.get_token()
    if not token:
        raise ValueError("TUSHARE_TOKEN is not set")
    return ts.pro_api(token)


def _write_events(path: Path, events: Iterable[CorporateActionEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            [event.to_dict() for event in events],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def fetch_dividend_events(
    code: str,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    *,
    root: Optional[Path] = None,
    refresh: bool = False,
    pro: Any = None,
) -> List[CorporateActionEvent]:
    """Read cache or refresh one symbol from the official ``dividend`` API.

    The API does not accept generic ``start_date``/``end_date`` inputs. A full
    per-symbol response is fetched and the requested ex-date range is filtered
    locally.
    """
    cache_key = str(_cache_path(code, root).resolve())
    if not refresh:
        if cache_key in TUSHARE_CA_CACHE:
            return _filter_events(TUSHARE_CA_CACHE[cache_key], start_date, end_date)
        cached = load_cached_dividend_events(
            code,
            start_date=None,
            end_date=None,
            root=root,
        )
        if _cache_path(code, root).exists():
            TUSHARE_CA_CACHE[cache_key] = cached
            return _filter_events(cached, start_date, end_date)

    provider = pro or _get_pro()
    ts_code = standard_to_tushare_code(code)
    frame = provider.dividend(ts_code=ts_code, fields=DIVIDEND_FIELDS)
    events = dividend_rows_to_events(frame, default_code=ts_code)
    _write_events(_cache_path(code, root), events)
    TUSHARE_CA_CACHE[cache_key] = events
    return _filter_events(events, start_date, end_date)


def preload_ca_for_universe(
    codes: List[str],
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    batch_size: int = 30,
    *,
    root: Optional[Path] = None,
    refresh: bool = False,
    pro: Any = None,
    sleep_seconds: float = 0.0,
) -> Dict[str, List[CorporateActionEvent]]:
    """Load or refresh events for a universe; empty symbols are omitted."""
    all_events: Dict[str, List[CorporateActionEvent]] = {}
    provider = pro or (_get_pro() if refresh else None)
    for i in range(0, len(codes), max(1, int(batch_size))):
        for code in codes[i : i + max(1, int(batch_size))]:
            events = fetch_dividend_events(
                code,
                start_date,
                end_date,
                root=root,
                refresh=refresh,
                pro=provider,
            )
            if events:
                all_events[tushare_to_standard_code(code)] = events
            if refresh and sleep_seconds > 0:
                time.sleep(float(sleep_seconds))
    return all_events
