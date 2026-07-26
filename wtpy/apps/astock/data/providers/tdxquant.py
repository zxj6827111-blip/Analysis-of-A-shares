"""TdxQuant provider – wraps tqcenter for front-adjusted bars from TDX client."""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .base import (
    AdjustmentMode,
    BarPeriod,
    DataNotDownloaded,
    DataSource,
    IncompleteResponse,
    InvalidSymbol,
    MarketBar,
    MarketDataRequest,
    NormalizationError,
    ProviderCapabilities,
    ProviderUnavailable,
    UniverseEntry,
)

logger = logging.getLogger(__name__)

TQCENTER_MODULE_VERSION = "1.0.3"
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 20
MAX_RETRIES = 3

_FIELD_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Amount": "amount",
}


def _load_tqcenter(tdx_root: Path):
    """Import tqcenter from the TDX PYPlugins directory."""
    tq_path = tdx_root / "PYPlugins" / "user" / "tqcenter.py"
    if not tq_path.exists():
        raise ProviderUnavailable(
            f"tqcenter.py not found at {tq_path}. Is TDX installed at {tdx_root}?"
        )
    spec = importlib.util.spec_from_file_location("tqcenter", str(tq_path))
    if spec is None or spec.loader is None:
        raise ProviderUnavailable(f"Cannot load tqcenter from {tq_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tqcenter"] = mod
    spec.loader.exec_module(mod)
    return mod


class TdxQuantProvider:
    """Fetches bars from the TDX client via tqcenter interface."""

    def __init__(
        self,
        tdx_root: Path | str = r"D:\通达信",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self._tdx_root = Path(tdx_root)
        self._batch_size = min(batch_size, MAX_BATCH_SIZE)
        self._tq = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        mod = _load_tqcenter(self._tdx_root)
        try:
            tq = mod.tq
            tq.initialize(str(self._tdx_root / "PYPlugins" / "user" / "probe.py"))
            self._tq = tq
            self._initialized = True
        except Exception as e:
            raise ProviderUnavailable(
                f"TdxQuant client initialization failed (client may be offline): {e}"
            )

    def health_check(self) -> bool:
        try:
            self._ensure_initialized()
            return True
        except ProviderUnavailable:
            return False

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source=DataSource.TDXQUANT,
            adjustments=[AdjustmentMode.NONE, AdjustmentMode.FRONT],
            periods=[BarPeriod.DAY, BarPeriod.WEEK],
            supports_batch=True,
            max_batch_size=MAX_BATCH_SIZE,
            requires_client_online=True,
            supports_universe=False,
            supports_delisted=False,
            supports_bse=False,
        )

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        self._ensure_initialized()

        if request.adjustment not in (AdjustmentMode.NONE, AdjustmentMode.FRONT):
            raise InvalidSymbol(
                f"TdxQuantProvider supports none/front, got {request.adjustment}"
            )
        if request.period not in (BarPeriod.DAY, BarPeriod.WEEK):
            raise InvalidSymbol(
                f"TdxQuantProvider supports 1d/1w, got {request.period}"
            )

        dividend_type = "front" if request.adjustment == AdjustmentMode.FRONT else "none"
        period_str = request.period.value

        all_bars: List[MarketBar] = []
        symbols = list(request.symbols)

        for i in range(0, len(symbols), self._batch_size):
            batch = symbols[i : i + self._batch_size]
            batch_bars = self._fetch_batch_with_retry(
                batch, period_str, dividend_type, request
            )
            all_bars.extend(batch_bars)

        return all_bars

    def _fetch_batch_with_retry(
        self,
        symbols: List[str],
        period: str,
        dividend_type: str,
        request: MarketDataRequest,
    ) -> List[MarketBar]:
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._fetch_batch(symbols, period, dividend_type, request)
            except (ProviderUnavailable, IncompleteResponse) as e:
                last_err = e
                if len(symbols) > 1:
                    logger.warning(
                        "Batch %s failed (attempt %d), splitting to single stocks: %s",
                        symbols,
                        attempt + 1,
                        e,
                    )
                    return self._fetch_singles(symbols, period, dividend_type, request)
                time.sleep(0.5 * (attempt + 1))
        raise last_err or ProviderUnavailable("Batch fetch failed after retries")

    def _fetch_singles(
        self,
        symbols: List[str],
        period: str,
        dividend_type: str,
        request: MarketDataRequest,
    ) -> List[MarketBar]:
        bars: List[MarketBar] = []
        failures: List[dict] = []
        for sym in symbols:
            try:
                bars.extend(
                    self._fetch_batch([sym], period, dividend_type, request)
                )
            except (ProviderUnavailable, DataNotDownloaded, InvalidSymbol) as e:
                failures.append({"symbol": sym, "error": str(e), "type": type(e).__name__})
                logger.warning("Single stock %s failed: %s", sym, e)
        if failures:
            raise IncompleteResponse(
                f"{len(failures)}/{len(symbols)} stocks failed: "
                + "; ".join(f"{f['symbol']}({f['type']})" for f in failures[:10])
            )
        return bars

    def _fetch_batch(
        self,
        symbols: List[str],
        period: str,
        dividend_type: str,
        request: MarketDataRequest,
    ) -> List[MarketBar]:
        assert self._tq is not None
        try:
            result = self._tq.get_market_data(
                stock_list=symbols,
                period=period,
                dividend_type=dividend_type,
                start_time=str(request.start_date or ""),
                end_time=str(request.end_date or ""),
            )
        except Exception as e:
            err_str = str(e).lower()
            if "login" in err_str or "登录" in err_str:
                raise ProviderUnavailable(f"TDX client not logged in: {e}")
            if "connect" in err_str or "连接" in err_str:
                raise ProviderUnavailable(f"TDX client not running: {e}")
            raise ProviderUnavailable(f"tqcenter call failed: {e}")

        if not result or not isinstance(result, dict):
            raise IncompleteResponse(
                f"Empty or invalid response for {symbols[:3]}... (period={period})"
            )

        return self._normalize_wide_table(result, symbols, period, dividend_type, request)

    def _normalize_wide_table(
        self,
        result: Dict,
        symbols: List[str],
        period: str,
        dividend_type: str,
        request: MarketDataRequest,
    ) -> List[MarketBar]:
        bars: List[MarketBar] = []
        cutoff = request.end_date

        close_df = result.get("Close")
        if close_df is None or (hasattr(close_df, "empty") and close_df.empty):
            raise IncompleteResponse("No Close data in response")

        dates = list(close_df.index)
        for sym in symbols:
            if sym not in close_df.columns:
                continue
            for dt in dates:
                try:
                    trade_date = int(str(dt)[:10].replace("-", ""))
                except (ValueError, TypeError):
                    continue
                if request.start_date and trade_date < request.start_date:
                    continue
                if request.end_date and trade_date > request.end_date:
                    continue

                o = self._safe_float(result.get("Open"), sym, dt)
                h = self._safe_float(result.get("High"), sym, dt)
                l = self._safe_float(result.get("Low"), sym, dt)
                c = self._safe_float(result.get("Close"), sym, dt)
                v = self._safe_float(result.get("Volume"), sym, dt)
                a = self._safe_float(result.get("Amount"), sym, dt)

                if c is None or np.isnan(c) or c <= 0:
                    continue

                bars.append(
                    MarketBar(
                        symbol=sym,
                        trade_date=trade_date,
                        period=period,
                        open=o if o is not None and not np.isnan(o) else c,
                        high=h if h is not None and not np.isnan(h) else c,
                        low=l if l is not None and not np.isnan(l) else c,
                        close=c,
                        volume=v if v is not None and not np.isnan(v) else 0.0,
                        amount=a if a is not None and not np.isnan(a) else 0.0,
                        source=DataSource.TDXQUANT.value,
                        adjustment=dividend_type,
                        anchor_date=request.anchor_date,
                        data_cutoff_date=cutoff,
                        provider_version=self.provider_version(),
                    )
                )
        return bars

    @staticmethod
    def _safe_float(df, col, idx) -> Optional[float]:
        if df is None:
            return None
        try:
            val = df.loc[idx, col]
            return float(val)
        except (KeyError, TypeError, ValueError):
            return None

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        return []

    def provider_version(self) -> str:
        return f"tdxquant_tqcenter_{TQCENTER_MODULE_VERSION}"
