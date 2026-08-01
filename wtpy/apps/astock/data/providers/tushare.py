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
            if request.adjustment == AdjustmentMode.QFQ:
                sym_bars = self._fetch_qfq(ts_code, request)
            else:
                sym_bars = self._fetch_raw_daily(ts_code, request)
            bars.extend(sym_bars)
        return bars

    def _fetch_raw_daily(
        self, ts_code: str, request: MarketDataRequest
    ) -> List[MarketBar]:
        df = self._call_with_retry(
            self._pro.daily,
            ts_code=ts_code,
            start_date=str(request.start_date or ""),
            end_date=str(request.end_date or ""),
        )
        if df is None or df.empty:
            raise DataNotDownloaded(f"No daily data for {ts_code}")
        return self._dataframe_to_bars(df, ts_code, request, AdjustmentMode.NONE)

    def _fetch_qfq(
        self, ts_code: str, request: MarketDataRequest
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
        return self._dataframe_to_bars(df, ts_code, request, AdjustmentMode.QFQ)

    def _dataframe_to_bars(
        self,
        df,
        ts_code: str,
        request: MarketDataRequest,
        adjustment: AdjustmentMode,
    ) -> List[MarketBar]:
        bars: List[MarketBar] = []
        symbol = self._from_ts_code(ts_code)
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
        """Convert SSE.STK.600000 or 600000.SH to 600000.SH format."""
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
