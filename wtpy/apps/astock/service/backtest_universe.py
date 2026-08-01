# -*- coding: utf-8 -*-
"""Universe selection helpers for A-stock backtests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional, Sequence, Union

from ..config import AStockConfig
from ..data.universe import AShareUniverse

def _astock_code_sha() -> str:
    import hashlib

    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    files = sorted(
        [
            p
            for p in root.rglob("*")
            if p.is_file()
            and p.suffix in {".py", ".json"}
            and "__pycache__" not in p.parts
        ],
        key=lambda p: str(p.relative_to(root)).replace("\\", "/"),
    )
    for p in files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


DEMO_CODES = ["SSE.STK.600000", "SZSE.STK.000001"]

FULL_MARKET_TOKENS = frozenset(
    {
        "*",
        "ALL",
        "ALL_A",
        "ALL_MARKET",
        "FULL",
        "FULL_MARKET",
        "全市场",
        "全部A股",
        "全部",
    }
)


def _is_full_market_token(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if t in FULL_MARKET_TOKENS:
        return True
    return t.upper() in {x.upper() for x in FULL_MARKET_TOKENS if x.isascii()}


def select_universe(cfg: AStockConfig, codes: Optional[Union[Sequence[str], str]]) -> List[str]:
    """Resolve stock universe.

    - None / empty / full-market token -> entire universe.json (all A-shares)
    - otherwise parse comma list or sequence of codes
    """
    from ..data.universe import to_std_code

    def _full() -> List[str]:
        if cfg.universe_path.exists():
            return AShareUniverse.load(cfg.universe_path).codes()
        return list(DEMO_CODES)

    if codes is None:
        return _full()
    if isinstance(codes, str):
        parts = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        parts = [str(c).strip() for c in codes if str(c).strip()]
    if not parts:
        return _full()
    if any(_is_full_market_token(c) for c in parts):
        return _full()
    out: List[str] = []
    for c in parts:
        if c.startswith("SSE.") or c.startswith("SZSE.") or c.startswith("BSE."):
            out.append(c)
        else:
            out.append(to_std_code(c))
    return out if out else _full()
