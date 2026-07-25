"""Affine corporate-action adjustment model.

Replaces pure-multiplicative factor model with event-level affine transforms:

    adjusted = a * raw + b

For a single event (cash dividend D, bonus B, transfer S, rights R at price P_r):

    a = 1 / (1 + B + S + R)
    b = -(D - P_r * R) / (1 + B + S + R)

Multiple events compose as:
    P_final = cum_a * P_raw + cum_b

This exactly reproduces TDX/THS forward-adjusted OHLC.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

BAOSTOCK_HISTORY_START = "1990-01-01"


@dataclass
class DividendEvent:
    event_date: int
    cash_per_share: float = 0.0
    bonus_per_share: float = 0.0
    transfer_per_share: float = 0.0
    rights_per_share: float = 0.0
    rights_price: float = 0.0

    @property
    def a(self) -> float:
        denom = 1.0 + self.bonus_per_share + self.transfer_per_share + self.rights_per_share
        return 1.0 / denom if denom != 0.0 else 1.0

    @property
    def b(self) -> float:
        denom = 1.0 + self.bonus_per_share + self.transfer_per_share + self.rights_per_share
        if denom == 0.0:
            return 0.0
        return -(self.cash_per_share - self.rights_price * self.rights_per_share) / denom

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AffineSeries:
    std_code: str
    dates: List[int]
    cum_a: List[float]
    cum_b: List[float]
    source: str
    source_detail: str = ""
    events: List[Dict] = field(default_factory=list)
    quality: str = "unknown"
    sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_identity(self) -> bool:
        return self.quality in ("no_events_identity", "forced_identity", "error")


def fetch_baostock_dividend_events(
    std_code: str,
) -> Tuple[Optional[List[DividendEvent]], str]:
    """Fetch corporate action events from Baostock query_dividend_data."""
    try:
        import baostock as bs
    except ImportError:
        return None, "baostock_not_installed"

    parts = std_code.split(".")
    if len(parts) == 3:
        exch, _, code = parts
    elif len(parts) == 2:
        exch, code = parts
    else:
        return None, f"bad_std_code:{std_code}"
    prefix = "sh" if exch.upper() in ("SSE", "SH") else "sz"
    bs_code = f"{prefix}.{code}"

    lg = bs.login()
    if getattr(lg, "error_code", "0") != "0":
        return None, f"baostock_login_failed:{getattr(lg, 'error_msg', '')}"

    events: List[DividendEvent] = []
    detail_parts = []
    try:
        for year in range(1990, 2027):
            rs = bs.query_dividend_data(code=bs_code, year=str(year), yearType="report")
            if rs.error_code != "0":
                continue
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                try:
                    operate_date = str(row[6]).replace("-", "")
                    if not operate_date or operate_date == "":
                        continue
                    d = int(operate_date)
                    cash = float(row[9]) if row[9] else 0.0
                    bonus = float(row[11]) if row[11] else 0.0
                    transfer = float(row[13]) if row[13] else 0.0
                    if cash == 0.0 and bonus == 0.0 and transfer == 0.0:
                        continue
                    events.append(DividendEvent(
                        event_date=d,
                        cash_per_share=cash,
                        bonus_per_share=bonus,
                        transfer_per_share=transfer,
                        rights_per_share=0.0,
                        rights_price=0.0,
                    ))
                except (TypeError, ValueError, IndexError):
                    continue
        detail_parts.append(f"baostock_dividend:{bs_code}")
    except Exception as e:
        return None, f"baostock_dividend_error:{e}"
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    events.sort(key=lambda e: e.event_date)
    seen = set()
    deduped = []
    for ev in events:
        if ev.event_date not in seen:
            seen.add(ev.event_date)
            deduped.append(ev)
    return deduped, ";".join(detail_parts) + f";n_events={len(deduped)}"


def compute_affine_params(
    events: Sequence[DividendEvent],
    dates: Sequence[int],
    *,
    anchor_index: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute per-date cumulative affine (cum_a, cum_b) for standard_qfq.

    anchor_index: the index in `dates` that serves as the anchor (price unchanged).
    Default: last index. Events with event_date > anchor_date are excluded.
    """
    n = len(dates)
    dates_i = [int(d) for d in dates]

    if anchor_index is None:
        anchor_index = n - 1
    anchor_date = dates_i[anchor_index]

    relevant = [ev for ev in events if ev.event_date <= anchor_date]

    cum_a_arr = np.ones(n, dtype=np.float64)
    cum_b_arr = np.zeros(n, dtype=np.float64)

    if not relevant:
        return cum_a_arr, cum_b_arr

    relevant_sorted = sorted(relevant, key=lambda e: e.event_date, reverse=True)

    cur_a = 1.0
    cur_b = 0.0

    for ev in relevant_sorted:
        new_a = cur_a * ev.a
        new_b = cur_a * ev.b + cur_b
        cur_a, cur_b = new_a, new_b

        for i, d in enumerate(dates_i):
            if d < ev.event_date:
                cum_a_arr[i] = cur_a
                cum_b_arr[i] = cur_b

    return cum_a_arr, cum_b_arr


def compute_affine_params_asof(
    events: Sequence[DividendEvent],
    dates: Sequence[int],
    asof_date: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute per-date cumulative affine for asof_forward_qfq.

    Only events with event_date <= asof_date are used.
    Anchor = asof_date (price at asof = raw).
    """
    n = len(dates)
    dates_i = [int(d) for d in dates]

    relevant = [ev for ev in events if ev.event_date <= int(asof_date)]

    cum_a_arr = np.ones(n, dtype=np.float64)
    cum_b_arr = np.zeros(n, dtype=np.float64)

    if not relevant:
        return cum_a_arr, cum_b_arr

    relevant_sorted = sorted(relevant, key=lambda e: e.event_date, reverse=True)

    cur_a = 1.0
    cur_b = 0.0

    for ev in relevant_sorted:
        new_a = cur_a * ev.a
        new_b = cur_a * ev.b + cur_b
        cur_a, cur_b = new_a, new_b

        for i, d in enumerate(dates_i):
            if d < ev.event_date:
                cum_a_arr[i] = cur_a
                cum_b_arr[i] = cur_b

    return cum_a_arr, cum_b_arr


def build_affine_series(
    std_code: str,
    dates: Sequence[int],
    *,
    adj_root: Path,
    refresh: bool = False,
) -> AffineSeries:
    """Load or fetch dividend events; compute affine params; persist cache."""
    adj_root = Path(adj_root)
    adj_root.mkdir(parents=True, exist_ok=True)
    dates_i = [int(d) for d in dates]
    cache_path = adj_root / f"affine_{std_code.replace('.', '_')}.json"

    if not refresh and cache_path.exists():
        cached = _load_affine_cache(cache_path, dates_i)
        if cached is not None:
            return cached

    events, detail = fetch_baostock_dividend_events(std_code)

    if events is None:
        return AffineSeries(
            std_code=std_code,
            dates=dates_i,
            cum_a=[1.0] * len(dates_i),
            cum_b=[0.0] * len(dates_i),
            source="identity_error",
            source_detail=detail,
            quality="error",
        )

    if not events:
        series = AffineSeries(
            std_code=std_code,
            dates=dates_i,
            cum_a=[1.0] * len(dates_i),
            cum_b=[0.0] * len(dates_i),
            source="baostock_dividend",
            source_detail=detail,
            events=[],
            quality="no_events_identity",
        )
        _save_affine_cache(cache_path, series)
        return series

    cum_a, cum_b = compute_affine_params(events, dates_i)

    series = AffineSeries(
        std_code=std_code,
        dates=dates_i,
        cum_a=cum_a.tolist(),
        cum_b=cum_b.tolist(),
        source="baostock_dividend",
        source_detail=detail,
        events=[ev.to_dict() for ev in events],
        quality="complete",
    )
    payload = json.dumps(series.to_dict(), sort_keys=True, default=str)
    series.sha256 = hashlib.sha256(payload.encode()).hexdigest()
    _save_affine_cache(cache_path, series)
    return series


def _save_affine_cache(path: Path, series: AffineSeries) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = series.to_dict()
    if not series.sha256:
        series.sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        payload["sha256"] = series.sha256
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_affine_cache(path: Path, dates: Sequence[int]) -> Optional[AffineSeries]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    events_raw = data.get("events", [])
    events = [DividendEvent(**ev) for ev in events_raw] if events_raw else []

    dates_i = [int(d) for d in dates]

    if data.get("dates") and len(data["dates"]) == len(dates_i):
        if [int(x) for x in data["dates"]] == dates_i:
            cum_a = [float(x) for x in data.get("cum_a", [])]
            cum_b = [float(x) for x in data.get("cum_b", [])]
            if len(cum_a) == len(dates_i) and len(cum_b) == len(dates_i):
                return AffineSeries(
                    std_code=data.get("std_code", ""),
                    dates=dates_i,
                    cum_a=cum_a,
                    cum_b=cum_b,
                    source=data.get("source", "cache"),
                    source_detail=data.get("source_detail", str(path)),
                    events=[ev.to_dict() for ev in events],
                    quality=data.get("quality", "complete"),
                    sha256=data.get("sha256", ""),
                )

    if events:
        cum_a, cum_b = compute_affine_params(events, dates_i)
        return AffineSeries(
            std_code=data.get("std_code", ""),
            dates=dates_i,
            cum_a=cum_a.tolist(),
            cum_b=cum_b.tolist(),
            source=data.get("source", "cache"),
            source_detail=data.get("source_detail", str(path)),
            events=[ev.to_dict() for ev in events],
            quality=data.get("quality", "complete"),
            sha256=data.get("sha256", ""),
        )

    return None
