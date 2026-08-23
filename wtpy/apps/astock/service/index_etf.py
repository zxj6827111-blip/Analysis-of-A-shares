# -*- coding: utf-8 -*-
"""Index (沪深指数) and ETF symbol support for 卦象查询.

Handles symbol classification, standard-code normalization, preset
watchlists, display-name resolution, and TDX local day-file loading for
symbols that are NOT part of the A-share stock warehouse:

  - SSE indices   : sh000xxx (上证指数 / 沪深300 / ...) -> SSE.IDX.*
  - SZSE indices  : sz399xxx (深证成指 / 创业板指 / ...) -> SZSE.IDX.*
  - SH ETFs       : sh51/56/58xxxx -> SSE.ETF.*
  - SZ ETFs/LOF   : sz15/16/18xxxx -> SZSE.ETF.*

Indices have no 复权 concept and ETF factor data is not stored in the
warehouse, so both always use unadjusted (raw) prices from TDX day files.
"""

from __future__ import annotations

import re
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AStockConfig
from ..data.tdx_reader import DayBar, TdxDayReader
from ..data.tushare_constituents import TushareConstituentsError

# ---------------------------------------------------------------------------
# Preset watchlists
# ---------------------------------------------------------------------------

# 指数跟踪沪深主要指数（上证系 / 深证系 / 跨市场）
INDEX_WATCHLIST: List[Dict[str, str]] = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sh000300", "name": "沪深300"},
    {"code": "sz399300", "name": "沪深300(深)"},
    {"code": "sh000016", "name": "上证50"},
    {"code": "sh000905", "name": "中证500"},
    {"code": "sh000852", "name": "中证1000"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000688", "name": "科创50"},
]

# 常见 ETF（含交易所内/跨市场主要宽基）
ETF_WATCHLIST: List[Dict[str, str]] = [
    {"code": "sh510050", "name": "上证50ETF"},
    {"code": "sh510300", "name": "沪深300ETF"},
    {"code": "sz159919", "name": "沪深300ETF(深)"},
    {"code": "sh510500", "name": "中证500ETF"},
    {"code": "sh512100", "name": "中证1000ETF"},
    {"code": "sz159901", "name": "深100ETF"},
    {"code": "sz159915", "name": "创业板ETF"},
    {"code": "sh588000", "name": "科创50ETF"},
    {"code": "sz159949", "name": "创业板50ETF"},
    {"code": "sh510880", "name": "红利ETF"},
]

# Bare-6-digit fallback names for the preset watchlists (code -> name).
_WATCHLIST_NAME_MAP: Dict[str, str] = {
    item["code"]: item["name"]
    for item in INDEX_WATCHLIST + ETF_WATCHLIST
}

# Extra well-known names beyond presets (raw -> name).
_EXTRA_NAMES: Dict[str, str] = {
    "sh000002": "A股指数",
    "sh000010": "上证180",
    "sh000905": "中证500",
    "sz399002": "成份A指",
    "sz399005": "中小板指",
    "sz399300": "沪深300(深)",
}

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_SSE_INDEX_RE = re.compile(r"^(?:sh|SSE\.IDX\.)000\d{3}$")
_SZSE_INDEX_RE = re.compile(r"^(?:sz|SZSE\.IDX\.)399\d{3}$")
_SH_ETF_RE = re.compile(r"^(?:sh|SSE\.ETF\.)(?:(?:51|52|56|58)\d{4}|(?:530|551)\d{3})$")
_SZ_ETF_RE = re.compile(r"^(?:sz|SZSE\.ETF\.)(?:158|159)\d{3}$")

# Dotted ts_code-style forms: 000001.SH (上证指数), 399001.SZ, 510300.SH, 159915.SZ.
# Unlike bare 000xxx (which stays a stock — SZSE 000001 平安银行), the dotted
# form is unambiguous because the exchange suffix is explicit.
_SSE_INDEX_DOTTED_RE = re.compile(r"^000\d{3}\.sh$")
_SZSE_INDEX_DOTTED_RE = re.compile(r"^399\d{3}\.sz$")
_SH_ETF_DOTTED_RE = re.compile(r"^(?:(?:51|52|56|58)\d{4}|(?:530|551)\d{3})\.sh$")
_SZ_ETF_DOTTED_RE = re.compile(r"^(?:158|159)\d{3}\.sz$")

# Bare 6-digit codes that cannot be A-share stocks (unambiguous segments):
# 399xxx = SZSE index, 51/52/530/551/56/58xxxx = SH ETF, 158/159xxx = SZ ETF.
_BARE_INDEX_RE = re.compile(r"^399\d{3}$")
_BARE_SH_ETF_RE = re.compile(r"^(?:(?:51|52|56|58)\d{4}|(?:530|551)\d{3})$")
_BARE_SZ_ETF_RE = re.compile(r"^(?:158|159)\d{3}$")


def classify_symbol(raw: str) -> str:
    """Return 'index' | 'etf' | 'stock' | '' for a raw query code.

    Bare 6-digit codes only become index/ETF when unambiguous
    (399xxx / 51/52/56/58xxxx / 158/159xxx). Bare 000xxx stays a stock
    (SZSE 000001 平安银行), so SSE indices must carry the sh prefix.
    """
    t = str(raw or "").strip().lower()
    if not t:
        return ""
    t = t.split()[0].split("\u3000")[0]
    up = t.upper()
    if up.startswith(("SSE.", "SZSE.")):
        if up.startswith(("SSE.STK.", "SZSE.STK.")):
            return ""
        return "index" if ".IDX." in up else ("etf" if ".ETF." in up else "")
    if _SSE_INDEX_RE.match(t) or _SZSE_INDEX_RE.match(t):
        return "index"
    if _SH_ETF_RE.match(t) or _SZ_ETF_RE.match(t):
        return "etf"
    if _SSE_INDEX_DOTTED_RE.match(t) or _SZSE_INDEX_DOTTED_RE.match(t):
        return "index"
    if _SH_ETF_DOTTED_RE.match(t) or _SZ_ETF_DOTTED_RE.match(t):
        return "etf"
    if _BARE_INDEX_RE.match(t) or _BARE_SH_ETF_RE.match(t) or _BARE_SZ_ETF_RE.match(t):
        return "index" if t.startswith("399") else "etf"
    return ""


def to_index_etf_std_code(raw: str) -> str:
    """Normalize index/ETF raw code to canonical std code, or '' if stock.

    sh000001 -> SSE.IDX.000001, sz399001 -> SZSE.IDX.399001,
    sh510300 -> SSE.ETF.510300, sz159915 -> SZSE.ETF.159915.
    """
    t = str(raw or "").strip().lower()
    if not t:
        return ""
    t = t.split()[0].split("\u3000")[0]
    up = t.upper()
    if up.startswith("SSE.IDX.") or up.startswith("SZSE.IDX."):
        return up
    if up.startswith("SSE.ETF.") or up.startswith("SZSE.ETF."):
        return up
    if t.startswith("sse.stk.") or t.startswith("szse.stk."):
        return ""
    if _SSE_INDEX_RE.match(t) or _SZSE_INDEX_RE.match(t):
        exch = "SSE" if t.startswith("sh") or t.startswith("SSE.") else "SZSE"
        return f"{exch}.IDX.{t[2:]}" if len(t) > 2 and t[2:].isdigit() else t.upper()
    if _SH_ETF_RE.match(t) or _SZ_ETF_RE.match(t):
        exch = "SSE" if t.startswith("sh") or t.startswith("SSE.") else "SZSE"
        return f"{exch}.ETF.{t[2:]}" if len(t) > 2 and t[2:].isdigit() else t.upper()
    if _SSE_INDEX_DOTTED_RE.match(t):
        return f"SSE.IDX.{t[:6]}"
    if _SZSE_INDEX_DOTTED_RE.match(t):
        return f"SZSE.IDX.{t[:6]}"
    if _SH_ETF_DOTTED_RE.match(t):
        return f"SSE.ETF.{t[:6]}"
    if _SZ_ETF_DOTTED_RE.match(t):
        return f"SZSE.ETF.{t[:6]}"
    if _BARE_INDEX_RE.match(t):
        return f"SZSE.IDX.{t}"
    if _BARE_SH_ETF_RE.match(t):
        return f"SSE.ETF.{t}"
    if _BARE_SZ_ETF_RE.match(t):
        return f"SZSE.ETF.{t}"
    return ""


def display_code(std_code: str) -> str:
    """SSE.IDX.000001 -> sh000001, SZSE.ETF.159915 -> sz159915."""
    if std_code.startswith("SSE.IDX."):
        return "sh" + std_code.split(".")[-1]
    if std_code.startswith("SZSE.IDX."):
        return "sz" + std_code.split(".")[-1]
    if std_code.startswith("SSE.ETF."):
        return "sh" + std_code.split(".")[-1]
    if std_code.startswith("SZSE.ETF."):
        return "sz" + std_code.split(".")[-1]
    return std_code


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def resolve_index_etf_name(std_code: str) -> str:
    """Return Chinese name for index/ETF std code, or empty string."""
    disp = display_code(std_code)
    if disp in _WATCHLIST_NAME_MAP:
        return _WATCHLIST_NAME_MAP[disp]
    if disp in _EXTRA_NAMES:
        return _EXTRA_NAMES[disp]
    return ""


# ---------------------------------------------------------------------------
# TDX day-file loading
# ---------------------------------------------------------------------------

def load_index_etf_day_bars(
    cfg: AStockConfig,
    std_code: str,
    *,
    tdx_root: Optional[Path] = None,
) -> List[DayBar]:
    """Load unadjusted day bars for an index/ETF from TDX local files.

    Indices live in the same vipdoc/{sh,sz}/lday/ directory as stocks
    (e.g. sh000001.day), so TdxDayReader resolves them directly.
    """
    root = Path(tdx_root) if tdx_root is not None else Path(cfg.tdx_root)
    reader = TdxDayReader(root)
    raw = display_code(std_code)
    bars, _ = reader.read(raw)
    if not bars:
        raise FileNotFoundError(f"no bars for {display_code(std_code)}")
    return list(bars)


def list_etf_std_codes(cfg: AStockConfig) -> List[str]:
    """枚举通达信本地 day 目录中的全部 ETF 标准代码（SSE.ETF.* / SZSE.ETF.*）。

    经 to_index_etf_std_code 识别后仅保留 .ETF. 段（沪 51/52/56/58、
    深 158/159），排除指数（SSE.IDX.* / SZSE.IDX.*）与股票；
    目录缺失或为空时返回空列表。
    """
    root = Path(cfg.tdx_root)
    out: List[str] = []
    for ex, pat in (("sh", "sh*.day"), ("sz", "sz*.day")):
        d = root / "vipdoc" / ex / "lday"
        if not d.is_dir():
            continue
        try:
            names = [p.name[:-4] for p in d.glob(pat)]
        except OSError:
            continue
        for n in names:
            std = to_index_etf_std_code(n)
            if std and ".ETF." in std:
                out.append(std)
    return sorted(set(out))


def _load_warehouse_datasets(cfg: AStockConfig):
    """Load warehouse none/1d manifests once for availability checks.

    Returns (repo, [manifests]); repo is None when the warehouse is absent.
    """
    md_root = getattr(cfg, "market_data_root", None)
    if not md_root or not Path(md_root).exists():
        return None, []
    try:
        from ..data.repository import MarketDataRepository

        repo = MarketDataRepository.from_root(md_root)
        # Read-only availability lookups (see _availability_from_warehouse):
        # skip per-manifest deepcopy (~100k symbol records each on large
        # warehouses).
        return repo, [m for m in repo.list_datasets(adjustment="none", period="1d", deep_copy=False)]
    except Exception:
        return None, []


def _availability_from_warehouse(
    repo, datasets, std_code: str
) -> Tuple[bool, Optional[int]]:
    """Look up a std code in pre-loaded warehouse manifests."""
    if not datasets or repo is None:
        return False, None
    for m in datasets:
        rec = repo._find_symbol_record(m, std_code)
        if rec is None or not getattr(rec, "blob_sha256", None):
            continue
        # manifest first/last may be stored descending (Tushare
        # returns newest-first); take the true latest date.
        d0 = int(rec.first_date) if rec.first_date else None
        d1 = int(rec.last_date) if rec.last_date else None
        if d0 is not None and d1 is not None:
            last = max(d0, d1)
        else:
            last = d1 if d1 is not None else d0
        return True, last
    return False, None


def check_availability(cfg: AStockConfig, code: str) -> Tuple[bool, Optional[int]]:
    """Check data availability; return (available, last_date).

    Warehouse datasets (tushare/none etc.) are checked first — mirroring
    query behavior — then TDX day files as fallback.
    """
    std_code = to_index_etf_std_code(code)
    if std_code:
        repo, datasets = _load_warehouse_datasets(cfg)
        ok, last = _availability_from_warehouse(repo, datasets, std_code)
        if ok:
            return True, last
    return check_tdx_availability(cfg, code)


def check_tdx_availability(cfg: AStockConfig, code: str) -> Tuple[bool, Optional[int]]:
    """Check whether a TDX day file exists; return (available, last_date)."""
    raw = str(code or "").strip().lower()
    if not raw or not raw.startswith(("sh", "sz")):
        return False, None
    root = Path(cfg.tdx_root)
    path = root / "vipdoc" / raw[:2] / "lday" / f"{raw}.day"
    if not path.exists():
        return False, None
    try:
        bars, _ = TdxDayReader(root).read(raw)
        last = int(bars[-1].date) if bars else None
    except Exception:
        last = None
    return True, last


def watchlist(
    cfg: AStockConfig,
    *,
    kind: str = "all",
    include_availability: bool = True,
) -> List[Dict[str, Any]]:
    """Return preset watchlist entries with availability/coverage info.

    kind: all | index | etf
    """
    k = (kind or "all").strip().lower()
    if k not in ("all", "index", "etf"):
        raise ValueError("kind must be all | index | etf")
    repo, datasets = (None, [])
    if include_availability:
        repo, datasets = _load_warehouse_datasets(cfg)
    items: List[Dict[str, Any]] = []
    for raw in INDEX_WATCHLIST + ETF_WATCHLIST:
        stype = classify_symbol(raw["code"])
        if k != "all" and stype != k:
            continue
        entry: Dict[str, Any] = {
            "code": raw["code"],
            "name": raw["name"],
            "type": stype,
            "std_code": to_index_etf_std_code(raw["code"]),
        }
        if include_availability:
            available, last_date = _availability_from_warehouse(repo, datasets, entry["std_code"])
            if not available:
                available, last_date = check_tdx_availability(cfg, raw["code"])
            entry["available"] = available
            entry["last_date"] = last_date
        items.append(entry)
    return items


# ---------------------------------------------------------------------------
# Constituents (成分股) via TDX local files
#
# ETF -> tracked index:  T0002/hq_cache/specetfdata.txt
#   line format: {mkt},{etf_code6},{tracked_index_code6},...
# index -> constituent stocks: T0002/hq_cache/infoharbor_block.dat
#   blocks named ZS_{index_name}; each member line "{mkt}#{code6}"
#   where mkt: 0 = SZSE, 1 = SSE.
# ---------------------------------------------------------------------------

# index code6 -> Chinese short name (matches ZS_{name} blocks)
_INDEX_CODE_TO_NAME: Dict[str, str] = {
    "000001": "上证指数",
    "000002": "A股指数",
    "000015": "上证红利",
    "000016": "上证50",
    "000300": "沪深300",
    "000688": "科创50",
    "000852": "中证1000",
    "000905": "中证500",
    "000922": "中证红利",
    "399001": "深证成指",
    "399006": "创业板指",
    "399300": "沪深300",
    "399330": "深证100",
    "399673": "创业板50",
}

_BLOCK_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "blocks": None}
_BLOCK_LOCK = threading.Lock()
_SPEC_ETF_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "map": None}


def _tdx_hq_cache(cfg: AStockConfig) -> Path:
    return Path(cfg.tdx_root) / "T0002" / "hq_cache"


def load_spec_etf_map(cfg: AStockConfig) -> Dict[str, str]:
    """Parse ETF -> tracked index code6 map from specetfdata.txt."""
    global _SPEC_ETF_CACHE
    path = _tdx_hq_cache(cfg) / "specetfdata.txt"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    if (
        _SPEC_ETF_CACHE.get("path") == str(path)
        and _SPEC_ETF_CACHE.get("mtime") == mtime
        and _SPEC_ETF_CACHE.get("map") is not None
    ):
        return dict(_SPEC_ETF_CACHE["map"])
    out: Dict[str, str] = {}
    try:
        text = path.read_bytes().decode("gbk", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split(",")
        if (
            len(parts) >= 3
            and parts[0] in ("0", "1")
            and parts[1].isdigit()
            and len(parts[1]) == 6
            and parts[2].isdigit()
        ):
            out[parts[1]] = parts[2].zfill(6)
    _SPEC_ETF_CACHE["path"] = str(path)
    _SPEC_ETF_CACHE["mtime"] = mtime
    _SPEC_ETF_CACHE["map"] = out
    return dict(out)


def load_block_constituents(cfg: AStockConfig) -> Dict[str, List[Tuple[int, str]]]:
    """Parse ZS_* index constituent blocks -> {index_name: [(mkt, code6)]}.

    mkt: 0 = SZSE, 1 = SSE.
    """
    global _BLOCK_CACHE
    path = _tdx_hq_cache(cfg) / "infoharbor_block.dat"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _BLOCK_LOCK:
        if (
            _BLOCK_CACHE.get("path") == str(path)
            and _BLOCK_CACHE.get("mtime") == mtime
            and _BLOCK_CACHE.get("blocks") is not None
        ):
            return dict(_BLOCK_CACHE["blocks"])
        out: Dict[str, List[Tuple[int, str]]] = {}
        try:
            text = path.read_bytes().decode("gbk", errors="ignore")
        except OSError:
            return out
        cur_name: Optional[str] = None
        cur_codes: List[Tuple[int, str]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if cur_name:
                    out[cur_name] = cur_codes
                header = line[1:].split(",")[0].strip()
                cur_name = header[3:] if header.startswith("ZS_") else ""
                cur_codes = []
            elif cur_name:
                for tok in line.split(","):
                    tok = tok.strip()
                    if not tok or "#" not in tok:
                        continue
                    mkt_s, _, code6 = tok.partition("#")
                    if not mkt_s.isdigit() or not (code6.isdigit() and len(code6) == 6):
                        continue
                    cur_codes.append((int(mkt_s), code6))
        if cur_name:
            out[cur_name] = cur_codes
        _BLOCK_CACHE["path"] = str(path)
        _BLOCK_CACHE["mtime"] = mtime
        _BLOCK_CACHE["blocks"] = out
        return dict(out)


def _constituents_provider(cfg: AStockConfig):
    """Lazy Tushare constituents provider (cache under storage/cache/constituents).

    The project is Tushare-only: servers without a 通达信 client use this
    as the formal constituents data source; TDX block files remain a
    fallback for local deployments that still download them.
    """
    import os as _os

    from ..data.tushare_constituents import TushareConstituentsProvider

    return TushareConstituentsProvider(
        token=_os.environ.get("TUSHARE_TOKEN", "").strip() or None,
        cache_dir=Path(cfg.storage_root) / "cache" / "constituents",
    )


def _weight_members(snap: List[Dict]) -> List[Tuple[int, str]]:
    """index_weight rows -> [(mkt, code6)]; mkt: 1=SSE, 0=SZSE.

    ``con_code`` arrives as ``300750.SZ`` (with exchange suffix); the
    suffix is stripped to keep a bare 6-digit code.
    """
    out: List[Tuple[int, str]] = []
    for row in snap or []:
        code = str(row.get("con_code") or "").strip()
        code = code.split(".")[0] if "." in code else code
        if len(code) != 6 or not code.isdigit():
            continue
        out.append((1 if code[0] in ("6", "9") else 0, code))
    return out


def index_constituents(
    cfg: AStockConfig,
    std_code: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Constituent stocks of an index std code (e.g. SSE.IDX.000300).

    Tushare ``index_weight`` is the formal source (works without TDX); the
    TDX ``infoharbor_block`` ZS_ blocks remain a fallback.
    """
    disp = display_code(std_code)
    code6 = std_code.split(".")[-1]
    name = _INDEX_CODE_TO_NAME.get(code6) or resolve_index_etf_name(std_code)

    # ---- Tushare 数据源(正式) ----
    tushare_source = ""
    try:
        prov = _constituents_provider(cfg)
        suffix = "SH" if std_code.startswith("SSE.IDX") else "SZ"
        snap_date, snap = prov.fetch_index_constituents(f"{code6}.{suffix}")
        members = _weight_members(snap)
        items = _members_to_entries(cfg, members)
        if limit is not None:
            items = items[: max(0, int(limit))]
        return {
            "ok": True,
            "code": disp,
            "name": name or "",
            "symbol_type": "index",
            "std_code": std_code,
            "tracked_index": code6,
            "tracked_index_name": name or "",
            "count": len(members),
            "constituents": items,
            "source": f"tushare:index_weight@{snap_date}",
            "note": "",
            "tushare_error": None,
        }
    except Exception as _e:
        tushare_source = str(_e)

    # ---- TDX 兜底(本地有通达信盘后数据时) ----
    blocks = load_block_constituents(cfg)
    members = blocks.get(name, []) if name else []
    source = f"tdx_infoharbor_block:ZS_{name}" if name else ""
    note = ""
    if not members:
        note = (
            f"通达信本地板块文件未收录「{name or code6}」成分股"
            "（盘后数据未下载或该指数无 ZS_ 板块），请更新通达信盘后数据后重试。"
        )
    items = _members_to_entries(cfg, members)
    if limit is not None:
        items = items[: max(0, int(limit))]
    return {
        "ok": True,
        "code": disp,
        "name": name or "",
        "symbol_type": "index",
        "std_code": std_code,
        "tracked_index": code6,
        "tracked_index_name": name or "",
        "count": len(members),
        "constituents": items,
        "source": source,
        "note": note,
        "tushare_error": tushare_source or None,
    }


def etf_constituents(
    cfg: AStockConfig,
    std_code: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Constituent stocks of an ETF via its tracked index.

    Tushare ``fund_basic.benchmark`` (keyword-matched to the tracked index)
    + ``index_weight`` is the formal source; TDX ``specetfdata`` /
    ``infoharbor_block`` remain a fallback.
    """
    disp = display_code(std_code)
    code6 = std_code.split(".")[-1]
    etf_name = resolve_index_etf_name(std_code)

    # ---- Tushare 数据源(正式) ----
    tushare_source = ""
    try:
        prov = _constituents_provider(cfg)
        suffix = "SH" if std_code.startswith("SSE.ETF") else "SZ"
        ts_code = f"{code6}.{suffix}"
        track_map = prov.fetch_etf_track_map()
        hit = track_map.get(ts_code)
        if hit is None:
            # 新 ETF 可能不在当日缓存里:每日最多强制刷新一次映射
            # (避免每个未映射 ETF 的每次查询都全量拉 fund_basic)。
            track_map = prov.fetch_etf_track_map(force=prov.should_force_refresh())
            hit = track_map.get(ts_code)
        if hit:
            key, idx_codes = hit
            # 同名指数可能有多个代码,逐个尝试直到有 index_weight 数据
            # (如 创业板 395004.SZ 无数据,创业板指 399006.SZ 有)。
            resolved_code, snap_date, snap = prov.fetch_index_constituents_multi(
                idx_codes
            )
            members = _weight_members(snap)
            items = _members_to_entries(cfg, members)
            if limit is not None:
                items = items[: max(0, int(limit))]
            tracked6 = resolved_code.split(".")[0]
            return {
                "ok": True,
                "code": disp,
                "name": etf_name or "",
                "symbol_type": "etf",
                "std_code": std_code,
                "tracked_index": tracked6,
                "tracked_index_name": key,
                "count": len(members),
                "constituents": items,
                "source": (
                    f"tushare:fund_basic.benchmark({ts_code}->{resolved_code});"
                    f"index_weight@{snap_date}"
                ),
                "note": "",
                "tushare_error": None,
            }
        raise TushareConstituentsError(
            f"fund_basic.benchmark 未匹配到跟踪指数: {ts_code}"
        )
    except Exception as _e:
        tushare_source = str(_e)

    # ---- TDX 兜底(本地有通达信盘后数据时) ----
    spec = load_spec_etf_map(cfg)
    tracked = spec.get(code6)
    idx_std = f"SSE.IDX.{tracked}" if tracked and tracked.startswith("0") else (
        f"SZSE.IDX.{tracked}" if tracked else ""
    )
    idx_name = _INDEX_CODE_TO_NAME.get(tracked or "", "") if tracked else ""
    blocks = load_block_constituents(cfg)
    members = blocks.get(idx_name, []) if idx_name else []
    source = f"specetfdata:{code6}->{tracked};tdx_infoharbor_block:ZS_{idx_name}" if (tracked and idx_name) else ""
    note = ""
    if not tracked:
        note = f"通达信 specetfdata 未收录该 ETF 的跟踪指数（{disp}），无法获取成分股。"
    elif not idx_name:
        note = f"已知跟踪指数 {tracked}，但缺少指数代码→名称映射，无法定位成分板块。"
    elif not members:
        note = (
            f"跟踪指数「{idx_name}」({tracked}) 的 ZS_ 板块未收录"
            "（通达信盘后数据未下载），请更新后重试。"
        )
    items = _members_to_entries(cfg, members)
    if limit is not None:
        items = items[: max(0, int(limit))]
    return {
        "ok": True,
        "code": disp,
        "name": etf_name or "",
        "symbol_type": "etf",
        "std_code": std_code,
        "tracked_index": tracked or "",
        "tracked_index_name": idx_name,
        "count": len(members),
        "constituents": items,
        "source": source,
        "note": note,
        "tushare_error": tushare_source or None,
    }


def resolve_constituents(
    cfg: AStockConfig,
    std_code: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Dispatch to index/ETF constituent lookup by std code type."""
    if std_code.startswith(("SSE.ETF.", "SZSE.ETF.")):
        return etf_constituents(cfg, std_code, limit=limit)
    if std_code.startswith(("SSE.IDX.", "SZSE.IDX.")):
        return index_constituents(cfg, std_code, limit=limit)
    raise ValueError(f"成分股查询仅支持指数/ETF：{std_code}")


def _members_to_entries(
    cfg: AStockConfig,
    members: List[Tuple[int, str]],
) -> List[Dict[str, str]]:
    """Convert (mkt, code6) members to display entries with stock names."""
    from .stock_names import resolve_stock_name

    out: List[Dict[str, str]] = []
    seen: set = set()
    for mkt, code6 in members:
        if code6 in seen:
            continue
        seen.add(code6)
        if mkt == 1:
            std = f"SSE.STK.{code6}"
            disp = f"sh{code6}"
        else:
            std = f"SZSE.STK.{code6}"
            disp = f"sz{code6}"
        out.append(
            {
                "code": disp,
                "std_code": std,
                "name": resolve_stock_name(cfg, disp, std_code=std) or "",
            }
        )
    return out
