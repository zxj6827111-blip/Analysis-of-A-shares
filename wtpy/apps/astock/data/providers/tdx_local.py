"""TDX local .day file provider – wraps existing TdxDayReader + DataStore."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..tdx_reader import DayBar, TdxDayReader
from .base import (
    AdjustmentMode,
    BarPeriod,
    DataNotDownloaded,
    DataSource,
    InvalidSymbol,
    MarketBar,
    MarketDataRequest,
    ProviderCapabilities,
    ProviderUnavailable,
    UniverseEntry,
)


class TdxLocalProvider:
    """Reads unadjusted daily bars from local TDX .day files."""

    def __init__(self, tdx_root: Path | str = r"D:\通达信"):
        self._reader = TdxDayReader(tdx_root)
        self._tdx_root = Path(tdx_root)

    @staticmethod
    def _to_reader_code(symbol: str) -> str:
        """Convert any symbol format to TdxDayReader-compatible code (sh600000/sz000001/bj430047)."""
        parts = symbol.split(".")
        if len(parts) == 3:
            exch, _, code = parts
            prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exch)
            if prefix:
                return f"{prefix}{code}"
        if len(parts) == 2:
            code, suffix = parts
            prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix.upper())
            if prefix:
                return f"{prefix}{code}"
        lower = symbol.lower()
        if len(lower) == 8 and lower[:2] in ("sh", "sz", "bj") and lower[2:].isdigit():
            return lower
        if symbol.isdigit() and len(symbol) == 6:
            if symbol.startswith(("5", "6", "9")):
                return f"sh{symbol}"
            if symbol.startswith(("4", "8")):
                return f"bj{symbol}"
            return f"sz{symbol}"
        return symbol

    def health_check(self) -> bool:
        return self._tdx_root.exists()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source=DataSource.TDX_LOCAL,
            adjustments=[AdjustmentMode.NONE],
            periods=[BarPeriod.DAY],
            supports_batch=False,
            max_batch_size=1,
            requires_client_online=False,
            supports_universe=True,
            supports_delisted=True,
            supports_bse=True,
        )

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        if request.adjustment != AdjustmentMode.NONE:
            raise InvalidSymbol(
                f"TdxLocalProvider only supports adjustment=none, got {request.adjustment}"
            )
        if request.period != BarPeriod.DAY:
            raise InvalidSymbol(
                f"TdxLocalProvider only supports period=1d, got {request.period}"
            )
        bars: List[MarketBar] = []
        for symbol in request.symbols:
            reader_code = self._to_reader_code(symbol)
            try:
                day_bars, _ = self._reader.read(reader_code)
            except FileNotFoundError:
                raise DataNotDownloaded(f"Symbol {symbol} not found in TDX local files")
            except Exception as e:
                raise ProviderUnavailable(f"Failed to read {symbol}: {e}")
            for db in day_bars:
                if request.start_date and db.date < request.start_date:
                    continue
                if request.end_date and db.date > request.end_date:
                    continue
                bars.append(
                    MarketBar(
                        symbol=symbol,
                        trade_date=db.date,
                        period=request.period.value,
                        open=db.open,
                        high=db.high,
                        low=db.low,
                        close=db.close,
                        volume=db.volume,
                        amount=db.amount,
                        source=DataSource.TDX_LOCAL.value,
                        adjustment=AdjustmentMode.NONE.value,
                        provider_version=self.provider_version(),
                    )
                )
        return bars

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        from ..universe import AShareUniverse

        uni = AShareUniverse.from_tdx_dirs(
            self._reader.sh_dir,
            self._reader.sz_dir,
            include_bj=include_bse,
            bj_dir=self._reader.bj_dir,
        )
        entries = []
        for s in uni.symbols:
            entries.append(
                UniverseEntry(
                    symbol=s.std_code,
                    name=s.name,
                    exchange=s.exchange,
                    status="listed",
                    source=DataSource.TDX_LOCAL.value,
                )
            )
        return entries

    def provider_version(self) -> str:
        return "tdx_local_v1"
