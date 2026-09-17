# -*- coding: utf-8 -*-
"""Resolve A-share display names (code6 -> 股票名称).

Sources (cached；前三源为本地导入产物，第四源为纯 Tushare 部署的兜底):
1. Forecast weekly snapshot stocks.jsonl (if active week present)
2. TDX hq_cache/infoharbor_ex.code (GBK pipe file)
3. universe.json SymbolInfo.name (often empty)
4. rizhu_list_dates.json 的 stock_names/etf_names（Tushare stock_basic 缓存，
   只补前面三源缺失的代码）

1)-3) 都是「通达信导入时代」的本地产物：Tushare-only 部署三者全无，名称会
整体为空（跟踪页 L2 名称列全「—」即此因）。第 4 源由 bagua 导出/Namelike
快照链从 Tushare 拉取维护，是本模块在无本地导入产物时唯一的名称来源。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from ..config import AStockConfig
from ..forecast.name_norm import normalize_stock_code
from ..research.fingerprint import short_fingerprint

_lock = threading.Lock()
_cache: Dict[str, str] = {}
_loaded_for: Optional[str] = None  # cache key of sources used

# NAMELIKE 名称快照的时效窗口（天）：只使用仍满足时效的来源；
# 超出窗口且刷新失败的名称不参与计算（缺名称策略：报错可见）。
NAME_FRESH_DAYS = 7


def _clean_name(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    # TDX often pads full-width spaces, e.g. "万  科Ａ"
    s = re.sub(r"[\s\u3000]+", "", s)
    # normalize full-width A
    s = s.replace("Ａ", "A").replace("Ｂ", "B")
    return s


def _load_from_forecast_weekly(
    cfg: AStockConfig, *, max_age_seconds: Optional[float] = None
) -> Dict[str, str]:
    """周报快照名称。``max_age_seconds`` 传入时逐文件判 mtime 时效，过期
    快照文件不参与合并（默认 None = 不过滤，通用名称解析路径行为不变）。"""
    out: Dict[str, str] = {}
    weekly = getattr(cfg, "forecast_weekly_dir", None)
    froot = getattr(cfg, "forecast_root", None)
    root = Path(weekly or (Path(froot or "") / "weekly"))
    if not root.exists():
        return out
    index_path = root / "index.json"
    week_key = None
    if index_path.exists():
        try:
            idx = json.loads(index_path.read_text(encoding="utf-8"))
            week_key = idx.get("active_week_key")
            if not week_key:
                weeks = idx.get("weeks") or {}
                if isinstance(weeks, dict) and weeks:
                    week_key = sorted(weeks.keys())[-1]
                elif isinstance(weeks, list) and weeks:
                    keys = [
                        str(w.get("week_key"))
                        for w in weeks
                        if isinstance(w, dict) and w.get("week_key")
                    ]
                    week_key = sorted(keys)[-1] if keys else None
        except Exception:
            week_key = None
    snap = root / "snapshots"
    candidates = []
    if week_key:
        candidates.append(snap / str(week_key) / "stocks.jsonl")
        candidates.append(snap / str(week_key) / "etfs.jsonl")
    if snap.exists():
        for d in sorted(snap.iterdir(), reverse=True):
            if d.is_dir():
                candidates.append(d / "stocks.jsonl")
                candidates.append(d / "etfs.jsonl")
    seen = set()
    for path in candidates:
        path = Path(path)
        if path in seen or not path.exists():
            continue
        seen.add(path)
        # 逐文件时效：候选序列与通用路径一致（active week 优先、再按目录名
        # 从新到旧），过期文件跳过——新鲜的靠后候选仍能补上，不会整源被
        # 单个过期文件拖死，也不会把过期快照里的旧名混入 NAMELIKE 快照
        if max_age_seconds is not None and _path_fresh_seconds(path) > max_age_seconds:
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    code = normalize_stock_code(row.get("code6") or row.get("code"))
                    name = _clean_name(row.get("name") or "")
                    if code and name and code not in out:
                        out[code] = name
        except Exception:
            continue
    return out


def _load_from_tdx_infoharbor(
    cfg: AStockConfig, *, max_age_seconds: Optional[float] = None
) -> Dict[str, str]:
    """TDX infoharbor 名称。``max_age_seconds`` 传入时逐文件判 mtime 时效，
    过期文件直接跳过（默认 None = 不过滤，通用名称解析路径行为不变）。"""
    out: Dict[str, str] = {}
    tdx_root = getattr(cfg, "tdx_root", None)
    tdx = Path(tdx_root) if tdx_root else None
    if not tdx:
        return out
    for path in _tdx_infoharbor_candidates(tdx):
        # 逐文件时效：原逻辑即「首个产出名称的文件生效」，叠加过滤后等价于
        # 「首个新鲜且产出的文件生效」；过期文件里的名称不得参与 NAMELIKE 快照
        if max_age_seconds is not None and _path_fresh_seconds(path) > max_age_seconds:
            continue
        try:
            text = path.read_text(encoding="gbk", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            # formats:
            # 000001|平安银行|...
            # 0|001248|华润新能 (infoharbor_ex.name)
            if len(parts) >= 3 and re.fullmatch(r"\d", parts[0] or ""):
                code = normalize_stock_code(parts[1])
                name = _clean_name(parts[2])
            else:
                code = normalize_stock_code(parts[0])
                name = _clean_name(parts[1] if len(parts) > 1 else "")
            if code and name and code not in out:
                out[code] = name
        if out:
            break
    return out


def _tdx_infoharbor_candidates(tdx: Path) -> list:
    return [
        tdx / "T0002" / "hq_cache" / "infoharbor_ex.code",
        tdx / "hq_cache" / "infoharbor_ex.code",
        tdx / "T0002" / "hq_cache" / "infoharbor_ex.name",
    ]


def _load_from_universe(
    cfg: AStockConfig, *, max_age_seconds: Optional[float] = None
) -> Dict[str, str]:
    """universe.json 名称。``max_age_seconds`` 传入时过期文件整体不参与
    （默认 None = 不过滤，通用名称解析路径行为不变）。"""
    out: Dict[str, str] = {}
    path = getattr(cfg, "universe_path", None)
    if path is None:
        path = Path(cfg.storage_root) / "universe.json"
    path = Path(path)
    if not path.exists():
        return out
    if max_age_seconds is not None and _path_fresh_seconds(path) > max_age_seconds:
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    for s in data.get("symbols") or []:
        if not isinstance(s, dict):
            continue
        code = normalize_stock_code(s.get("code") or s.get("std_code") or s.get("raw"))
        name = _clean_name(s.get("name") or "")
        if code and name:
            out[code] = name
    return out


def _symbol_meta_cache_path(cfg: AStockConfig) -> Path:
    """Tushare 元数据缓存（rizhu_list_dates.json）路径。

    与 bagua_query 的日柱/名称兜底共用同一份文件（同一 storage_root 下），
    保证「导出侧写入的缓存」与「本模块读取的缓存」不会各自指向不同文件。
    """
    return Path(cfg.storage_root) / "rizhu_list_dates.json"


def _load_from_symbol_meta(cfg: AStockConfig) -> Dict[str, str]:
    """Tushare 元数据缓存名称（code6 -> 名称）。

    Tushare-only 部署（无 TDX infoharbor、无 universe.json、无周报快照）下
    唯一的本地名称源：由导出/NAMELIKE 快照链的 ``ensure_name_coverage`` /
    ``ensure_fresh_symbol_names`` 从 Tushare ``stock_basic`` 拉取后落盘。

    **只读本地文件、绝不联网**：本函数会被跟踪页 L2 读取路径调用，联网会把
    「读一次表格」变成网络等待（首屏性能约束）。缓存缺失/损坏返回 {}——
    缺名如实留空，绝不拿代码冒充名称。
    """
    path = _symbol_meta_cache_path(cfg)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    # stock_names 先于 etf_names：指数与股票代码段存在重叠（000001 既是上证
    # 指数也是平安银行），股票口径优先，与 bagua 侧按 kind 过滤的取舍一致。
    for section in ("stock_names", "etf_names"):
        rows = data.get(section)
        if not isinstance(rows, dict):
            continue
        for k, v in rows.items():
            code = normalize_stock_code(k)
            name = _clean_name(v)
            if code and name and code not in out:
                out[code] = name
    return out


def _source_fingerprint(cfg: AStockConfig) -> str:
    parts = []
    weekly = getattr(cfg, "forecast_weekly_dir", None) or (
        Path(getattr(cfg, "forecast_root", "") or "") / "weekly"
    )
    tdx = getattr(cfg, "tdx_root", None) or ""
    storage = getattr(cfg, "storage_root", None) or ""
    for p in (
        Path(weekly or "") / "index.json",
        Path(tdx) / "T0002" / "hq_cache" / "infoharbor_ex.code",
        Path(storage) / "universe.json",
        _symbol_meta_cache_path(cfg),
    ):
        try:
            if p.exists():
                st = p.stat()
                parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except Exception:
            continue
    return "|".join(parts) or "empty"


def ensure_name_cache(cfg: AStockConfig, *, force: bool = False) -> Dict[str, str]:
    global _cache, _loaded_for
    fp = _source_fingerprint(cfg)
    with _lock:
        if not force and _loaded_for == fp and _cache:
            return _cache
        merged: Dict[str, str] = {}
        # Prefer TDX full list, then overlay forecast (usually fresher names)
        for loader in (_load_from_tdx_infoharbor, _load_from_forecast_weekly, _load_from_universe):
            try:
                chunk = loader(cfg)
            except Exception:
                chunk = {}
            for k, v in chunk.items():
                if k and v:
                    merged[k] = v
        # Tushare 元数据缓存兜底：**只补缺口**，不覆盖上面三源。有本地导入
        # 产物时口径与从前完全一致（Tushare 名不参与覆盖）；无产物时它就是
        # 唯一来源，避免 Tushare-only 部署名称整体为空。
        try:
            for k, v in _load_from_symbol_meta(cfg).items():
                if k and v and k not in merged:
                    merged[k] = v
        except Exception:
            pass
        _cache = merged
        _loaded_for = fp
        return _cache


def resolve_stock_name(
    cfg: AStockConfig,
    code: str,
    *,
    std_code: str = "",
) -> str:
    """Return Chinese stock name for code, or empty string."""
    code6 = normalize_stock_code(code or std_code)
    if not code6 and std_code:
        code6 = normalize_stock_code(std_code.split(".")[-1])
    if not code6:
        return ""
    cache = ensure_name_cache(cfg)
    return cache.get(code6) or ""


def display_code_with_name(code: str, name: str) -> str:
    code = (code or "").strip()
    name = (name or "").strip()
    if code and name:
        return f"{code} {name}"
    return code or name or ""


def fill_missing_names(
    cfg: AStockConfig,
    rows,
    *,
    code_key: str = "code",
    name_key: str = "name",
) -> int:
    """把 ``name`` 为空的记录按当前名称源补齐展示名（原地修改），返回补齐条数。

    用途：**不可变产物**不做回写，但历史产物的 name 可能整列为空——结算发生在
    缺本地导入产物（无 TDX / 无 universe.json / 无周报快照）的部署上时，名称源
    全缺、name 被写成 ""。展示层（跟踪页 L2）与导出层在读产物时用它补展示名，
    两处共用同一口径，避免「页面有名字、导出没有」。

    来源与结算层同一函数链（见 :func:`resolve_stock_name`），一次加载整表后查
    字典，避免逐票重复做来源指纹 stat。补不到保持原样，**绝不拿代码冒充名称**。
    """
    targets = [
        r for r in (rows or ()) if isinstance(r, dict) and not r.get(name_key)
    ]
    if not targets:
        return 0
    cache = ensure_name_cache(cfg)
    if not cache:
        return 0
    filled = 0
    for r in targets:
        raw = str(r.get(code_key) or "")
        if not raw:
            continue
        # 产物 code 可能是 SSE.STK.600033 / SZSE.000003.SZ 两种形态
        code6 = normalize_stock_code(raw) or normalize_stock_code(raw.split(".")[-1])
        name = (cache.get(code6) or "") if code6 else ""
        if name:
            r[name_key] = name
            filled += 1
    return filled


def _formula_uses_namelike(spec) -> bool:
    text = getattr(spec, "formula_text", "") or ""
    return bool(re.search(r"NAMELIKE", text, flags=re.IGNORECASE))


def _path_fresh_seconds(path) -> float:
    """返回文件距现在的秒数；不存在返回 inf（视为过期）。"""
    try:
        return time.time() - Path(path).stat().st_mtime
    except OSError:
        return float("inf")


def ensure_stock_names_for(
    cfg: AStockConfig,
    codes: Sequence[str],
    specs: Sequence,
) -> Tuple[Dict[str, str], str]:
    """为含 NAMELIKE 的规则解析 code6 -> 名称快照。返回 (name_map, snapshot_id)。

    - **任一** spec 的公式含 NAMELIKE 才解析（`any`，不是 all——混合规则
      不得跳过名称加载）；全部不含时返回 ({}, "")，非 NAMELIKE 回测零开销、
      信号缓存键不变。
    - 来源优先级（后者覆盖前者）：universe.json < 周报快照 < TDX infoharbor
      < Tushare 元数据缓存（per-code 时效）。本地来源在加载器内部**逐文件**
      判 mtime 时效（候选序列与通用名称解析路径完全一致，门与加载零口径差），
      过期文件不参与合并；Tushare 缓存过期时触发强制刷新，刷新失败则过期
      名称不参与返回——调用方按缺名称策略报错可见，绝不静默用旧名。
    - snapshot_id = 名称**内容**指纹（本股票池 code->name 排序哈希），不含
      mtime/fetched_at：仅刷新时间变化而名称未变时，昂贵信号缓存不失效。
    """
    if not specs or not any(_formula_uses_namelike(s) for s in specs):
        return {}, ""

    needed = sorted({normalize_stock_code(c) for c in codes if normalize_stock_code(c)})

    merged: Dict[str, str] = {}
    max_age = NAME_FRESH_DAYS * 86400.0

    # 1)-3) universe.json → 周报快照 → TDX infoharbor：逐文件 mtime 时效过滤
    # 由加载器内部完成（候选序列与通用路径同一套代码），过期来源的 code 留给
    # Tushare per-code 层兜底，兜不住则缺名称报错可见
    for k, v in _load_from_universe(cfg, max_age_seconds=max_age).items():
        merged[k] = v
    for k, v in _load_from_forecast_weekly(cfg, max_age_seconds=max_age).items():
        merged[k] = v
    for k, v in _load_from_tdx_infoharbor(cfg, max_age_seconds=max_age).items():
        merged[k] = v

    # 4) Tushare 元数据缓存（per-code 时效；延迟导入避免与 bagua_query 循环依赖）
    from .bagua_query import ensure_fresh_symbol_names

    fresh_names, _ages = ensure_fresh_symbol_names(
        cfg, needed, max_age_days=NAME_FRESH_DAYS
    )
    for k, v in fresh_names.items():
        merged[k] = v

    pool_map = {c: merged[c] for c in needed if c in merged}
    snapshot_id = short_fingerprint({"names": sorted(pool_map.items())}, n=16)
    return pool_map, snapshot_id
