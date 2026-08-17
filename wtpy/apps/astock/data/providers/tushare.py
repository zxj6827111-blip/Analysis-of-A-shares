"""Tushare provider – wraps tushare pro API for daily/adj_factor/qfq bars."""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from .base import (
    AdjustmentMode,
    AuthenticationError,
    BarPeriod,
    DataNotDownloaded,
    DataSource,
    IncompleteResponse,
    InvalidSymbol,
    MarketBar,
    MarketDataRequest,
    PermissionDenied,
    ProviderCapabilities,
    ProviderUnavailable,
    RateLimited,
    UniverseEntry,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SEC = 1.0
RATE_LIMIT_BACKOFF_SEC = 5.0

# Tushare 普通积分限频 500 次/分钟(全局 token 级滑动窗口)。股票链 none+qfq
# 两个 phase 共 5000+ 只逐只调用,无节流连打必然触发限流,进而拖垮紧随其后
# 的 adj_factor 链(见 2026-08-13 EOD 事故: raw 成功但 factor 限流 partial,
# 正式 L1/L2 被 fail-closed 跳过)。默认 300/min 留出约 40% 余量给偶发重试
# 与限流窗口抖动;可用 TUSHARE_RATE_PER_MIN 覆盖。
DEFAULT_RATE_PER_MIN = 300

# Tushare pro.daily / index_daily / fund_daily cap a single call at 6000 rows
# (~25 years of daily bars). One-shot full-history requests used to be silently
# truncated, leaving per-symbol series stuck on early 2000s data. Split every
# window into year-sized chunks so each call stays far below the cap.
PAGE_YEARS = 3
PAGE_YEAR_MIN = 1990


def _page_year_ranges(
    start_date: Optional[int], end_date: Optional[int]
) -> List[Tuple[Optional[int], Optional[int]]]:
    """Split [start, end] into <= PAGE_YEARS-year sub-windows (inclusive)."""
    import datetime as _dt

    end_int = int(end_date or int(_dt.date.today().strftime("%Y%m%d")))
    start_int = int(start_date or PAGE_YEAR_MIN * 10000 + 101)
    if start_int > end_int:
        return []
    y0, y1 = start_int // 10000, end_int // 10000
    out: List[Tuple[Optional[int], Optional[int]]] = []
    for y in range(y0, y1 + 1, PAGE_YEARS):
        seg_start = max(start_int, y * 10000 + 101)
        seg_end = min(end_int, min(y + PAGE_YEARS - 1, y1) * 10000 + 1231)
        if seg_start <= seg_end:
            out.append((seg_start, seg_end))
    return out


def _merge_paged_frames(frames: List) -> Any:
    """Concatenate paged API frames into one ascending, deduped frame."""
    import pandas as pd

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "trade_date" not in df.columns:
        return df
    df = df.drop_duplicates(subset="trade_date", keep="last")
    df = df.sort_values("trade_date", ascending=True)
    return df.reset_index(drop=True)


def _symbol_kind(symbol: str) -> str:
    """stock / index / etf from any symbol spelling (SSE.IDX.*, sh000001...)."""
    s = symbol.upper()
    if ".IDX." in s or "/IDX/" in s:
        return "index"
    if ".ETF." in s or "/ETF/" in s:
        return "etf"
    lower = symbol.lower()
    if len(lower) == 8 and lower[:2] in ("sh", "sz") and lower[2:].isdigit():
        code, pfx = lower[2:], lower[:2]
        if pfx == "sh":
            if code.startswith("000"):
                return "index"
            if code.startswith(("51", "56", "58")):
                return "etf"
        else:
            if code.startswith("399"):
                return "index"
            if code.startswith(("15", "16", "18")):
                return "etf"
        return "stock"
    parts = lower.split(".")
    if (
        len(parts) == 2
        and parts[1] in ("sh", "sz")
        and len(parts[0]) == 6
        and parts[0].isdigit()
    ):
        if parts[1] == "sh":
            if parts[0].startswith("000"):
                return "index"
            if parts[0].startswith(("51", "56", "58")):
                return "etf"
        else:
            if parts[0].startswith("399"):
                return "index"
            if parts[0].startswith(("15", "16", "18")):
                return "etf"
    return "stock"


class _GlobalRateGate:
    """Process-wide Tushare rate gate shared by every TushareProvider.

    Tushare 限流是全局 token 级(500/min 滑动窗口),而 EOD 链中股票链和
    factor 链各自 new 一个 TushareProvider:若每个实例独立节流,股票链刚
    打完 300/min 的窗口,factor 链立刻用新实例继续打,叠加后必然撞 500/min
    限流(2026-08-13 事故: factor 批量路径第一天就 RateLimited 并 fallback
    到 per-symbol,尾部 27 只仍失败)。所有实例必须共享同一个速率闸。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call_ts = 0.0

    def throttle(self, rate_per_min: int) -> None:
        rpm = max(0, int(rate_per_min or 0))
        if rpm <= 0:
            return
        interval = 60.0 / rpm
        with self._lock:
            now = time.time()
            wait = interval - (now - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            self._last_call_ts = now


_GLOBAL_RATE_GATE = _GlobalRateGate()


class TushareProvider:
    """Fetches bars from Tushare pro API."""

    def __init__(self, token: Optional[str] = None, rate_per_min: Optional[int] = None):
        self._token = token
        self._pro = None
        self._ts = None
        self._initialized = False
        # 全局限速:所有 Tushare 调用都经过 _call_with_retry,在那里统一节流。
        # rate 解析顺序:显式参数 > TUSHARE_RATE_PER_MIN 环境变量 > 默认值。
        # 0 表示显式禁用节流(不能用 `or DEFAULT` 兜底,0 是合法值)。
        rpm = rate_per_min
        if rpm is None:
            import os as _os

            env_val = _os.environ.get("TUSHARE_RATE_PER_MIN")
            try:
                rpm = int(env_val) if env_val else DEFAULT_RATE_PER_MIN
            except (TypeError, ValueError):
                rpm = DEFAULT_RATE_PER_MIN
        self._rate_per_min = max(0, int(rpm or 0))

    def _throttle(self) -> None:
        """Enforce the global per-minute call budget before each API call.

        Delegates to the process-wide rate gate so the EOD raw + factor chains
        (separate provider instances) share one budget — otherwise the factor
        chain starts right after the raw chain burned the sliding window and
        trips Tushare's 500/min limit (2026-08-13 事故根因)。
        """
        _GLOBAL_RATE_GATE.throttle(self._rate_per_min)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            import tushare as ts
        except ImportError:
            raise ProviderUnavailable("tushare package not installed")

        self._ts = ts
        token = self._token
        if not token:
            try:
                token = ts.get_token()
            except Exception:
                pass
        if not token:
            raise AuthenticationError(
                "No Tushare token available. Use ts.set_token() or pass token explicitly."
            )
        try:
            ts.set_token(token)
            # requests 的单个 timeout 只限制读取阶段,connect(SYN)阶段无限
            # 等待:服务器到 tushare 间歇性丢包时,全量 delta EOD 会在建连
            # 处永久挂死(2026-08-17 生产事故)。用元组限制 connect=8s +
            # read=30s,超时抛异常交给 _call_with_retry 重试。
            self._pro = ts.pro_api(timeout=(8, 30))
            self._initialized = True
        except Exception as e:
            raise AuthenticationError(f"Tushare API init failed: {e}")

    def health_check(self) -> bool:
        try:
            self._ensure_initialized()
            df = self._pro.trade_cal(exchange="SSE", limit=1)
            return df is not None and len(df) > 0
        except Exception:
            return False

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source=DataSource.TUSHARE,
            adjustments=[AdjustmentMode.NONE, AdjustmentMode.QFQ],
            periods=[BarPeriod.DAY],
            supports_batch=False,
            max_batch_size=1,
            requires_client_online=False,
            supports_universe=True,
            supports_delisted=True,
            supports_bse=True,
        )

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        self._ensure_initialized()

        if request.adjustment not in (AdjustmentMode.NONE, AdjustmentMode.QFQ):
            raise InvalidSymbol(
                f"TushareProvider supports none/qfq, got {request.adjustment}"
            )
        if request.period != BarPeriod.DAY:
            raise InvalidSymbol(
                f"TushareProvider only supports period=1d, got {request.period}"
            )

        bars: List[MarketBar] = []
        for symbol in request.symbols:
            ts_code = self._to_ts_code(symbol)
            kind = _symbol_kind(symbol)
            if request.adjustment == AdjustmentMode.QFQ:
                if kind != "stock":
                    raise InvalidSymbol(
                        f"Tushare QFQ not available for {symbol} "
                        "(indices/ETFs are unadjusted only)"
                    )
                sym_bars = self._fetch_qfq(ts_code, request, symbol)
            elif kind == "index":
                sym_bars = self._fetch_index_daily(ts_code, request, symbol)
            elif kind == "etf":
                sym_bars = self._fetch_fund_daily(ts_code, request, symbol)
            else:
                sym_bars = self._fetch_raw_daily(ts_code, request, symbol)
            bars.extend(sym_bars)
        return bars

    def _fetch_raw_daily(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        frames = []
        for seg_start, seg_end in _page_year_ranges(
            request.start_date, request.end_date
        ):
            df = self._call_with_retry(
                self._pro.daily,
                ts_code=ts_code,
                start_date=str(seg_start or ""),
                end_date=str(seg_end or ""),
            )
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            raise DataNotDownloaded(f"No daily data for {ts_code}")
        return self._dataframe_to_bars(
            _merge_paged_frames(frames), request, AdjustmentMode.NONE, symbol
        )

    def _fetch_index_daily(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        frames = []
        for seg_start, seg_end in _page_year_ranges(
            request.start_date, request.end_date
        ):
            df = self._call_with_retry(
                self._pro.index_daily,
                ts_code=ts_code,
                start_date=str(seg_start or ""),
                end_date=str(seg_end or ""),
            )
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            raise DataNotDownloaded(f"No index_daily data for {ts_code}")
        return self._dataframe_to_bars(
            _merge_paged_frames(frames), request, AdjustmentMode.NONE, symbol
        )

    def _fetch_fund_daily(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        frames = []
        for seg_start, seg_end in _page_year_ranges(
            request.start_date, request.end_date
        ):
            df = self._call_with_retry(
                self._pro.fund_daily,
                ts_code=ts_code,
                start_date=str(seg_start or ""),
                end_date=str(seg_end or ""),
            )
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            raise DataNotDownloaded(f"No fund_daily data for {ts_code}")
        return self._dataframe_to_bars(
            _merge_paged_frames(frames), request, AdjustmentMode.NONE, symbol
        )

    def _fetch_qfq(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        assert self._ts is not None
        frames = []
        for seg_start, seg_end in _page_year_ranges(
            request.start_date, request.end_date
        ):
            df = self._call_with_retry(
                self._ts.pro_bar,
                ts_code=ts_code,
                adj="qfq",
                start_date=str(seg_start or ""),
                end_date=str(seg_end or ""),
            )
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            raise IncompleteResponse(f"No qfq data for {ts_code}")
        return self._dataframe_to_bars(
            _merge_paged_frames(frames), request, AdjustmentMode.QFQ, symbol
        )

    def _dataframe_to_bars(
        self,
        df,
        request: MarketDataRequest,
        adjustment: AdjustmentMode,
        symbol: Optional[str] = None,
    ) -> List[MarketBar]:
        bars: List[MarketBar] = []
        if symbol is None:
            symbol = request.symbols[0]

        def _num(v, default: float = 0.0) -> float:
            """Coerce a value to float; None / NaN / blank -> default.

            Tushare index_daily/fund_daily may carry null OHLC on some
            historical rows (e.g. SH indices around 2004-12), which after
            paged-frame merging surface as None and crash float(None). Bars
            still have a close, so OHLC falls back to close; vol/amount to 0.
            """
            if v is None:
                return default
            try:
                f = float(v)
            except (TypeError, ValueError):
                return default
            return f if f == f else default  # NaN -> default

        for _, row in df.iterrows():
            try:
                trade_date = int(str(row["trade_date"]))
            except (KeyError, ValueError, TypeError):
                continue
            if request.start_date and trade_date < request.start_date:
                continue
            if request.end_date and trade_date > request.end_date:
                continue
            close = _num(row.get("close"))
            # Unit normalization (share/CNY standard, UNIT_STANDARD=share_yuan):
            # Tushare daily `vol` is in 手 (lots of 100 shares) and `amount`
            # in 千元 (1000 CNY) — convert to 股/元 so the blob matches the
            # delisted pool, local_vendor and minute_vendor surfaces, which
            # all store 股/元 (x100 / x1000). Without this, a hybrid dataset
            # mixing local_vendor history with tushare increments breaks at
            # the seam (100x volume / 1000x amount), and the formal L2
            # (tushare/none + delisted complement) is unit-mixed.
            bars.append(
                MarketBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    period=request.period.value,
                    open=_num(row.get("open"), close),
                    high=_num(row.get("high"), close),
                    low=_num(row.get("low"), close),
                    close=close,
                    volume=_num(row.get("vol", row.get("volume"))) * 100.0,
                    amount=_num(row.get("amount")) * 1000.0,
                    source=DataSource.TUSHARE.value,
                    adjustment=adjustment.value,
                    anchor_date=request.anchor_date,
                    data_cutoff_date=request.end_date,
                    provider_version=self.provider_version(),
                )
            )
        return bars

    def _call_with_retry(self, fn, **kwargs):
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                return fn(**kwargs)
            except PermissionDenied:
                raise
            except AuthenticationError:
                raise
            except Exception as e:
                err_str = str(e).lower()
                if "permission" in err_str or "权限" in err_str:
                    raise PermissionDenied(f"Tushare permission denied: {e}")
                if "token" in err_str or "认证" in err_str:
                    raise AuthenticationError(f"Tushare auth failed: {e}")
                if "limit" in err_str or "频率" in err_str or "freq" in err_str:
                    last_err = RateLimited(str(e), retry_after=RATE_LIMIT_BACKOFF_SEC)
                    time.sleep(RATE_LIMIT_BACKOFF_SEC * (2 ** attempt))
                    continue
                last_err = ProviderUnavailable(f"Tushare call failed: {e}")
                time.sleep(BASE_BACKOFF_SEC * (2 ** attempt))
        raise last_err or ProviderUnavailable("Tushare call failed after retries")

    def fetch_adj_factor(
        self,
        ts_code: Optional[str] = None,
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        trade_date: Optional[int] = None,
    ):
        """Fetch adj_factor for a stock or full market on a trade_date."""
        self._ensure_initialized()
        if trade_date and ts_code:
            raise ValueError("ts_code and trade_date are mutually exclusive")
        kwargs = {}
        if trade_date:
            kwargs["trade_date"] = str(trade_date)
        else:
            kwargs["ts_code"] = ts_code
            if start_date:
                kwargs["start_date"] = str(start_date)
            if end_date:
                kwargs["end_date"] = str(end_date)
        return self._call_with_retry(self._pro.adj_factor, **kwargs)

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        self._ensure_initialized()
        entries: List[UniverseEntry] = []

        df_listed = self._call_with_retry(
            self._pro.stock_basic, list_status="L"
        )
        if df_listed is not None and not df_listed.empty:
            entries.extend(self._stock_basic_to_entries(df_listed, "listed"))

        if include_delisted:
            df_delisted = self._call_with_retry(
                self._pro.stock_basic, list_status="D"
            )
            if df_delisted is not None and not df_delisted.empty:
                entries.extend(self._stock_basic_to_entries(df_delisted, "delisted"))

        if not include_bse:
            entries = [e for e in entries if e.exchange != "BSE"]

        return entries

    def fetch_index_etf_universe(self) -> List[UniverseEntry]:
        """Fetch exchange-listed indices (SSE/SZSE) and ETFs for the warehouse.

        Returns UniverseEntry symbols in SSE.IDX.* / SZSE.IDX.* /
        SSE.ETF.* / SZSE.ETF.* form so bars land in the same dataset family
        as stocks (raw/none only — indices/ETFs have no 复权).
        """
        self._ensure_initialized()
        entries: List[UniverseEntry] = []
        seen: set = set()

        df_idx = self._call_with_retry(self._pro.index_basic)
        if df_idx is not None and not df_idx.empty:
            df_idx = df_idx[df_idx.get("market", "").isin(("SSE", "SZSE"))]
            for _, row in df_idx.iterrows():
                ts_code = str(row.get("ts_code", ""))
                symbol = self._index_etf_ts_code_to_symbol(ts_code, "IDX")
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                entries.append(
                    UniverseEntry(
                        symbol=symbol,
                        name=str(row.get("name", "")),
                        exchange=symbol.split(".")[0],
                        list_date=self._parse_date(row.get("list_date")),
                        status="listed",
                        source=DataSource.TUSHARE.value,
                    )
                )

        # list_status=L alone truncates at 15000 rows (Tushare cap), which can
        # drop large ETFs; market='E' (exchange-traded funds) returns them all.
        df_fund = self._call_with_retry(
            self._pro.fund_basic, market="E", list_status="L"
        )
        if df_fund is not None and not df_fund.empty:
            df_fund = df_fund[df_fund.get("market", "") == "E"]
            if "fund_type" in df_fund.columns:
                df_fund = df_fund[df_fund["fund_type"] != "REITs"]
            for _, row in df_fund.iterrows():
                ts_code = str(row.get("ts_code", ""))
                symbol = self._index_etf_ts_code_to_symbol(ts_code, "ETF")
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                entries.append(
                    UniverseEntry(
                        symbol=symbol,
                        name=str(row.get("name", "")),
                        exchange=symbol.split(".")[0],
                        list_date=self._parse_date(row.get("list_date")),
                        status="listed",
                        source=DataSource.TUSHARE.value,
                    )
                )

        return entries

    @staticmethod
    def _index_etf_ts_code_to_symbol(ts_code: str, kind: str) -> str:
        """000001.SH -> SSE.IDX.000001, 510300.SH -> SSE.ETF.510300.

        Only exchange-listed codes with the app's known segments are kept
        (indices sh000xxx / sz399xxx; ETFs sh51/56/58xxxx / sz15/16/18xxxx).
        """
        parts = ts_code.split(".")
        if len(parts) != 2:
            return ""
        code, suffix = parts
        suffix = suffix.upper()
        if suffix not in ("SH", "SZ"):
            return ""
        exch = "SSE" if suffix == "SH" else "SZSE"
        if kind == "IDX":
            ok = (exch == "SSE" and code.startswith("000")) or (
                exch == "SZSE" and code.startswith("399")
            )
        elif kind == "ETF":
            ok = (exch == "SSE" and code.startswith(("51", "56", "58"))) or (
                exch == "SZSE" and code.startswith(("15", "16", "18"))
            )
        else:
            ok = False
        if not ok:
            return ""
        return f"{exch}.{kind}.{code}"

    def _stock_basic_to_entries(self, df, status: str) -> List[UniverseEntry]:
        entries = []
        for _, row in df.iterrows():
            ts_code = str(row.get("ts_code", ""))
            symbol = self._from_ts_code(ts_code)
            exchange = self._exchange_from_ts_code(ts_code)
            list_date = self._parse_date(row.get("list_date"))
            delist_date = self._parse_date(row.get("delist_date"))
            entries.append(
                UniverseEntry(
                    symbol=symbol,
                    name=str(row.get("name", "")),
                    exchange=exchange,
                    list_date=list_date,
                    delist_date=delist_date,
                    status=status,
                    source=DataSource.TUSHARE.value,
                )
            )
        return entries

    @staticmethod
    def _to_ts_code(symbol: str) -> str:
        """Convert SSE.STK.600000 / 600000.SH / sh600000 to 600000.SH format."""
        lower = symbol.lower()
        if len(lower) == 8 and lower[:2] in ("sh", "sz", "bj") and lower[2:].isdigit():
            return f"{lower[2:]}.{lower[:2].upper()}"
        if "." in symbol:
            parts = symbol.split(".")
            if len(parts) == 3:
                code = parts[2]
            else:
                code = parts[0]
                suffix = parts[1].upper()
                if suffix in ("SH", "SZ", "BJ"):
                    return f"{code}.{suffix}"
            exch = parts[0]
            if exch == "SSE":
                return f"{code}.SH"
            elif exch == "SZSE":
                return f"{code}.SZ"
            elif exch == "BSE":
                return f"{code}.BJ"
            return f"{code}.SH"
        code = symbol
        if code.startswith(("5", "6", "9")):
            return f"{code}.SH"
        return f"{code}.SZ"

    @staticmethod
    def _from_ts_code(ts_code: str) -> str:
        """Convert 600000.SH to SSE.STK.600000."""
        parts = ts_code.split(".")
        if len(parts) != 2:
            return ts_code
        code, suffix = parts
        suffix = suffix.upper()
        if suffix == "SH":
            return f"SSE.STK.{code}"
        elif suffix == "SZ":
            return f"SZSE.STK.{code}"
        elif suffix == "BJ":
            return f"BSE.STK.{code}"
        return ts_code

    @staticmethod
    def _exchange_from_ts_code(ts_code: str) -> str:
        parts = ts_code.split(".")
        if len(parts) != 2:
            return ""
        suffix = parts[1].upper()
        return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, "")

    @staticmethod
    def _parse_date(val) -> Optional[int]:
        if val is None:
            return None
        try:
            return int(str(val))
        except (ValueError, TypeError):
            return None

    def provider_version(self) -> str:
        try:
            import tushare as ts

            return f"tushare_{ts.__version__}"
        except Exception:
            return "tushare_unknown"
