"""Tests for TdxLocalProvider."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from wtpy.apps.astock.data.providers.tdx_local import TdxLocalProvider
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    DataNotDownloaded,
    InvalidSymbol,
    MarketDataRequest,
)
from wtpy.apps.astock.data.tdx_reader import DayBar


@pytest.fixture
def provider(tmp_path):
    return TdxLocalProvider(tdx_root=tmp_path)


class TestTdxLocalProvider:
    def test_capabilities(self, provider):
        caps = provider.capabilities()
        assert caps.source == DataSource.TDX_LOCAL
        assert AdjustmentMode.NONE in caps.adjustments
        assert BarPeriod.DAY in caps.periods
        assert caps.requires_client_online is False

    def test_health_check_missing_root(self, tmp_path):
        p = TdxLocalProvider(tdx_root=tmp_path / "nonexistent")
        assert p.health_check() is False

    def test_health_check_existing_root(self, tmp_path):
        p = TdxLocalProvider(tdx_root=tmp_path)
        assert p.health_check() is True

    def test_rejects_non_none_adjustment(self, provider):
        req = MarketDataRequest(
            symbols=["sh600000"],
            adjustment=AdjustmentMode.FRONT,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_rejects_non_day_period(self, provider):
        req = MarketDataRequest(
            symbols=["sh600000"],
            period=BarPeriod.WEEK,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_fetch_bars_missing_symbol(self, provider):
        req = MarketDataRequest(symbols=["sh999999"])
        with pytest.raises(DataNotDownloaded):
            provider.fetch_bars(req)

    def test_fetch_bars_with_mock(self, provider):
        fake_bars = [
            DayBar(date=20240101, open=10.0, high=11.0, low=9.0, close=10.5, amount=100.0, volume=1000.0),
            DayBar(date=20240102, open=10.5, high=11.5, low=10.0, close=11.0, amount=110.0, volume=1100.0),
        ]
        with patch.object(provider._reader, "read", return_value=(fake_bars, [])):
            req = MarketDataRequest(symbols=["sh600000"])
            bars = provider.fetch_bars(req)
            assert len(bars) == 2
            assert bars[0].source == "tdx_local"
            assert bars[0].adjustment == "none"
            assert bars[0].open == 10.0

    def test_fetch_bars_date_filter(self, provider):
        fake_bars = [
            DayBar(date=20240101, open=10.0, high=11.0, low=9.0, close=10.5, amount=100.0, volume=1000.0),
            DayBar(date=20240115, open=10.5, high=11.5, low=10.0, close=11.0, amount=110.0, volume=1100.0),
        ]
        with patch.object(provider._reader, "read", return_value=(fake_bars, [])):
            req = MarketDataRequest(symbols=["sh600000"], start_date=20240110)
            bars = provider.fetch_bars(req)
            assert len(bars) == 1
            assert bars[0].trade_date == 20240115

    def test_provider_version(self, provider):
        assert provider.provider_version() == "tdx_local_v1"
