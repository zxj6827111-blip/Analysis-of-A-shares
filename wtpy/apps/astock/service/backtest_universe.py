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

    overlay_v1 仓库 + 例行链已写 ``eod_universe_latest.json`` 时优先取它：
    名单每周同步刷新（含北交所与 delta-only 新票），TDX 时代的静态
    ``universe.json`` 退为兜底（见 ``_overlay_eod_universe``）。
    """
    from ..data.universe import to_std_code

    def _full() -> List[str]:
        live = _overlay_eod_universe(cfg)
        if live:
            return live
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


# overlay 例行链现役名单快照的最长龄期：EOD 链每周写一次，隔周导出仍沿用；
# 超时视为失联（服务停更/全新部署），回退 universe.json / manifest 扫描。
_OVERLAY_EOD_UNIVERSE_MAX_AGE_DAYS = 14


def _overlay_eod_universe(cfg: AStockConfig) -> List[str]:
    """overlay_v1 现役名单快照（``eod_universe_latest.json``）。

    为什么需要（2026-09-23 北交所接入审查发现）：overlay 架构下导出/复核/
    卦象的票池走 base manifest 票单，delta-only 票（新上市、首批北交所）
    要等下次 consolidation 才可见——而例行 delta 链每次同步后都会重写这份
    快照（``_write_eod_universe_snapshot``），它才是「当下全市场」的权威
    口径。文件缺失/过期/明显不是全市场（防夹具面冒充）时返回空列表，
    调用方回退到原有 manifest/universe.json 路径，绝不静默缩池。
    """
    import json as _json
    import time as _time

    try:
        from ..data.delta_store import load_overlay_state

        if not load_overlay_state(cfg.market_data_root).enabled:
            return []
        path = cfg.market_data_root / "eod_universe_latest.json"
        if not path.exists():
            return []
        raw = _json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("schema") or 0) != 1:
            return []
        fetched = str(raw.get("fetched_at") or "")
        if fetched:
            age = _time.time() - _time.mktime(
                _time.strptime(fetched, "%Y-%m-%d %H:%M:%S")
            )
            if age > _OVERLAY_EOD_UNIVERSE_MAX_AGE_DAYS * 86400:
                return []
        syms = [str(s) for s in (raw.get("symbols") or []) if ".STK." in str(s)]
        if len(syms) < 1000:  # 全市场名单不可能只有几百只，防止夹具面冒充
            return []
        return syms
    except Exception:  # noqa: BLE001 — 任何读取异常回退旧路径
        return []


def _universe_from_data_root(cfg: AStockConfig) -> List[str]:
    """Derive the full-market list from the Tushare raw baseline.

    Tushare-only deployments never produce the TDX-era ``universe.json``;
    without this fallback every "全市场" scope silently degrades to the two
    demo codes (600000/000001). overlay_v1 仓库优先取 EOD 现役名单快照
    （``_overlay_eod_universe``，delta-only 新票不等 consolidation）；
    否则用 raw manifest 基线（content-addressed，快照间不变）。短 TTL
    缓存让导出/同卦扫描的重复调用不必重扫 manifest。无可用基线时返回
    DEMO_CODES。
    """
    live = _overlay_eod_universe(cfg)
    if live:
        return live
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
