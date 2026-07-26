"""Tests that providers never silently fall back to another source."""
import pytest
from unittest.mock import patch, MagicMock

from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketDataRequest,
    PermissionDenied,
    ProviderUnavailable,
    DataNotDownloaded,
    InvalidSymbol,
)
from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
from wtpy.apps.astock.data.providers.tushare import TushareProvider
from wtpy.apps.astock.data.providers.tdx_local import TdxLocalProvider


class TestNoSilentFallback:
    def test_tdxquant_failure_raises_not_fallback(self, tmp_path):
        provider = TdxQuantProvider(tdx_root=tmp_path)
        provider._initialized = True
        provider._tq = MagicMock()
        provider._tq.get_market_data.side_effect = Exception("client offline")

        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.FRONT,
        )
        with pytest.raises(ProviderUnavailable):
            provider.fetch_bars(req)

    def test_tushare_failure_raises_not_fallback(self):
        provider = TushareProvider(token="fake")
        provider._initialized = True
        provider._pro = MagicMock()
        provider._pro.daily.side_effect = PermissionDenied("no access")

        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
        )
        with pytest.raises(PermissionDenied):
            provider.fetch_bars(req)

    def test_tdx_local_missing_raises_not_fallback(self, tmp_path):
        provider = TdxLocalProvider(tdx_root=tmp_path)
        req = MarketDataRequest(symbols=["sh999999"])
        with pytest.raises(DataNotDownloaded):
            provider.fetch_bars(req)

    def test_wrong_adjustment_raises_not_silent_switch(self, tmp_path):
        provider = TdxLocalProvider(tdx_root=tmp_path)
        req = MarketDataRequest(
            symbols=["sh600000"],
            adjustment=AdjustmentMode.FRONT,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_tdxquant_does_not_call_tushare(self, tmp_path):
        provider = TdxQuantProvider(tdx_root=tmp_path)
        provider._initialized = True
        provider._tq = MagicMock()
        provider._tq.get_market_data.side_effect = Exception("offline")

        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.FRONT,
        )
        with patch.object(TushareProvider, "fetch_bars") as mock_ts:
            with pytest.raises(ProviderUnavailable):
                provider.fetch_bars(req)
            mock_ts.assert_not_called()

    def test_tushare_does_not_call_tdxquant(self):
        provider = TushareProvider(token="fake")
        provider._initialized = True
        provider._pro = MagicMock()
        provider._pro.daily.side_effect = Exception("network error")

        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
        )
        with patch.object(TdxQuantProvider, "fetch_bars") as mock_tq:
            with pytest.raises(Exception):
                provider.fetch_bars(req)
            mock_tq.assert_not_called()
