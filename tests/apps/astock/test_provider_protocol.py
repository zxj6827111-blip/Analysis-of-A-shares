"""Tests for the Provider protocol and domain model."""
import pytest

from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketBar,
    MarketDataRequest,
    MarketDataProvider,
    ProviderCapabilities,
    UniverseEntry,
    WeeklyBarMode,
    ProviderError,
    ProviderUnavailable,
    AuthenticationError,
    PermissionDenied,
    RateLimited,
    DataNotDownloaded,
    InvalidSymbol,
    IncompleteResponse,
    NormalizationError,
    SIGNAL_SOURCE_ADJUSTMENT,
)


class TestEnums:
    def test_data_source_values(self):
        assert DataSource.TDXQUANT.value == "tdxquant"
        assert DataSource.TUSHARE.value == "tushare"
        assert DataSource.INTERNAL.value == "internal"
        assert DataSource.TDX_LOCAL.value == "tdx_local"
        assert DataSource.LEGACY_TDX_LOCAL_ASOF.value == "legacy_tdx_local_asof"

    def test_adjustment_mode_values(self):
        assert AdjustmentMode.NONE.value == "none"
        assert AdjustmentMode.FRONT.value == "front"
        assert AdjustmentMode.QFQ.value == "qfq"
        assert AdjustmentMode.ASOF_QFQ.value == "asof_qfq"

    def test_bar_period_values(self):
        assert BarPeriod.DAY.value == "1d"
        assert BarPeriod.WEEK.value == "1w"
        assert BarPeriod.MONTH.value == "1mon"

    def test_weekly_bar_mode_values(self):
        assert WeeklyBarMode.LOCAL_AGGREGATE.value == "local_aggregate"
        assert WeeklyBarMode.VENDOR_NATIVE.value == "vendor_native"

    def test_signal_source_adjustment_mapping(self):
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.TDXQUANT] == AdjustmentMode.FRONT
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.TUSHARE] == AdjustmentMode.QFQ
        assert SIGNAL_SOURCE_ADJUSTMENT[DataSource.INTERNAL] == AdjustmentMode.ASOF_QFQ


class TestMarketBar:
    def test_creation(self):
        bar = MarketBar(
            symbol="SSE.STK.600000",
            trade_date=20240101,
            period="1d",
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1000.0,
            amount=10500.0,
            source="tdxquant",
            adjustment="front",
        )
        assert bar.symbol == "SSE.STK.600000"
        assert bar.trade_date == 20240101
        assert bar.source == "tdxquant"
        assert bar.adjustment == "front"

    def test_to_dict(self):
        bar = MarketBar(
            symbol="SZSE.STK.000001",
            trade_date=20240101,
            period="1d",
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1000.0,
            amount=10500.0,
        )
        d = bar.to_dict()
        assert d["symbol"] == "SZSE.STK.000001"
        assert d["open"] == 10.0

    def test_frozen(self):
        bar = MarketBar(
            symbol="X", trade_date=1, period="1d",
            open=1, high=1, low=1, close=1, volume=1, amount=1,
        )
        with pytest.raises(Exception):
            bar.open = 99


class TestMarketDataRequest:
    def test_defaults(self):
        req = MarketDataRequest(symbols=["A", "B"])
        assert req.period == BarPeriod.DAY
        assert req.adjustment == AdjustmentMode.NONE
        assert req.start_date is None
        assert req.end_date is None
        assert req.anchor_date is None
        assert req.fields is None


class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(ProviderUnavailable, ProviderError)
        assert issubclass(AuthenticationError, ProviderError)
        assert issubclass(PermissionDenied, ProviderError)
        assert issubclass(RateLimited, ProviderError)
        assert issubclass(DataNotDownloaded, ProviderError)
        assert issubclass(InvalidSymbol, ProviderError)
        assert issubclass(IncompleteResponse, ProviderError)
        assert issubclass(NormalizationError, ProviderError)

    def test_rate_limited_retry_after(self):
        e = RateLimited("too fast", retry_after=5.0)
        assert e.retry_after == 5.0


class TestProviderProtocol:
    def test_protocol_is_runtime_checkable(self):
        class FakeProvider:
            def health_check(self): return True
            def capabilities(self): return ProviderCapabilities(source=DataSource.TDX_LOCAL)
            def fetch_bars(self, request): return []
            def fetch_universe(self, **kw): return []
            def provider_version(self): return "fake_v1"

        assert isinstance(FakeProvider(), MarketDataProvider)


class TestUniverseEntry:
    def test_to_dict(self):
        e = UniverseEntry(symbol="SSE.STK.600000", name="浦发银行", exchange="SSE")
        d = e.to_dict()
        assert d["symbol"] == "SSE.STK.600000"
        assert d["status"] == "listed"
