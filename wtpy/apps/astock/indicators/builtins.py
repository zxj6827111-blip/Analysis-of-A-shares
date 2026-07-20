"""Built-in Tongdaxin series functions (vectorized, no eval)."""

from __future__ import annotations

from typing import Callable, Dict

import numpy as np


def _as_float(a) -> np.ndarray:
    return np.asarray(a, dtype=np.float64)


def _as_bool(a) -> np.ndarray:
    x = np.asarray(a)
    if x.dtype == np.bool_:
        return x
    return np.nan_to_num(x.astype(np.float64), nan=0.0) != 0.0


def fn_ma(series, n) -> np.ndarray:
    s = _as_float(series)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full_like(s, np.nan, dtype=np.float64)
    if n <= 0:
        return out
    csum = np.cumsum(np.nan_to_num(s, nan=0.0))
    for i in range(n - 1, len(s)):
        if i - n >= 0:
            total = csum[i] - csum[i - n]
        else:
            total = csum[i]
        window = s[i - n + 1 : i + 1]
        if np.any(np.isnan(window)):
            # still compute on available? TDX typically needs full window
            if np.count_nonzero(~np.isnan(window)) < n:
                out[i] = np.nan
            else:
                out[i] = np.nanmean(window)
        else:
            out[i] = total / n
    return out


def fn_ema(series, n) -> np.ndarray:
    s = _as_float(series)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full_like(s, np.nan, dtype=np.float64)
    if n <= 0 or len(s) == 0:
        return out
    alpha = 2.0 / (n + 1.0)
    # seed with first value
    prev = np.nan
    for i, v in enumerate(s):
        if np.isnan(v):
            out[i] = prev
            continue
        if np.isnan(prev):
            prev = v
            out[i] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[i] = prev
    return out


def fn_ref(series, n) -> np.ndarray:
    s = _as_float(series)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full_like(s, np.nan, dtype=np.float64)
    if n < 0:
        n = abs(n)
    if n == 0:
        return s.copy()
    if n < len(s):
        out[n:] = s[:-n]
    return out


def fn_cross(a, b) -> np.ndarray:
    """CROSS(A,B): A crosses above B — prev A<=B and now A>B."""
    aa = _as_float(a)
    bb = _as_float(b)
    out = np.zeros(len(aa), dtype=np.float64)
    if len(aa) == 0:
        return out
    prev_le = aa[0] <= bb[0]
    for i in range(1, len(aa)):
        now_gt = aa[i] > bb[i]
        prev_le = aa[i - 1] <= bb[i - 1]
        out[i] = 1.0 if (prev_le and now_gt) else 0.0
    return out


def fn_barslast(cond) -> np.ndarray:
    """Bars since last True. If never true, TDX often returns large number / nan.
    We use nan until first true, then 0 on true bar, increasing after.
    """
    c = _as_bool(cond)
    out = np.full(len(c), np.nan, dtype=np.float64)
    last = -1
    for i, v in enumerate(c):
        if v:
            last = i
            out[i] = 0.0
        elif last >= 0:
            out[i] = float(i - last)
    return out


def fn_count(cond, n) -> np.ndarray:
    """COUNT(X,N): number of True values in the last N bars (including current).

    Requires a full N-bar window; earlier bars are NaN (pre-warm).
    """
    c = _as_bool(cond).astype(np.float64)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full(len(c), np.nan, dtype=np.float64)
    if n <= 0:
        return out
    csum = np.cumsum(c)
    for i in range(len(c)):
        if i + 1 < n:
            continue
        if i - n >= 0:
            out[i] = csum[i] - csum[i - n]
        else:
            out[i] = csum[i]
    return out


def fn_abs(series) -> np.ndarray:
    return np.abs(_as_float(series))


def fn_max(a, b) -> np.ndarray:
    return np.maximum(_as_float(a), _as_float(b))


def fn_min(a, b) -> np.ndarray:
    return np.minimum(_as_float(a), _as_float(b))


def fn_hhv(series, n) -> np.ndarray:
    s = _as_float(series)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full_like(s, np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        window = s[lo : i + 1]
        if len(window) >= n:
            out[i] = np.nanmax(window)
    return out


def fn_llv(series, n) -> np.ndarray:
    s = _as_float(series)
    n = int(np.asarray(n).reshape(-1)[0] if np.size(n) else n)
    out = np.full_like(s, np.nan)
    for i in range(len(s)):
        lo = max(0, i - n + 1)
        window = s[lo : i + 1]
        if len(window) >= n:
            out[i] = np.nanmin(window)
    return out


def fn_if(cond, a, b) -> np.ndarray:
    c = _as_bool(cond)
    aa = _as_float(a)
    bb = _as_float(b)
    # broadcast scalars
    if aa.ndim == 0:
        aa = np.full(len(c), float(aa))
    if bb.ndim == 0:
        bb = np.full(len(c), float(bb))
    out = np.where(c, aa, bb).astype(np.float64)
    return out


def fn_not(x) -> np.ndarray:
    return (~_as_bool(x)).astype(np.float64)


BUILTINS: Dict[str, Callable] = {
    "MA": fn_ma,
    "EMA": fn_ema,
    "REF": fn_ref,
    "CROSS": fn_cross,
    "BARSLAST": fn_barslast,
    "COUNT": fn_count,
    "ABS": fn_abs,
    "MAX": fn_max,
    "MIN": fn_min,
    "HHV": fn_hhv,
    "LLV": fn_llv,
    "IF": fn_if,
    "NOT": fn_not,
}


def get_builtin(name: str) -> Callable:
    key = name.upper()
    if key not in BUILTINS:
        raise KeyError(key)
    return BUILTINS[key]
