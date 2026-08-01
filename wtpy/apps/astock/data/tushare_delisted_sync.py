"""Pure helpers for the Gate B2 Tushare delisted-daily sync.

Offline-testable logic only: pagination merging, validation, unit
transformation and error classification. Network calls live in
scripts/sync_tushare_delisted.py, never here.

Units (validated by the B1 probe, median implied-price ratio ~1.0):
  Tushare daily vol    = 手   -> bars volume = vol * 100 (股)
  Tushare daily amount = 千元 -> bars amount = amount * 1000 (元)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

UNIT_TRANSFORM_VERSION = "tushare_vol_hand_x100_amount_kcny_x1000_v1"
API_NAME = "tushare.pro.daily(paginated)"
TUSHARE_DAILY_PAGE_ROWS = 6000

# per-symbol download lifecycle
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_VALIDATED = "validated"
STATUS_NO_DATA = "no_data"
STATUS_FAILED = "failed"
STATUS_PUBLISHED = "published"

ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_DOWNLOADING,
    STATUS_DOWNLOADED,
    STATUS_VALIDATED,
    STATUS_NO_DATA,
    STATUS_FAILED,
    STATUS_PUBLISHED,
)


def scrub_secret(text: str) -> str:
    """Mask token-like hex material in error strings."""
    return re.sub(r"[0-9a-fA-F]{24,}", "<scrubbed>", str(text))


def classify_sync_error(exc: Exception) -> str:
    """Map an exception to an error class (never silently swallowed)."""
    s = str(exc).lower()
    if "permission" in s or "权限" in s or "积分" in s:
        return "permission_denied"
    if "token" in s or "认证" in s or "auth" in s:
        return "auth_failed"
    if "limit" in s or "频率" in s or "freq" in s or "too many" in s:
        return "rate_limited"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    if "connection" in s or "network" in s or "resolve" in s:
        return "network_error"
    return "api_failed"


def fetch_daily_paginated(
    fetch_page: Callable[..., pd.DataFrame],
    ts_code: str,
    *,
    page_rows: int = TUSHARE_DAILY_PAGE_ROWS,
    max_pages: int = 20,
) -> Tuple[pd.DataFrame, int]:
    """Fetch full daily history for ts_code, walking past the 6000-row cap.

    fetch_page(ts_code=..., end_date=...) must return a newest-first frame
    (Tushare convention). Returns (deduped ascending-sorted frame, pages).
    Raises RuntimeError if max_pages is hit (guards infinite loops).
    """
    frames: List[pd.DataFrame] = []
    end_date = ""
    known_oldest: Optional[int] = None
    for page in range(max_pages):
        df = fetch_page(ts_code=ts_code, end_date=end_date)
        if df is None or df.empty:
            break
        oldest = int(pd.to_numeric(df["trade_date"]).min())
        if known_oldest is not None and oldest >= known_oldest:
            # no progress — page holds nothing older than what we have;
            # stop rather than spin (defensive against API quirks)
            break
        frames.append(df)
        known_oldest = oldest
        if len(df) < page_rows:
            break
        # next page strictly before the oldest date we have
        end_date = str(oldest - 1)
    else:
        raise RuntimeError(f"pagination exceeded {max_pages} pages for {ts_code}")
    if not frames:
        return pd.DataFrame(), 0
    merged = pd.concat(frames, ignore_index=True)
    merged["trade_date"] = pd.to_numeric(merged["trade_date"]).astype("int64")
    merged = (
        merged.drop_duplicates(subset="trade_date")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    return merged, len(frames)


@dataclass
class ValidationResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    row_count: int = 0
    first_date: Optional[int] = None
    last_date: Optional[int] = None
    dup_dates: int = 0
    ohlc_invalid: int = 0
    nonpositive_close: int = 0


def validate_daily_frame(df: pd.DataFrame) -> ValidationResult:
    """Validate a merged ascending daily frame (pre-transform, Tushare units)."""
    if df is None or df.empty:
        return ValidationResult(ok=False, reasons=["empty"])
    reasons: List[str] = []
    dates = pd.to_numeric(df["trade_date"]).astype("int64")
    dups = int(dates.duplicated().sum())
    if dups:
        reasons.append(f"duplicate_trade_dates:{dups}")
    if not dates.is_monotonic_increasing:
        reasons.append("dates_not_ascending")
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    lo = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    v = pd.to_numeric(df["vol"], errors="coerce")
    a = pd.to_numeric(df["amount"], errors="coerce")
    nan_rows = int((o.isna() | h.isna() | lo.isna() | c.isna()).sum())
    if nan_rows:
        reasons.append(f"nan_price_rows:{nan_rows}")
    ohlc_bad = int(((h < lo) | (h < o) | (h < c) | (lo > o) | (lo > c)).sum())
    if ohlc_bad:
        reasons.append(f"ohlc_invalid_rows:{ohlc_bad}")
    nonpos = int((c <= 0).sum())
    if nonpos:
        reasons.append(f"nonpositive_close_rows:{nonpos}")
    neg_vol = int((v.fillna(0) < 0).sum())
    if neg_vol:
        reasons.append(f"negative_volume_rows:{neg_vol}")
    neg_amt = int((a.fillna(0) < 0).sum())
    if neg_amt:
        reasons.append(f"negative_amount_rows:{neg_amt}")
    bad_dates = int(
        (~dates.astype(str).str.fullmatch(r"(19|20)\d{6}")).sum()
    )
    if bad_dates:
        reasons.append(f"malformed_dates:{bad_dates}")
    return ValidationResult(
        ok=not reasons,
        reasons=reasons,
        row_count=len(df),
        first_date=int(dates.min()),
        last_date=int(dates.max()),
        dup_dates=dups,
        ohlc_invalid=ohlc_bad,
        nonpositive_close=nonpos,
    )


OHLC_REPAIR_RULE_VERSION = "ohlc_envelope_repair_v1"
OHLC_REPAIR_MAX_RATIO = 0.005


def repair_ohlc_envelope(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[int]]:
    """Clamp high/low to the envelope of (open, close) — OHLC_REPAIR_RULE_VERSION.

    Historical vendor rows occasionally report close outside [low, high]
    (e.g. 000015.SZ 19921016 close=17.1 < low=17.35, while the next day's
    pre_close confirms the close). The official close is authoritative for
    backtests, so high/low are widened to contain open and close. Returns
    (repaired copy, list of repaired trade_dates). Callers must enforce
    OHLC_REPAIR_MAX_RATIO and record repairs in provenance.
    """
    out = df.copy()
    o = pd.to_numeric(out["open"], errors="coerce")
    h = pd.to_numeric(out["high"], errors="coerce")
    lo = pd.to_numeric(out["low"], errors="coerce")
    c = pd.to_numeric(out["close"], errors="coerce")
    new_high = pd.concat([h, o, c], axis=1).max(axis=1)
    new_low = pd.concat([lo, o, c], axis=1).min(axis=1)
    changed = (new_high != h) | (new_low != lo)
    repaired_dates = (
        pd.to_numeric(out.loc[changed, "trade_date"]).astype("int64").tolist()
    )
    out["high"] = new_high
    out["low"] = new_low
    return out, repaired_dates


def transform_to_bar_arrays(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """Convert a validated ascending Tushare daily frame to blob arrays.

    Applies UNIT_TRANSFORM_VERSION: vol 手->股 (x100), amount 千元->元 (x1000).
    NaN volume/amount become 0.0 (price NaNs are rejected in validation).
    """
    dates = pd.to_numeric(df["trade_date"]).astype("int64").to_numpy()
    return {
        "trade_date": dates,
        "open": pd.to_numeric(df["open"]).astype("float64").to_numpy(),
        "high": pd.to_numeric(df["high"]).astype("float64").to_numpy(),
        "low": pd.to_numeric(df["low"]).astype("float64").to_numpy(),
        "close": pd.to_numeric(df["close"]).astype("float64").to_numpy(),
        "volume": (
            pd.to_numeric(df["vol"], errors="coerce").fillna(0.0).astype("float64")
            * 100.0
        ).to_numpy(),
        "amount": (
            pd.to_numeric(df["amount"], errors="coerce").fillna(0.0).astype("float64")
            * 1000.0
        ).to_numpy(),
    }


def clip_to_cutoff(df: pd.DataFrame, cutoff: int) -> pd.DataFrame:
    """Drop bars after the dataset cutoff date (inclusive keep)."""
    dates = pd.to_numeric(df["trade_date"]).astype("int64")
    return df[dates <= cutoff].reset_index(drop=True)
