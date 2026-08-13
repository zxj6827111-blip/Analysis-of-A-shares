# -*- coding: utf-8 -*-
"""Tushare constituent-stock data source (index members / ETF tracked index).

Replaces the TDX-local block files (``T0002/hq_cache/specetfdata.txt`` and
``infoharbor_block.dat``) so the bagua constituents endpoint works on
deployments WITHOUT a local 通达信 client — the project is Tushare-only.

Data sources:
  - index -> members   : ``index_weight`` (index_code, con_code, trade_date,
                         weight). Weight snapshots publish at month ends, so
                         the latest available trade_date is discovered by
                         backtracking from the query date (bounded, cached).
  - etf -> tracked idx : two-stage matching of ``fund_basic.benchmark``:
                         1) a curated keyword table (every entry verified
                            against index_weight), 2) automatic longest-suffix
                            matching against the full ``index_basic``
                            universe (paged fetch). ``fund_basic`` has no
                            machine-readable track_index field with the
                            current Tushare permission level, so benchmark
                            text matching is the reliable route.

All fetched data is cached under ``<storage_root>/cache/constituents/``:
the etf->index map is refreshed once a day, index member snapshots are keyed
by (index_code, trade_date) and immutable.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .io_util import atomic_write_json

# ---------------------------------------------------------------------------
# 手工关键词表(benchmark 文本 -> 指数 ts_code)。
# 每一项都经过 index_weight 实测验证(2026-07-31 有成分数据);匹配按
# key 长度降序,命中即停。覆盖 fund_basic 无 track_index 字段、且
# index_basic 名称与 benchmark 用词不一致的高频指数。
# ---------------------------------------------------------------------------
_BENCHMARK_KEYWORDS: List[Tuple[str, str]] = [
    ("中证全指证券公司", "399975.SZ"),     # 证券公司
    ("中证全指医药卫生", "000991.SH"),     # 全指医药
    ("中证主要消费", "000932.SH"),         # 800消费
    ("中证新能源汽车", "930997.CSI"),      # 新能源车
    ("中证电池主题", "931719.CSI"),        # CS电池
    ("中证光伏产业", "931151.CSI"),        # 光伏产业
    ("中证细分有色金属产业主题", "000811.CSI"),  # 细分有色
    ("中证细分化工产业主题", "000813.CSI"),  # 细分化工
    ("中证红利低波动100", "930955.CSI"),   # 红利低波100
    ("中证稀有金属主题", "930632.CSI"),    # CS稀金属
    ("中证小盘500", "000905.SH"),          # 中证500 旧名
    ("沪深300医药卫生", "000913.SH"),      # 300医药
    ("上海证券交易所上证红利", "000015.SH"),  # 上证红利
    ("国证有色金属", "399395.SZ"),         # 国证有色
    ("国证疫苗与生物科技", "980015.SZ"),   # 疫苗生科
    ("国证消费电子", "980030.SZ"),         # 消费电子
    ("中证银行", "399986.SZ"),
    ("中证软件", "930601.CSI"),
    ("中证高装", "930599.CSI"),
    ("上证科创板50成份", "000688.SH"),     # 科创50
    ("上证科创板100", "000698.SH"),        # 科创100
    ("上证科创板芯片", "000685.SH"),       # 科创芯片
    ("上海证券交易所50成份", "000016.SH"),  # 上证50
    ("上证综合", "000001.SH"),             # 上证指数
    ("上海证券交易所180成份", "000010.SH"),  # 上证180
    ("深圳证券交易所成份", "399001.SZ"),   # 深证成指
]

# 自动匹配前缀/标志词:benchmark 分段里出现这些词才尝试匹配 A 股指数。
_INDEX_FLAG_WORDS = (
    "中证", "上证", "深证", "国证", "科创", "创业", "沪深",
    "中华", "中债", "恒生", "标普",
)

# 缓存文件: ETF->指数映射(每日刷新) 与 指数成分快照(按交易日,不可变)
_TRACK_MAP_CACHE = "constituents_etf_track_map.json"
_INDEX_UNIVERSE_CACHE = "constituents_index_universe.json"
_CACHE_MAX_AGE_SEC = 24 * 3600

# index_basic 分页拉取参数
_INDEX_BASIC_PAGE = 5000
_INDEX_BASIC_MAX_PAGES = 4


class TushareConstituentsError(Exception):
    """成分股数据源不可用(无 token / 接口失败 / 无数据)。"""


def _norm_name(s: str) -> str:
    """Normalize an index name / benchmark fragment for matching."""
    s = str(s or "")
    for token in (
        " ", "指数", "收益率", "全收益", "净收益", "收盘", "同期",
        "(税后)", "(使用估值汇率折算)", "(经汇率调整后)", "(港元)", "(CNH)", "(价格)",
    ):
        s = s.replace(token, "")
    s = re.sub(r"×\d+(?:\.\d+)?%", "", s)   # ×95% / ×100%
    s = re.sub(r"[+×]?\d*\.?\d+%", "", s)   # 5% / +95% 等残留
    s = re.sub(r"[PR]$", "", s)             # 尾部 P/R(价格/收益版本标记,如 创业板指数P)
    return s


def _keyword_match(benchmark: str) -> Optional[Tuple[str, str]]:
    """Curated-table match: benchmark -> (index_ts_code, matched_key)."""
    if not benchmark:
        return None
    text = _norm_name(benchmark)
    for key, code in sorted(_BENCHMARK_KEYWORDS, key=lambda kv: -len(kv[0])):
        if key in text:
            return code, key
    return None


class TushareConstituentsProvider:
    """Tushare-backed constituents provider with on-disk caching.

    Thread-safe (single lock guards cache dir + map refresh); network calls
    happen outside the lock so concurrent queries serialize only on disk IO.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        *,
        max_backtrack_days: int = 60,
    ):
        token = token or os.environ.get("TUSHARE_TOKEN", "").strip()
        if not token:
            raise TushareConstituentsError(
                "TUSHARE_TOKEN 未配置,无法获取成分股数据"
            )
        self._token = token
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._max_backtrack_days = max(1, int(max_backtrack_days))
        self._lock = threading.Lock()
        self._pro = None
        self._index_universe: Optional[Dict[str, List[str]]] = None
        self._index_sorted: Optional[List[Tuple[str, List[str]]]] = None
        # name_key -> [ts_code, ...](按市场优先级),用于自动后缀匹配

    # ------------------------------------------------------------------
    # Tushare client
    # ------------------------------------------------------------------
    def _client(self):
        if self._pro is None:
            import tushare as ts

            self._pro = ts.pro_api(self._token)
        return self._pro

    def _call(self, fn, **kw) -> Optional[object]:
        """One-shot Tushare call; returns None on auth/network failure."""
        try:
            return fn(**kw)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 指数名称宇宙 (index_basic 全量,分页)
    # ------------------------------------------------------------------
    def _load_index_universe(
        self, cache_dir: Optional[Path], force: bool = False
    ) -> Dict[str, List[str]]:
        """Full A-share index name universe as {name_key: [ts_code, ...]}.

        Fetches index_basic in pages (~12.5k indices), keeps SSE/SZSE/CSI/CNI
        price versions only (drops CNY/HKD/USD variants and HK-prefixed
        codes), normalizes names, and sorts by name length descending so the
        longest name wins during suffix matching. The SAME name may map to
        several codes (e.g. 创业板 -> 395004.SZ without index_weight vs
        创业板指 -> 399006.SZ with data); all candidates are kept, ordered
        by market priority, and tried in turn at fetch time. Cached daily.
        """
        cache_dir = Path(cache_dir) if cache_dir else self._cache_dir
        if cache_dir is not None and not force:
            path = cache_dir / _INDEX_UNIVERSE_CACHE
            try:
                if path.exists():
                    import time as _time

                    if _time.time() - path.stat().st_mtime < _CACHE_MAX_AGE_SEC:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        return {k: list(v) for k, v in raw.items()}
            except Exception:
                pass
        pro = self._client()
        frames = []
        for off in range(0, _INDEX_BASIC_PAGE * _INDEX_BASIC_MAX_PAGES, _INDEX_BASIC_PAGE):
            df = self._call(
                pro.index_basic, limit=_INDEX_BASIC_PAGE, offset=off
            )
            if df is None or df.empty:
                break
            frames.append(df)
        universe: Dict[str, List[str]] = {}
        prio_map: Dict[str, int] = {}
        for df in frames:
            for _, row in df.iterrows():
                code = str(row.get("ts_code") or "")
                mkt = str(row.get("market") or "")
                if mkt not in ("SSE", "SZSE", "CSI", "CNI"):
                    continue
                if "CNY" in code or (code and code[0] in ("H", "N")):
                    continue  # 外币/港股版本,index_weight 不支持
                key = _norm_name(str(row.get("name") or ""))
                if len(key) < 3:
                    continue
                prio = {"SSE": 0, "SZSE": 0, "CSI": 1, "CNI": 2}[mkt]
                codes = universe.setdefault(key, [])
                if code not in codes:
                    codes.append(code)
                    prio_map[f"{key}\x00{code}"] = prio
        # 同一 key 内按市场优先级排序(SSE/SZSE 优先,index_weight 覆盖更全)
        for key, codes in universe.items():
            codes.sort(key=lambda c: prio_map.get(f"{key}\x00{c}", 9))
        if cache_dir is not None:
            try:
                path = cache_dir / _INDEX_UNIVERSE_CACHE
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, {k: list(v) for k, v in universe.items()})
            except Exception:
                pass
        return universe

    def _auto_match_index(
        self, benchmark: str
    ) -> Optional[Tuple[str, List[str]]]:
        """Automatic longest-suffix match of a benchmark against the universe.

        For every '+' segment containing an A-share index flag word, the
        longest normalized universe name that is a SUFFIX of the segment
        (prefix gap <= 2 chars, e.g. "中证"+"新能源车") wins. Suffix-only
        matching rejects prefix collisions like "沪深300" vs
        "沪深300自由现金流" which would otherwise map to the wrong index.
        Returns (matched_key, candidate_codes).
        """
        try:
            if self._index_sorted is None:
                universe = self._load_index_universe(self._cache_dir)
                self._index_universe = universe
                # 按 key 长度降序排序一次,匹配时直接遍历(避免每次排序)
                self._index_sorted = sorted(
                    universe.items(), key=lambda kv: -len(kv[0])
                )
            sorted_items = self._index_sorted
        except Exception:
            return None
        text = _norm_name(benchmark)
        if len(text) < 2:
            return None
        for seg in text.split("+"):
            if not any(w in seg for w in _INDEX_FLAG_WORDS):
                continue
            for key, codes in sorted_items:
                if key in seg and seg.endswith(key):
                    if len(seg) - len(key) <= 2:
                        return key, codes
        return None

    # ------------------------------------------------------------------
    # 指数成分 (index_weight)
    # ------------------------------------------------------------------
    def _weight_snapshot(
        self, index_code: str, trade_date: int
    ) -> Optional[List[Dict]]:
        df = self._call(
            self._client().index_weight,
            index_code=index_code,
            trade_date=str(trade_date),
        )
        if df is None or df.empty:
            return None
        out = []
        for _, row in df.iterrows():
            out.append(
                {
                    "con_code": str(row.get("con_code", "")),
                    "weight": float(row.get("weight") or 0.0),
                }
            )
        return out or None

    def _latest_snapshot_date(self, index_code: str, asof: Optional[int]) -> int:
        """Find the newest trade_date (<= asof) with a published weight snapshot.

        Backtracks day by day from asof (bounded by max_backtrack_days),
        starting at month ends which is when Tushare publishes index weights.
        """
        import datetime as _dt

        base = _dt.date.today()
        if asof:
            try:
                base = _dt.datetime.strptime(str(asof), "%Y%m%d").date()
            except ValueError:
                base = _dt.date.today()
        for i in range(self._max_backtrack_days):
            d = base - _dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            snap = self._weight_snapshot(index_code, int(d.strftime("%Y%m%d")))
            if snap:
                return int(d.strftime("%Y%m%d"))
        raise TushareConstituentsError(
            f"index_weight 在最近 {self._max_backtrack_days} 天内无数据: {index_code}"
        )

    def fetch_index_constituents(
        self,
        index_code: str,
        *,
        asof: Optional[int] = None,
        cache_dir: Optional[Path] = None,
    ) -> Tuple[int, List[Dict]]:
        """Return (weight_trade_date, [{'con_code','weight'}]) for an index.

        Cache hit returns the snapshot without any Tushare call. The
        snapshot date is resolved once per query; a cached snapshot for the
        resolved date is reused, otherwise the latest published date is
        fetched and cached.
        """
        cache_dir = Path(cache_dir) if cache_dir else self._cache_dir
        if cache_dir is not None:
            try:
                latest_date = self._latest_date_from_cache(cache_dir, index_code)
            except Exception:
                latest_date = None
            if latest_date:
                cached = self._read_cache(cache_dir, index_code, latest_date)
                if cached is not None:
                    # 权重按月末发布:快照与查询日相差超过 40 天视为过期
                    # (如 8 月末新权重发布后,9 月查询不应再返回 7/31 快照),
                    # 过期则忽略缓存重新回溯最新快照。
                    if not self._snapshot_fresh(latest_date, asof):
                        latest_date = None
                    else:
                        return latest_date, cached
        snap_date = self._latest_snapshot_date(index_code, asof)
        snap = self._weight_snapshot(index_code, snap_date)
        if not snap:
            raise TushareConstituentsError(
                f"index_weight 无数据: {index_code} @ {snap_date}"
            )
        if cache_dir is not None:
            self._write_cache(cache_dir, index_code, snap_date, snap)
        return snap_date, snap

    @staticmethod
    def _snapshot_fresh(snapshot_date: int, asof: Optional[int]) -> bool:
        """True when a weight snapshot is still fresh for the query date.

        Tushare publishes index weights at month ends; a snapshot older than
        ~40 days is stale even if cached (a newer month-end snapshot should
        be fetched).
        """
        if not asof:
            return True
        try:
            import datetime as _dt

            a = _dt.datetime.strptime(str(asof), "%Y%m%d")
            l = _dt.datetime.strptime(str(snapshot_date), "%Y%m%d")
            return (a - l).days <= 40
        except ValueError:
            return True

    def fetch_index_constituents_multi(
        self,
        index_codes: List[str],
        *,
        asof: Optional[int] = None,
        cache_dir: Optional[Path] = None,
    ) -> Tuple[str, int, List[Dict]]:
        """Try candidate index codes in order until one yields constituents.

        The same name may map to several codes and only some carry
        index_weight data (e.g. 创业板 395004.SZ has none, 创业板指
        399006.SZ has it); candidates are tried in market-priority order.
        Returns (index_code, weight_trade_date, snapshot).
        """
        last_err: Optional[TushareConstituentsError] = None
        for code in index_codes or []:
            try:
                snap_date, snap = self.fetch_index_constituents(
                    code, asof=asof, cache_dir=cache_dir
                )
                return code, snap_date, snap
            except TushareConstituentsError as e:
                last_err = e
        raise TushareConstituentsError(
            f"候选指数均无成分数据 {index_codes}: {last_err}"
        )

    # ------------------------------------------------------------------
    # ETF -> 跟踪指数 (fund_basic.benchmark)
    # ------------------------------------------------------------------
    def should_force_refresh(self, cache_dir: Optional[Path] = None) -> bool:
        """Whether the etf->index map should be force-refreshed today.

        A fresh cache file (written within CACHE_MAX_AGE_SEC) means the map
        was already rebuilt recently; force-refreshing again would repeat a
        full fund_basic call for every unmapped ETF query.
        """
        cache_dir = Path(cache_dir) if cache_dir else self._cache_dir
        if cache_dir is None:
            return True
        path = cache_dir / _TRACK_MAP_CACHE
        try:
            import time as _time

            if path.exists() and _time.time() - path.stat().st_mtime < _CACHE_MAX_AGE_SEC:
                return False
        except Exception:
            pass
        return True

    def fetch_etf_track_map(
        self, cache_dir: Optional[Path] = None, *, force: bool = False
    ) -> Dict[str, Tuple[str, List[str]]]:
        """Return {etf_ts_code: (matched_key, [index_ts_code, ...])}.

        Only exchange-traded index funds participate: fund names containing
        "ETF" or "指数" (skips mixed/bond LOF funds whose benchmark is a
        deposit rate or similar). Cache is refreshed at most once per day
        (or immediately with ``force=True``).
        """
        cache_dir = Path(cache_dir) if cache_dir else self._cache_dir
        if cache_dir is not None and not force:
            path = cache_dir / _TRACK_MAP_CACHE
            try:
                if path.exists():
                    import time as _time

                    if _time.time() - path.stat().st_mtime < _CACHE_MAX_AGE_SEC:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        out: Dict[str, Tuple[str, List[str]]] = {}
                        for k, v in raw.items():
                            if isinstance(v, list) and len(v) == 2:
                                if isinstance(v[1], list):
                                    # 新格式 [key, [codes]]
                                    out[k] = (str(v[0]), list(v[1]))
                                elif isinstance(v[1], str):
                                    # 旧格式 [code, key]:包装为单代码候选
                                    out[k] = (v[1], [str(v[0])])
                        return out
            except Exception:
                pass
        df = self._call(
            self._client().fund_basic,
            market="E",
            list_status="L",
            fields="ts_code,name,benchmark",
        )
        if df is None or df.empty:
            raise TushareConstituentsError("fund_basic 无数据(检查 token/权限)")
        out: Dict[str, Tuple[str, List[str]]] = {}
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            name = str(row.get("name") or "")
            if not ts_code or ("ETF" not in name and "指数" not in name):
                continue  # 只映射场内指数基金
            benchmark = str(row.get("benchmark") or "")
            manual = _keyword_match(benchmark)
            if manual:
                code, key = manual
                out[ts_code] = (key, [code])
                continue
            auto = self._auto_match_index(benchmark)
            if auto:
                key, codes = auto
                out[ts_code] = (key, codes)
        if cache_dir is not None:
            try:
                path = cache_dir / _TRACK_MAP_CACHE
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, {k: [v[0], v[1]] for k, v in out.items()})
            except Exception:
                pass
        return out

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_path(cache_dir: Path, index_code: str, trade_date: int) -> Path:
        return cache_dir / f"constituents_{index_code.replace('.', '_')}_{trade_date}.json"

    @staticmethod
    def _read_cache(cache_dir: Path, index_code: str, trade_date: int):
        try:
            p = TushareConstituentsProvider._cache_path(
                cache_dir, index_code, trade_date
            )
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    @staticmethod
    def _write_cache(cache_dir: Path, index_code: str, trade_date: int, snap) -> None:
        try:
            p = TushareConstituentsProvider._cache_path(
                cache_dir, index_code, trade_date
            )
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(p, snap)
        except Exception:
            pass

    @staticmethod
    def _latest_date_from_cache(cache_dir: Path, index_code: str) -> Optional[int]:
        prefix = f"constituents_{index_code.replace('.', '_')}_"
        best = None
        try:
            for p in cache_dir.glob(prefix + "*.json"):
                try:
                    d = int(p.stem.rsplit("_", 1)[-1])
                except (ValueError, IndexError):
                    continue
                if best is None or d > best:
                    best = d
        except Exception:
            return None
        return best
