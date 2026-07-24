"""Corporate action / adjustment factors for A-share bars.

Four-lane price architecture (formal):
- raw: unadjusted market OHLC — execution, valuation, limits, bagua OHLC.
- standard_qfq: factor_t / factor_snapshot_end — default technical signals
  (reproducible only with frozen factor snapshot for the run).
- point_in_time_adjusted (alias causal_qfq): factor_t / base_factor (first
  finite) — advanced research / audit reference only; never cash or shares.
- Factors on date t use only events with event_date <= t (forward-filled).
- When Baostock is unavailable, formal backtest is No-Go unless the user
  explicitly enables research_unadjusted mode (signals also raw; exec still raw).

foreAdjustFactor interval semantics (Baostock):
- Each event records the cumulative forward-adjustment factor *effective on
  and after* dividOperateDate (inclusive).
- For any trading date d, the correct factor is the latest event with
  event_date <= d.
- Events *before* the local bar history start still matter: the last event
  with event_date <= first_local_date must seed the series. Using default 1.0
  for the pre-first-local-event segment is wrong whenever an earlier event
  exists (classic 600000 2016-06-22/23 discontinuity).
- Therefore we always query Baostock from a sufficiently early start
  (default 1990-01-01), not from the local first bar date.
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

# Query window floor so we capture the last event before local history.
BAOSTOCK_HISTORY_START = "1990-01-01"


@dataclass
class FactorSeries:
    std_code: str
    dates: List[int]
    factors: List[float]  # aligned to trading dates (forward-filled)
    source: str
    source_detail: str = ""
    event_dates: List[int] = field(default_factory=list)
    event_factors: List[float] = field(default_factory=list)
    sha256: str = ""
    prehistory_factor: Optional[float] = None  # last event factor <= first local date
    quality: str = "unknown"  # complete | no_events_identity | incomplete | error

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_identity(self) -> bool:
        return self.source in (
            "identity",
            "identity_missing",
            "identity_error",
            "identity_no_events",
        ) or self.quality in (
            "incomplete",
            "error",
            "no_events_identity",
            "forced_identity",
        )


def identity_factors(n: int) -> np.ndarray:
    return np.ones(int(n), dtype=np.float64)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def align_factors_to_dates(
    events: Dict[int, float],
    dates: Sequence[int],
    *,
    default: Optional[float] = None,
    seed_factor: Optional[float] = None,
) -> np.ndarray:
    """Forward-fill factor events onto trading dates without future leakage.

    On date d, use the latest event with event_date <= d.

    Seed / default rules:
    - If seed_factor is provided, it is the factor in force before any event
      in `events` that falls on/after the first date (typically the last
      historical event with event_date <= first_local_date).
    - If seed_factor is None and default is None, dates before the first event
      remain NaN (caller must treat as incomplete).
    - If default is provided (legacy), it is used only when seed_factor is None.
    """
    n = len(dates)
    if seed_factor is not None:
        start = float(seed_factor)
        out = np.full(n, start, dtype=np.float64)
    elif default is not None:
        out = np.full(n, float(default), dtype=np.float64)
        start = float(default)
    else:
        out = np.full(n, np.nan, dtype=np.float64)
        start = np.nan

    if not events:
        return out

    keys = sorted(int(k) for k in events.keys())
    j = 0
    cur = start
    for i, d in enumerate(dates):
        d = int(d)
        while j < len(keys) and keys[j] <= d:
            cur = float(events[keys[j]])
            j += 1
        out[i] = cur
    return out


def seed_factor_for_dates(
    events: Dict[int, float],
    first_date: int,
) -> Tuple[Optional[float], Optional[int]]:
    """Return (factor, event_date) of the latest event with event_date <= first_date.

    If an event falls exactly on first_date it is both the seed and the first
    in-range event; seed is still that factor (no 1.0 gap before it).
    """
    if not events:
        return None, None
    prior = [(int(d), float(f)) for d, f in events.items() if int(d) <= int(first_date)]
    if not prior:
        return None, None
    prior.sort(key=lambda x: x[0])
    d, f = prior[-1]
    return f, d


def causal_qfq_scale(
    factor: np.ndarray,
    *,
    base_factor: Optional[float] = None,
) -> np.ndarray:
    """Causal forward-adjustment scale: factor / base.

    BaoStock ``foreAdjustFactor`` is cumulative and effective on/after each
    event date. Dividing by ``factor[-1]`` (point-in-time qfq to the last bar)
    rewrites all historical adjusted prices when a later corporate action is
    appended ? a look-ahead leak for any backtest that loads a longer series
    than the decision horizon.

    Fixed base = first finite non-zero factor in the aligned series (the
    prehistory seed on the first local bar). This base depends only on events
    with event_date <= first_local_date. Return ratios are invariant to base:

        (p_t * f_t / base) / (p_s * f_s / base) - 1 == (p_t * f_t)/(p_s * f_s) - 1

    so 600000 2016-06-22?23 remains ~-0.51% while remaining causal.
    """
    factor = np.asarray(factor, dtype=np.float64)
    if factor.size == 0:
        return factor.copy()
    if base_factor is None:
        base = np.nan
        for v in factor:
            if np.isfinite(v) and float(v) != 0.0:
                base = float(v)
                break
        if not np.isfinite(base) or base == 0.0:
            return np.ones_like(factor, dtype=np.float64)
    else:
        base = float(base_factor)
        if not np.isfinite(base) or base == 0.0:
            return np.ones_like(factor, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = factor / base
    return np.where(np.isfinite(scale), scale, 1.0).astype(np.float64)



def standard_qfq_scale(
    factor: np.ndarray,
    *,
    snapshot_end_factor: Optional[float] = None,
) -> np.ndarray:
    """Standard ordinary forward-adjustment (通达信/东财风格锚点).

    scale_t = factor_t / factor_snapshot_end

    ``factor_snapshot_end`` is the cumulative factor at the **data snapshot
    cutoff** for this load (default: last finite non-zero factor in the
    aligned series). Absolute levels re-anchor when the snapshot gains a
    later corporate action — callers must record factor_manifest_sha /
    market_data_cutoff for reproducibility. Never use this series for
    shares, cash, commission, or account equity.
    """
    factor = np.asarray(factor, dtype=np.float64)
    if factor.size == 0:
        return factor.copy()
    if snapshot_end_factor is None:
        end = np.nan
        for v in factor[::-1]:
            if np.isfinite(v) and float(v) != 0.0:
                end = float(v)
                break
        if not np.isfinite(end) or end == 0.0:
            return np.ones_like(factor, dtype=np.float64)
    else:
        end = float(snapshot_end_factor)
        if not np.isfinite(end) or end == 0.0:
            return np.ones_like(factor, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = factor / end
    return np.where(np.isfinite(scale), scale, 1.0).astype(np.float64)



def asof_forward_adjusted_scale(
    factor: np.ndarray,
    *,
    asof_factor: Optional[float] = None,
    asof_index: Optional[int] = None,
) -> np.ndarray:
    """Historical time-point dynamic forward-adjustment (时点动态前复权).

    scale_t = factor_t / factor_asof

    ``factor_asof`` is the cumulative factor known at the observation time T
    (default: last finite factor in the series, same as standard_qfq for a
    fixed snapshot). For a query/backtest as-of date T, pass the factor on
    or before T so later corporate actions do not re-anchor history.

    Unlike point_in_time_adjusted (起点锚定 factor_t/base_first), this keeps
    the *asof day* price level equal to raw when factor_asof == factor_T.

    Signal / chart only — never for cash, shares, or fees.
    """
    factor = np.asarray(factor, dtype=np.float64)
    if factor.size == 0:
        return factor.copy()
    if asof_factor is not None:
        end = float(asof_factor)
    elif asof_index is not None:
        i = int(asof_index)
        if i < 0:
            i = factor.size + i
        i = max(0, min(i, factor.size - 1))
        end = float(factor[i]) if np.isfinite(factor[i]) and factor[i] != 0.0 else np.nan
        if not np.isfinite(end) or end == 0.0:
            # walk backward from asof_index
            end = np.nan
            for v in factor[i::-1]:
                if np.isfinite(v) and float(v) != 0.0:
                    end = float(v)
                    break
    else:
        end = np.nan
        for v in factor[::-1]:
            if np.isfinite(v) and float(v) != 0.0:
                end = float(v)
                break
    if not np.isfinite(end) or end == 0.0:
        return np.ones_like(factor, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = factor / end
    return np.where(np.isfinite(scale), scale, 1.0).astype(np.float64)


def factor_value_on_or_before(
    dates: Sequence[int],
    factors: Sequence[float],
    asof: int,
) -> Optional[float]:
    """Last factor with date <= asof from aligned series."""
    best = None
    best_d = None
    for d, f in zip(dates, factors):
        try:
            di, fv = int(d), float(f)
        except (TypeError, ValueError):
            continue
        if di <= int(asof) and np.isfinite(fv) and fv != 0.0:
            if best_d is None or di >= best_d:
                best_d = di
                best = fv
    return best


def point_in_time_adjusted_scale(
    factor: np.ndarray,
    *,
    base_factor: Optional[float] = None,
) -> np.ndarray:
    """起点锚定复权研究价 scale: factor_t / base_factor (first finite).

    Alias of historical ``causal_qfq_scale``. Research / audit only.
    """
    return causal_qfq_scale(factor, base_factor=base_factor)


def apply_standard_qfq(
    raw: Dict[str, np.ndarray],
    factor: np.ndarray,
    *,
    snapshot_end_factor: Optional[float] = None,
) -> Dict[str, np.ndarray]:
    """Apply standard ordinary qfq: price * standard_qfq_scale(factor)."""
    factor = np.asarray(factor, dtype=np.float64)
    if len(factor) == 0:
        return {k: np.asarray(v).copy() for k, v in raw.items()}
    scale = standard_qfq_scale(factor, snapshot_end_factor=snapshot_end_factor)
    out = {k: np.asarray(v).copy() for k, v in raw.items()}
    for key in ("open", "high", "low", "close"):
        if key in out:
            out[key] = out[key].astype(np.float64) * scale
    out["adj_factor"] = factor
    out["adj_scale"] = scale
    return out


def apply_qfq(
    raw: Dict[str, np.ndarray],
    factor: np.ndarray,
    *,
    base_factor: Optional[float] = None,
) -> Dict[str, np.ndarray]:
    """Apply 起点锚定复权研究价: price * causal_qfq_scale(factor).

    Never uses factor[-1] (that is standard_qfq). Volume/amount unchanged.
    Research/audit only — not for execution cash.
    """
    factor = np.asarray(factor, dtype=np.float64)
    if len(factor) == 0:
        return {k: np.asarray(v).copy() for k, v in raw.items()}
    scale = causal_qfq_scale(factor, base_factor=base_factor)
    out = {k: np.asarray(v).copy() for k, v in raw.items()}
    for key in ("open", "high", "low", "close"):
        if key in out:
            out[key] = out[key].astype(np.float64) * scale
    out["adj_factor"] = factor
    out["adj_scale"] = scale
    return out


def fetch_baostock_factor_events(
    std_code: str,
    start: str,
    end: str,
) -> Tuple[Optional[Dict[int, float]], str]:
    """Fetch foreAdjustFactor events from baostock.

    Returns (events, detail). events is None if unavailable.
    Empty dict means query succeeded but no events (true identity candidate).
    """
    try:
        import baostock as bs  # type: ignore
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
    try:
        rs = bs.query_adjust_factor(code=bs_code, start_date=start, end_date=end)
        events: Dict[int, float] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # fields: code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor
            try:
                d = str(row[1]).replace("-", "")
                f = float(row[2])  # foreAdjustFactor
                events[int(d)] = f
            except (TypeError, ValueError, IndexError):
                continue
        if not events:
            return {}, f"baostock_empty:{bs_code}"
        return events, f"baostock:{bs_code};start={start};end={end}"
    except Exception as e:  # noqa: BLE001
        return None, f"baostock_error:{e}"
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def build_factor_series(
    std_code: str,
    dates: Sequence[int],
    *,
    adj_root: Path,
    prefer_baostock: bool = True,
    force_identity: bool = False,
    refresh: bool = False,
    history_start: str = BAOSTOCK_HISTORY_START,
) -> FactorSeries:
    """Load or fetch factors for dates; persist under adj_root.

    Always queries Baostock from history_start (not local first bar) so the
    last pre-history event can seed the first local segment.
    """
    adj_root = Path(adj_root)
    adj_root.mkdir(parents=True, exist_ok=True)
    dates_i = [int(d) for d in dates]
    cache_path = adj_root / f"{std_code.replace('.', '_')}.json"

    if force_identity:
        fac = identity_factors(len(dates_i)).tolist()
        series = FactorSeries(
            std_code=std_code,
            dates=dates_i,
            factors=fac,
            source="identity",
            source_detail="force_identity",
            quality="forced_identity",
            sha256=sha256_text(json.dumps({"std_code": std_code, "factors": fac})),
        )
        _save_series(cache_path, series)
        return series

    if not refresh and cache_path.exists():
        cached = load_factor_file(cache_path, dates_i)
        if cached is not None and cached.quality == "complete" and not cached.is_identity:
            return cached
        # incomplete/error caches are rebuilt when prefer_baostock

    events: Optional[Dict[int, float]] = None
    detail = ""
    source = "identity_missing"
    quality = "incomplete"

    if prefer_baostock and dates_i:
        end = (
            f"{dates_i[-1]//10000:04d}-{(dates_i[-1]//100)%100:02d}-{dates_i[-1]%100:02d}"
        )
        # CRITICAL: query from history_start, not local first bar date
        events, detail = fetch_baostock_factor_events(std_code, history_start, end)
        if events is None:
            source = "identity_error"
            quality = "error"
            if cache_path.exists():
                cached = load_factor_file(cache_path, dates_i)
                if cached is not None and cached.quality == "complete":
                    return cached
            events = {}
        elif not events:
            source = "identity_no_events"
            quality = "no_events_identity"
        else:
            source = "baostock"
            quality = "complete"

    if events is None:
        events = {}

    first = dates_i[0] if dates_i else 0
    seed, seed_date = seed_factor_for_dates(events, first)

    if quality == "complete":
        # Must have a defined factor on every local date.
        # IPO / early bars before the first Baostock event: seed with the
        # earliest event factor (same cumulative level until that event).
        # Leaving NaN would block formal full-market backtests for new listings.
        if seed is None and events:
            first_ev = min(int(k) for k in events.keys())
            seed = float(events[first_ev])
            seed_date = first_ev
            detail = (detail or "") + (";seed_first_event=%s" % first_ev)
        arr = align_factors_to_dates(events, dates_i, seed_factor=seed)
        if np.any(np.isnan(arr)):
            arr = np.asarray(arr, dtype=np.float64)
            last = np.nan
            for i in range(len(arr)):
                if np.isfinite(arr[i]):
                    last = float(arr[i])
                elif np.isfinite(last):
                    arr[i] = last
            last = np.nan
            for i in range(len(arr) - 1, -1, -1):
                if np.isfinite(arr[i]):
                    last = float(arr[i])
                elif np.isfinite(last):
                    arr[i] = last
            if np.any(np.isnan(arr)):
                quality = "incomplete"
                source = "baostock_incomplete_nan"
            else:
                detail = (detail or "") + ";nan_ffill_bfill"
    elif quality == "no_events_identity":
        arr = identity_factors(len(dates_i))
        seed = 1.0
    else:
        arr = identity_factors(len(dates_i))
        seed = 1.0

    keys = sorted(events.keys())
    series = FactorSeries(
        std_code=std_code,
        dates=dates_i,
        factors=arr.tolist(),
        source=source,
        source_detail=detail + (f";seed_event={seed_date}" if seed_date else ""),
        event_dates=keys,
        event_factors=[float(events[k]) for k in keys],
        prehistory_factor=float(seed) if seed is not None else None,
        quality=quality,
        sha256="",
    )
    payload = {
        "std_code": std_code,
        "source": series.source,
        "source_detail": series.source_detail,
        "quality": series.quality,
        "prehistory_factor": series.prehistory_factor,
        "event_dates": series.event_dates,
        "event_factors": series.event_factors,
        "dates": series.dates,
        "factors": series.factors,
    }
    series.sha256 = sha256_text(json.dumps(payload, sort_keys=True))
    payload["sha256"] = series.sha256
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return series


def _save_series(path: Path, series: FactorSeries) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = series.to_dict()
    if not series.sha256:
        series.sha256 = sha256_text(json.dumps(payload, sort_keys=True, default=str))
        payload["sha256"] = series.sha256
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_factor_file(path: Path, dates: Sequence[int]) -> Optional[FactorSeries]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    events: Dict[int, float] = {}
    if data.get("event_dates") and data.get("event_factors"):
        for d, f in zip(data["event_dates"], data["event_factors"]):
            events[int(d)] = float(f)

    dates_i = [int(d) for d in dates]
    first = dates_i[0] if dates_i else 0
    seed = data.get("prehistory_factor")
    if seed is None:
        seed, _ = seed_factor_for_dates(events, first)

    quality = data.get("quality") or (
        "complete" if data.get("source") == "baostock" and events else "incomplete"
    )
    if quality == "complete" and seed is not None:
        arr = align_factors_to_dates(events, dates_i, seed_factor=float(seed))
    elif quality == "complete" and not events:
        arr = identity_factors(len(dates_i))
        quality = "no_events_identity"
    else:
        # prefer stored factors if date-aligned
        if data.get("dates") and data.get("factors") and len(data["dates"]) == len(dates_i):
            if [int(x) for x in data["dates"]] == dates_i:
                arr = np.array([float(x) for x in data["factors"]], dtype=np.float64)
            else:
                fmap = {int(d): float(f) for d, f in zip(data["dates"], data["factors"])}
                arr = np.array([fmap.get(int(d), np.nan) for d in dates_i], dtype=np.float64)
        else:
            arr = align_factors_to_dates(events, dates_i, seed_factor=seed if seed is not None else None)

    return FactorSeries(
        std_code=data.get("std_code", ""),
        dates=dates_i,
        factors=arr.tolist(),
        source=data.get("source", "file"),
        source_detail=data.get("source_detail", str(path)),
        event_dates=[int(x) for x in data.get("event_dates", [])],
        event_factors=[float(x) for x in data.get("event_factors", [])],
        prehistory_factor=float(seed) if seed is not None else data.get("prehistory_factor"),
        quality=quality,
        sha256=data.get("sha256", ""),
    )


def formal_adjustment_ready(series_list: Sequence[FactorSeries]) -> Tuple[bool, str]:
    """Formal backtest requires complete factors from a trusted query path.

    Accept:
      - quality=complete and source in {baostock, cached_baostock, file}
      - quality=no_events_identity AND source in {identity_no_events, baostock}
        (Baostock query succeeded with an empty event list)

    Reject:
      - quality=forced_identity or source_detail containing force_identity
      - source=identity (manual/forced)
      - identity_missing / incomplete / error
    """
    if not series_list:
        return False, "no_factor_series"
    bad = []
    for s in series_list:
        detail = s.source_detail or ""
        if s.quality == "forced_identity" or "force_identity" in detail:
            bad.append(s)
            continue
        if s.source == "identity":
            bad.append(s)
            continue
        if s.quality == "complete" and s.source in ("baostock", "file", "cached_baostock"):
            continue
        if s.quality == "no_events_identity" and s.source in (
            "identity_no_events",
            "baostock",
        ):
            continue
        bad.append(s)
    if bad:
        reasons = sorted({f"{s.std_code}:{s.source}/{s.quality}" for s in bad})
        return False, (
            "adjustment_factors_unavailable_or_incomplete: "
            + ",".join(reasons[:8])
            + (";..." if len(reasons) > 8 else "")
            + "; formal backtest is No-Go unless --research-unadjusted"
        )
    return True, "ok"


def factor_manifest_sha(series_list: Sequence[FactorSeries]) -> str:
    payload = [
        {
            "std_code": s.std_code,
            "source": s.source,
            "quality": s.quality,
            "sha256": s.sha256,
            "prehistory_factor": s.prehistory_factor,
        }
        for s in sorted(series_list, key=lambda x: x.std_code)
    ]
    return sha256_text(json.dumps(payload, sort_keys=True))
