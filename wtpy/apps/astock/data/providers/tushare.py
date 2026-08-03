"""Tushare provider – wraps tushare pro API for daily/adj_factor/qfq bars."""

from __future__ import annotations

import logging
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


class TushareProvider:
    """Fetches bars from Tushare pro API."""

    def __init__(self, token: Optional[str] = None):
        self._token = token
        self._pro = None
        self._ts = None
        self._initialized = False

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
            self._pro = ts.pro_api()
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
        df = self._call_with_retry(
            self._pro.daily,
            ts_code=ts_code,
            start_date=str(request.start_date or ""),
            end_date=str(request.end_date or ""),
        )
        if df is None or df.empty:
            raise DataNotDownloaded(f"No daily data for {ts_code}")
        return self._dataframe_to_bars(df, request, AdjustmentMode.NONE, symbol)

    def _fetch_index_daily(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        df = self._call_with_retry(
            self._pro.index_daily,
            ts_code=ts_code,
            start_date=str(request.start_date or ""),
            end_date=str(request.end_date or ""),
        )
        if df is None or df.empty:
            raise DataNotDownloaded(f"No index_daily data for {ts_code}")
        return self._dataframe_to_bars(df, request, AdjustmentMode.NONE, symbol)

    def _fetch_fund_daily(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        df = self._call_with_retry(
            self._pro.fund_daily,
            ts_code=ts_code,
            start_date=str(request.start_date or ""),
            end_date=str(request.end_date or ""),
        )
        if df is None or df.empty:
            raise DataNotDownloaded(f"No fund_daily data for {ts_code}")
        return self._dataframe_to_bars(df, request, AdjustmentMode.NONE, symbol)

    def _fetch_qfq(
        self, ts_code: str, request: MarketDataRequest, symbol: str
    ) -> List[MarketBar]:
        assert self._ts is not None
        df = self._call_with_retry(
            self._ts.pro_bar,
            ts_code=ts_code,
            adj="qfq",
            start_date=str(request.start_date or ""),
            end_date=str(request.end_date or ""),
        )
        if df is None or df.empty:
            raise IncompleteResponse(f"No qfq data for {ts_code}")
        return self._dataframe_to_bars(df, request, AdjustmentMode.QFQ, symbol)

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
        for _, row in df.iterrows():
            try:
                trade_date = int(str(row["trade_date"]))
            except (KeyError, ValueError, TypeError):
                continue
            if request.start_date and trade_date < request.start_date:
                continue
            if request.end_date and trade_date > request.end_date:
                continue
            bars.append(
                MarketBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    period=request.period.value,
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("vol", row.get("volume", 0))),
                    amount=float(row.get("amount", 0)),
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
        ts_code: str,
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        trade_date: Optional[int] = None,
    ):
        """Fetch adj_factor for a stock or full market on a trade_date."""
        self._ensure_initialized()
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
