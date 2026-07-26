"""Internal as-of qfq provider – wraps affine_adjust for causal forward adjustment."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..affine_adjust import AffineSeries, build_affine_series
from ..data_store import DataStore
from ..tdx_reader import DayBar
from .base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    InvalidSymbol,
    MarketBar,
    MarketDataRequest,
    ProviderCapabilities,
    ProviderUnavailable,
    UniverseEntry,
)


class InternalAsOfProvider:
    """Produces causal as-of forward-adjusted bars using the affine model.

    This wraps the existing affine_adjust.py + DataStore pipeline.
    source=internal, adjustment=asof_qfq.
    """

    def __init__(self, storage_root: Path | str, tdx_root: Path | str = r"D:\通达信"):
        self._store = DataStore(Path(storage_root))
        self._adj_root = Path(storage_root) / "adjustments"
        self._tdx_root = Path(tdx_root)

    def health_check(self) -> bool:
        return self._store.root.exists()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            source=DataSource.INTERNAL,
            adjustments=[AdjustmentMode.ASOF_QFQ],
            periods=[BarPeriod.DAY],
            supports_batch=False,
            max_batch_size=1,
            requires_client_online=False,
            supports_universe=False,
            supports_delisted=False,
            supports_bse=False,
        )

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        if request.adjustment != AdjustmentMode.ASOF_QFQ:
            raise InvalidSymbol(
                f"InternalAsOfProvider only supports asof_qfq, got {request.adjustment}"
            )
        if request.period != BarPeriod.DAY:
            raise InvalidSymbol(
                f"InternalAsOfProvider only supports period=1d, got {request.period}"
            )

        bars: List[MarketBar] = []
        for symbol in request.symbols:
            std_code = self._to_std_code(symbol)
            try:
                raw_bars = self._store.load_symbol(std_code)
            except FileNotFoundError:
                raise ProviderUnavailable(f"No local data for {symbol}")

            dates = [int(b.date) for b in raw_bars]
            if not dates:
                continue

            asof_date = request.anchor_date or dates[-1]
            affine = build_affine_series(
                std_code, dates, adj_root=self._adj_root
            )

            adjusted = self._apply_affine_asof(raw_bars, affine, dates, asof_date)
            for ab in adjusted:
                if request.start_date and ab.date < request.start_date:
                    continue
                if request.end_date and ab.date > request.end_date:
                    continue
                bars.append(
                    MarketBar(
                        symbol=symbol,
                        trade_date=ab.date,
                        period=request.period.value,
                        open=ab.open,
                        high=ab.high,
                        low=ab.low,
                        close=ab.close,
                        volume=ab.volume,
                        amount=ab.amount,
                        source=DataSource.INTERNAL.value,
                        adjustment=AdjustmentMode.ASOF_QFQ.value,
                        anchor_date=asof_date,
                        provider_version=self.provider_version(),
                    )
                )
        return bars

    def _apply_affine_asof(
        self,
        bars: List[DayBar],
        affine: AffineSeries,
        dates: List[int],
        asof_date: int,
    ) -> List[DayBar]:
        from ..affine_adjust import DividendEvent, compute_affine_params_asof

        events = [DividendEvent(**ev) for ev in affine.events]
        cum_a, cum_b = compute_affine_params_asof(events, dates, asof_date)

        result: List[DayBar] = []
        for i, bar in enumerate(bars):
            a = float(cum_a[i])
            b = float(cum_b[i])
            result.append(
                DayBar(
                    date=bar.date,
                    open=round(a * bar.open + b, 4),
                    high=round(a * bar.high + b, 4),
                    low=round(a * bar.low + b, 4),
                    close=round(a * bar.close + b, 4),
                    amount=bar.amount,
                    volume=bar.volume,
                    reserved=bar.reserved,
                )
            )
        return result

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        return []

    def provider_version(self) -> str:
        return "internal_asof_v1"

    @staticmethod
    def _to_std_code(symbol: str) -> str:
        if "." in symbol and symbol.count(".") == 2:
            return symbol
        from ..universe import to_std_code

        return to_std_code(symbol)
