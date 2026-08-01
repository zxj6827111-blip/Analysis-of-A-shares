#!/usr/bin/env python
"""Synchronize explicit corporate-action events from Tushare.

Usage:
  python scripts/sync_ca_events.py --mode full
  python scripts/sync_ca_events.py --mode incremental --days 90
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _ensure_package(name: str, path: Path) -> None:
    """Load only the A-stock package tree, without wtpy's native runtime imports."""
    if name in sys.modules:
        return
    module = ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    module.__package__ = name
    sys.modules[name] = module


_ensure_package("wtpy", PROJECT_ROOT / "wtpy")
_ensure_package("wtpy.apps", PROJECT_ROOT / "wtpy" / "apps")
_ensure_package("wtpy.apps.astock", PROJECT_ROOT / "wtpy" / "apps" / "astock")
_ensure_package(
    "wtpy.apps.astock.data",
    PROJECT_ROOT / "wtpy" / "apps" / "astock" / "data",
)

from wtpy.apps.astock.ca_ledger import CorporateActionEvent
from wtpy.apps.astock.data.tushare_ca_fetcher import (
    DIVIDEND_FIELDS,
    dividend_rows_to_events,
    standard_to_tushare_code,
    tushare_to_standard_code,
)

class _TushareHttpClient:
    """Minimal Tushare Pro HTTP client used when the SDK is unavailable."""

    endpoint = "https://api.tushare.pro"

    def __init__(self, token: str, timeout: float = 60.0):
        self.token = token
        self.timeout = float(timeout)

    def _query(self, api_name: str, *, fields: str = "", **params):
        payload = json.dumps(
            {
                "api_name": api_name,
                "token": self.token,
                "params": params,
                "fields": fields,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Tushare HTTP request failed: {exc}") from exc
        code = int(body.get("code") or 0)
        if code != 0:
            raise RuntimeError(
                "Tushare API error code=%s msg=%s"
                % (code, body.get("msg") or "unknown")
            )
        data = body.get("data") or {}
        names = list(data.get("fields") or [])
        return [dict(zip(names, item)) for item in (data.get("items") or [])]

    def stock_basic(self, **kwargs):
        return self._query("stock_basic", **kwargs)

    def dividend(self, **kwargs):
        return self._query("dividend", **kwargs)


def _provider(token: str):
    try:
        import tushare as ts
    except ImportError:
        return _TushareHttpClient(token)
    return ts.pro_api(token)


def _records(frame):
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return frame.to_dict(orient="records")
    return list(frame)


def _call_with_retry(func, *, retries: int, retry_delay: float, **kwargs):
    last_error = None
    for attempt in range(max(1, int(retries))):
        try:
            return func(**kwargs)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= max(1, int(retries)):
                break
            time.sleep(max(0.0, float(retry_delay)) * (attempt + 1))
    raise last_error


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _read_events(path: Path) -> List[CorporateActionEvent]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    result = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        data = dict(item)
        data["std_code"] = tushare_to_standard_code(data.get("std_code") or "")
        try:
            result.append(CorporateActionEvent(**data))
        except (TypeError, ValueError):
            continue
    return result


def _merge_events(
    old: List[CorporateActionEvent], new: List[CorporateActionEvent]
) -> List[CorporateActionEvent]:
    merged: Dict[tuple, CorporateActionEvent] = {}
    for event in old + new:
        key = (
            event.std_code,
            int(event.date),
            event.event_type,
            round(float(event.cash_per_share or 0.0), 12),
            round(float(event.share_multiplier or 1.0), 12),
        )
        merged[key] = event
    return sorted(merged.values(), key=lambda e: (e.date, e.std_code, e.event_type))


def _write_events(path: Path, events: List[CorporateActionEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _stock_codes(pro, *, retries: int, retry_delay: float) -> List[str]:
    codes = set()
    for status in ("L", "D"):
        frame = _call_with_retry(
            pro.stock_basic,
            retries=retries,
            retry_delay=retry_delay,
            exchange="",
            list_status=status,
            fields="ts_code",
        )
        for row in _records(frame):
            code = str((row or {}).get("ts_code") or "").strip()
            if code:
                codes.add(code)
    return sorted(codes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync CA events from Tushare")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    parser.add_argument("--days", type=int, default=90, help="incremental ex-date lookback")
    parser.add_argument("--storage-root", type=str, default=None)
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.22,
        help="seconds between dividend calls (about 270 calls/min before retries)",
    )
    parser.add_argument(
        "--codes",
        type=str,
        default=None,
        help="optional comma-separated TS/standard codes for smoke or retry",
    )
    parser.add_argument(
        "--as-of-date",
        type=int,
        default=None,
        help="inclusive YYYYMMDD cutoff; defaults to the current local date",
    )
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    args = parser.parse_args()

    _load_dotenv(PROJECT_ROOT / ".env")
    root = (
        Path(args.storage_root)
        if args.storage_root
        else Path(os.getenv("MARKET_DATA_ROOT", r"E:\AStockData"))
    )
    ca_dir = root / "ca_events"
    ca_dir.mkdir(parents=True, exist_ok=True)
    meta_file = ca_dir / "_meta.json"

    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
    if not token:
        try:
            import tushare as ts
            token = ts.get_token()
        except ImportError:
            token = None
    if not token:
        print("[CA_SYNC] ERROR: TUSHARE_TOKEN not set")
        return 1
    pro = _provider(token)

    end_date = int(args.as_of_date or datetime.now().strftime("%Y%m%d"))
    if len(str(end_date)) != 8:
        parser.error("--as-of-date must be YYYYMMDD")
    start_date = 19900101 if args.mode == "full" else int(
        (datetime.now() - timedelta(days=max(1, args.days))).strftime("%Y%m%d")
    )
    if args.codes:
        codes = sorted(
            {
                standard_to_tushare_code(code.strip())
                for code in args.codes.split(",")
                if code.strip()
            }
        )
    else:
        codes = _stock_codes(
            pro,
            retries=args.retries,
            retry_delay=args.retry_delay,
        )
    if not codes:
        print("[CA_SYNC] ERROR: cannot get stock list")
        return 1

    print(f"[CA_SYNC] mode={args.mode} ex_date_range={start_date}~{end_date}")
    print(f"[CA_SYNC] total stocks (listed + delisted): {len(codes)}")

    success = failed = nonempty = event_count = 0
    failed_codes: List[str] = []
    for i, ts_code in enumerate(codes, 1):
        path = ca_dir / f"{standard_to_tushare_code(ts_code)}.json"
        try:
            # dividend accepts ann_date/record_date/ex_date/imp_ann_date filters,
            # not generic start_date/end_date. Filter the returned ex_date locally.
            frame = _call_with_retry(
                pro.dividend,
                retries=args.retries,
                retry_delay=args.retry_delay,
                ts_code=ts_code,
                fields=DIVIDEND_FIELDS,
            )
            ranged_events = dividend_rows_to_events(
                frame,
                default_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if args.mode == "incremental":
                events = _merge_events(_read_events(path), ranged_events)
            else:
                events = ranged_events
            _write_events(path, events)
            success += 1
            if events:
                nonempty += 1
                event_count += len(events)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failed_codes.append(ts_code)
            if failed <= 10:
                print(f"[CA_SYNC] WARN {ts_code}: {exc}")
        if i % 50 == 0 or i == len(codes):
            print(
                f"[SYNC_PROGRESS] done={i} total={len(codes)} "
                f"nonempty={nonempty} failed={failed} phase=ca_events"
            )
        if args.sleep > 0:
            time.sleep(args.sleep)

    meta = {
        "last_sync_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_sync_mode": args.mode,
        "ex_date_range": f"{start_date}~{end_date}",
        "as_of_date": end_date,
        "total_stocks": len(codes),
        "success": success,
        "failed": failed,
        "nonempty_symbols": nonempty,
        "event_count": event_count,
        "query_filter": "per_ts_code_full_response_then_local_ex_date_filter",
        "failed_codes": failed_codes,
        "provider": type(pro).__name__,
    }
    tmp = meta_file.with_suffix(meta_file.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_file)

    print(
        f"[CA_SYNC] DONE success={success} failed={failed} "
        f"nonempty={nonempty} events={event_count}"
    )
    print(f"[CA_SYNC] meta saved to {meta_file}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
