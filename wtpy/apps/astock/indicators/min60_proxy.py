"""Research proxy: map #MIN60 MACD cross-period refs onto day-line MACD.

This is **not** true 60-minute history. Formulas that reference
``"MACD.DIF#MIN60"`` / ``"MACD.DEA#MIN60"`` can still run for research
backtests by substituting the same-bar daily DIF/DEA series.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from .builtins import fn_ema


def day_macd_dif_dea(
    close: np.ndarray,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray]:
    """Standard MACD DIF/DEA on a close series (TDX defaults)."""
    c = np.asarray(close, dtype=np.float64)
    dif = fn_ema(c, fast) - fn_ema(c, slow)
    dea = fn_ema(dif, signal)
    return dif, dea


def build_min60_day_proxy_cross(
    bars: Dict[str, np.ndarray],
) -> Dict[str, np.ndarray]:
    """Build cross_period_data keys expected by runtime for MIN60 MACD refs."""
    close = bars.get("close")
    if close is None and "CLOSE" in bars:
        close = bars["CLOSE"]
    if close is None:
        raise ValueError("bars must include close for MIN60 day proxy")
    dif, dea = day_macd_dif_dea(np.asarray(close, dtype=np.float64))
    # runtime looks up node.raw.upper() and FIELD#PERIOD variants
    return {
        "MACD.DIF#MIN60": dif,
        "MACD.DEA#MIN60": dea,
        "DIF#MIN60": dif,
        "DEA#MIN60": dea,
    }


def merge_cross_period(
    base: Optional[Dict[str, np.ndarray]],
    extra: Optional[Dict[str, np.ndarray]],
) -> Dict[str, np.ndarray]:
    out: Dict[str, np.ndarray] = {}
    if base:
        out.update(base)
    if extra:
        out.update(extra)
    return out
