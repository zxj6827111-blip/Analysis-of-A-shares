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

# universe.json 是 TDX 导入时代的产物；Tushare-only 新部署永远不会生成它，
# 缺失时回退到 2 只演示代码会让"导出全市场/同卦扫描"只剩 600000/000001。
# 兜底：从数据根的 tushare raw manifest 推导全市场（60s TTL 缓存）。
_universe_fallback_cache: dict = {"key": None, "ts": 0.0, "codes": None}
_UNIVERSE_FALLBACK_TTL = 60.0

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
        return _universe_from_data_root(cfg)

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


def _universe_from_data_root(cfg: AStockConfig) -> List[str]:
    """Derive the full-market list from the Tushare raw baseline.

    Tushare-only deployments never produce the TDX-era ``universe.json``;
    without this fallback every "全市场" scope silently degrades to the two
    demo codes (600000/000001). The raw manifest is content-addressed, so the
    symbol set only changes when a new baseline is published; a short TTL
    cache keeps repeated calls (export / same-gua scans) off the manifest
    walk. Returns DEMO_CODES when the data root has no usable baseline.
    """
    import time as _time

    try:
        key = str(cfg.market_data_root.resolve())
        cache = _universe_fallback_cache
        now = _time.time()
        if cache["key"] == key and now - cache["ts"] < _UNIVERSE_FALLBACK_TTL:
            return list(cache["codes"])
    except Exception:
        key = ""
        cache = {}
        now = 0.0
    try:
        from ..data.dataset_store import DatasetStore

        store = DatasetStore(cfg.market_data_root)
        best = None
        for mid in store.list_manifests():
            m = store.load_manifest(mid)
            if not m or m.source != "tushare" or (m.adjustment or "") != "none":
                continue
            if (m.period or "1d") != "1d" or m.status != "ready":
                continue
            if (m.universe_type or "").startswith("b1_delisted"):
                # 退市池也是 tushare/none/1d/ready，但只是补充面，不是全市场
                continue
            if not any(".STK." in (s.symbol or "") for s in m.symbols):
                # 纯 ETF/指数数据集与股票共用 tushare/none/1d scope，但不是
                # 全市场股票基线（否则 ETF 增量同步比股票新一天会污染 universe）
                continue
            if best is None or int(m.data_cutoff_date or 0) > int(
                best.data_cutoff_date or 0
            ):
                best = m
        if best is not None:
            syms = sorted(
                {
                    s.symbol
                    for s in best.symbols
                    if s.quality == "ok" and s.blob_sha256
                }
            )
            if syms:
                if cache:
                    cache["key"] = key
                    cache["ts"] = now
                    cache["codes"] = list(syms)
                return syms
    except Exception:
        pass
    return list(DEMO_CODES)
