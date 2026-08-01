"""Unified domain model and Provider protocol for multi-source market data.

This module defines the shared vocabulary used by every MarketDataProvider
implementation and by the MarketDataRepository.  Business layers must only
ever see ``MarketBar`` instances – never raw vendor DataFrames or dicts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Protocol, Sequence, runtime_checkable


class DataSource(str, Enum):
    LEGACY_TDX_LOCAL_ASOF = "legacy_tdx_local_asof"
    TDX_LOCAL = "tdx_local"
    LOCAL_VENDOR = "local_vendor"
    TDXQUANT = "tdxquant"
    TUSHARE = "tushare"
    INTERNAL = "internal"


class AdjustmentMode(str, Enum):
    NONE = "none"
    FRONT = "front"
    QFQ = "qfq"
    ASOF_QFQ = "asof_qfq"


class BarPeriod(str, Enum):
    DAY = "1d"
    WEEK = "1w"
    MONTH = "1mon"


class WeeklyBarMode(str, Enum):
    LOCAL_AGGREGATE = "local_aggregate"
    VENDOR_NATIVE = "vendor_native"


SIGNAL_SOURCE_ADJUSTMENT: Dict[DataSource, AdjustmentMode] = {
    DataSource.TDXQUANT: AdjustmentMode.FRONT,
    DataSource.TUSHARE: AdjustmentMode.QFQ,
    DataSource.INTERNAL: AdjustmentMode.ASOF_QFQ,
}


class ProviderError(Exception):
    """Base class for all provider errors."""


class ProviderUnavailable(ProviderError):
    """The external service / client is not reachable."""


class AuthenticationError(ProviderError):
    """Credentials are missing or rejected."""


class PermissionDenied(ProviderError):
    """Authenticated but not authorised for the requested data."""


class RateLimited(ProviderError):
    """Too many requests – caller should back off and retry."""

    def __init__(self, message: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class DataNotDownloaded(ProviderError):
    """The requested data has not been downloaded to the local client."""


class InvalidSymbol(ProviderError):
    """The symbol is not recognised by the provider."""


class IncompleteResponse(ProviderError):
    """The provider returned data but it is incomplete or truncated."""


class NormalizationError(ProviderError):
    """The provider returned data that could not be normalized to MarketBar."""


@dataclass(frozen=True)
class MarketBar:
    """A single normalized bar with full provenance metadata."""

    symbol: str
    trade_date: int
    period: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str = ""
    adjustment: str = ""
    anchor_date: Optional[int] = None
    snapshot_date: Optional[int] = None
    data_cutoff_date: Optional[int] = None
    provider_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketDataRequest:
    """Unified request accepted by every provider."""

    symbols: List[str]
    period: BarPeriod = BarPeriod.DAY
    adjustment: AdjustmentMode = AdjustmentMode.NONE
    start_date: Optional[int] = None
    end_date: Optional[int] = None
    anchor_date: Optional[int] = None
    fields: Optional[List[str]] = None


@dataclass
class ProviderCapabilities:
    """Static description of what a provider can serve."""

    source: DataSource
    adjustments: List[AdjustmentMode] = field(default_factory=list)
    periods: List[BarPeriod] = field(default_factory=list)
    supports_batch: bool = False
    max_batch_size: int = 1
    requires_client_online: bool = False
    supports_universe: bool = False
    supports_delisted: bool = False
    supports_bse: bool = False


@dataclass
class UniverseEntry:
    """A single symbol in the provider's universe."""

    symbol: str
    name: str = ""
    exchange: str = ""
    list_date: Optional[int] = None
    delist_date: Optional[int] = None
    status: str = "listed"
    source: str = ""
    first_market_date: Optional[int] = None
    last_market_date: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Protocol that every data-source provider must satisfy."""

    def health_check(self) -> bool:
        """Return True when the provider is reachable and usable."""
        ...

    def capabilities(self) -> ProviderCapabilities:
        """Return static capability description."""
        ...

    def fetch_bars(self, request: MarketDataRequest) -> List[MarketBar]:
        """Fetch normalized bars.  Must raise typed ProviderError on failure."""
        ...

    def fetch_universe(
        self,
        *,
        include_delisted: bool = False,
        include_bse: bool = False,
    ) -> List[UniverseEntry]:
        """Return the symbol universe known to this provider."""
        ...

    def provider_version(self) -> str:
        """Return a version string identifying the provider + upstream SDK."""
        ...
