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


def _to_tq_symbol(symbol: str) -> str:
    """Convert internal format (SSE.STK.600000) to tqcenter format (600000.SH)."""
    parts = symbol.split(".")
    if len(parts) == 3:
        exch, _, code = parts
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exch, exch)
        return f"{code}.{suffix}"
    return symbol


def _from_tq_symbol(symbol: str) -> str:
    """Convert tqcenter format (600000.SH) to internal format (SSE.STK.600000)."""
    parts = symbol.split(".")
    if len(parts) == 2 and parts[1] in ("SH", "SZ", "BJ"):
        code, suffix = parts
        exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[suffix]
        return f"{exch}.STK.{code}"
    return symbol
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 20
MAX_RETRIES = 3

# tqcenter returns Amount in 万元 — scaled x10000 here so the stored unit
# is 元 (repository convention). Volume is shares (股), unadjusted, as-is.
AMOUNT_UNIT_SCALE = 10000.0


def read_tqcenter_version(tdx_root: Path | str) -> str:
    """Read the Version string from tqcenter.py's header docstring."""
    import re
    tq_path = Path(tdx_root) / "PYPlugins" / "user" / "tqcenter.py"
    try:
        m = re.search(r"Version:\s*([0-9.]+)",
                      tq_path.read_text(encoding="utf-8", errors="ignore"))
        return m.group(1) if m else "unknown"
    except OSError:
        return "unavailable"

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
        self._tqcenter_version: Optional[str] = None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        mod = _load_tqcenter(self._tdx_root)
        self._tqcenter_version = read_tqcenter_version(self._tdx_root)
        try:
            tq = mod.tq
            # The path acts as the strategy/connection name inside the TDX
            # client. A killed process never calls CloseConnect, and a dead
            # fixed name blocks every later InitConnect (live-verified) —
            # so the name must be unique per process for crash recovery.
            import itertools
            import os as _os
            global _CONN_SEQ
            try:
                seq = next(_CONN_SEQ)
            except NameError:
                _CONN_SEQ = itertools.count(1)
                seq = next(_CONN_SEQ)
            conn_name = f"astock_sync_{_os.getpid()}_{int(time.time())}_{seq}.py"
            self._connection_name = conn_name
            tq.initialize(str(self._tdx_root / "PYPlugins" / "user" / conn_name))
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
            # live-verified 2026-07-26: current BSE (920-segment) codes are
            # served; delisted stocks return no data in every request shape.
            supports_delisted=False,
            supports_bse=True,
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
            except (ProviderUnavailable, DataNotDownloaded, InvalidSymbol,
                    IncompleteResponse) as e:
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
        tq_symbols = [_to_tq_symbol(s) for s in symbols]
        try:
            result = self._tq.get_market_data(
                stock_list=tq_symbols,
                period=period,
                dividend_type=dividend_type,
                start_time=str(request.start_date or ""),
                end_time=str(request.end_date or ""),
                fill_data=False,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "login" in err_str or "登录" in err_str:
                raise ProviderUnavailable(f"TDX client not logged in: {e}")
            if "connect" in err_str or "连接" in err_str:
                raise ProviderUnavailable(f"TDX client not running: {e}")
            raise ProviderUnavailable(f"tqcenter call failed: {e}")

        if not result or not isinstance(result, dict):
            if len(symbols) == 1:
                return []
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

        trade_dates = np.array(
            [int(str(dt)[:10].replace("-", "")) for dt in close_df.index],
            dtype=np.int64,
        )
        pv = self.provider_version()

        def _col(field: str, sym: str):
            df = result.get(field)
            if df is None or sym not in getattr(df, "columns", []):
                return None
            return df[sym].to_numpy(dtype=np.float64)

        # TDX front adjustment is affine (a*p+b): early prices of long-history
        # high-dividend stocks are legitimately <= 0, exactly as the client
        # displays them. Only raw (none) bars must be strictly positive.
        require_positive = dividend_type != "front"

        for sym in symbols:
            tq_sym = _to_tq_symbol(sym)
            c_arr = _col("Close", tq_sym)
            if c_arr is None:
                continue
            mask = ~np.isnan(c_arr)
            if require_positive:
                mask &= c_arr > 0
            if request.start_date:
                mask &= trade_dates >= int(request.start_date)
            if request.end_date:
                mask &= trade_dates <= int(request.end_date)
            if not mask.any():
                continue

            idx = np.nonzero(mask)[0]
            c_v = c_arr[idx]

            def _pick(field: str, fallback: np.ndarray) -> np.ndarray:
                arr = _col(field, tq_sym)
                if arr is None:
                    return fallback.copy()
                sel = arr[idx]
                nan = np.isnan(sel)
                if nan.any():
                    sel = np.where(nan, fallback, sel)
                return sel

            o_v = _pick("Open", c_v)
            h_v = _pick("High", c_v)
            l_v = _pick("Low", c_v)
            zeros = np.zeros_like(c_v)
            v_v = _pick("Volume", zeros)
            a_v = _pick("Amount", zeros) * AMOUNT_UNIT_SCALE
            d_v = trade_dates[idx]

            bars.extend(
                MarketBar(
                    symbol=sym,
                    trade_date=int(d_v[i]),
                    period=period,
                    open=float(o_v[i]),
                    high=float(h_v[i]),
                    low=float(l_v[i]),
                    close=float(c_v[i]),
                    volume=float(v_v[i]),
                    amount=float(a_v[i]),
                    source=DataSource.TDXQUANT.value,
                    adjustment=dividend_type,
                    anchor_date=request.anchor_date,
                    data_cutoff_date=cutoff,
                    provider_version=pv,
                )
                for i in range(len(idx))
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
        return f"tdxquant_tqcenter_{self._tqcenter_version or TQCENTER_MODULE_VERSION}"

    def tqcenter_version(self) -> str:
        if self._tqcenter_version is None:
            self._tqcenter_version = read_tqcenter_version(self._tdx_root)
        return self._tqcenter_version
