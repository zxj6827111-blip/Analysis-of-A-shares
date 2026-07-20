"""Tongdaxin .lc1 minute file probe (read-only).

Record layout commonly used by TDX 1-minute files: 32-byte little-endian records.
This module only measures coverage; it does not enable formal MIN60 backtests.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, Optional, Tuple


LC1_FMT = "<HHffffll"  # date, time, open, high, low, close, amount, volume ? common layout
# Note: real TDX layouts vary; we only use size-based coverage metrics for status.


def lc1_record_count(path: Path) -> Optional[int]:
    path = Path(path)
    if not path.exists():
        return None
    sz = path.stat().st_size
    if sz % 32 != 0:
        return None
    return sz // 32


def probe_symbol_minline(tdx_root: Path, raw_code: str) -> Dict:
    """raw_code like sh600000."""
    tdx_root = Path(tdx_root)
    exch = raw_code[:2]
    path = tdx_root / "vipdoc" / exch / "minline" / f"{raw_code}.lc1"
    n = lc1_record_count(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "n_records": n,
        "approx_trading_days_if_240bars": (n // 240) if n else None,
    }
