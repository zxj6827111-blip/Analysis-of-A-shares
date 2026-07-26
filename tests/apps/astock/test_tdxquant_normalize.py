"""Tests for TdxQuant normalization (wide table → MarketBar)."""
import pytest
from unittest.mock import patch, MagicMock
import numpy as np

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketDataRequest,
    InvalidSymbol,
)


@pytest.fixture
def provider(tmp_path):
    return TdxQuantProvider(tdx_root=tmp_path, batch_size=10)


class TestTdxQuantNormalization:
    def test_capabilities(self, provider):
        caps = provider.capabilities()
        assert caps.source == DataSource.TDXQUANT
        assert AdjustmentMode.FRONT in caps.adjustments
        assert AdjustmentMode.NONE in caps.adjustments
        assert BarPeriod.DAY in caps.periods
        assert BarPeriod.WEEK in caps.periods
        assert caps.requires_client_online is True
        assert caps.supports_batch is True

    def test_rejects_qfq_adjustment(self, provider):
        provider._initialized = True
        provider._tq = MagicMock()
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            adjustment=AdjustmentMode.QFQ,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_rejects_month_period(self, provider):
        provider._initialized = True
        provider._tq = MagicMock()
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.MONTH,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_normalize_wide_table(self, provider):
        dates = pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-03"])
        result = {
            "Open": pd.DataFrame({"301107.SZ": [20.0, 20.1, 20.2]}, index=dates),
            "High": pd.DataFrame({"301107.SZ": [20.5, 20.6, 20.7]}, index=dates),
            "Low": pd.DataFrame({"301107.SZ": [19.5, 19.6, 19.7]}, index=dates),
            "Close": pd.DataFrame({"301107.SZ": [20.3, 20.4, 20.5]}, index=dates),
            "Volume": pd.DataFrame({"301107.SZ": [1000, 1100, 1200]}, index=dates),
            "Amount": pd.DataFrame({"301107.SZ": [20000, 21000, 22000]}, index=dates),
        }
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.FRONT,
        )
        bars = provider._normalize_wide_table(
            result, ["301107.SZ"], "1d", "front", req
        )
        assert len(bars) == 3
        assert bars[0].symbol == "301107.SZ"
        assert bars[0].trade_date == 20240701
        assert bars[0].open == 20.0
        assert bars[0].close == 20.3
        assert bars[0].source == "tdxquant"
        assert bars[0].adjustment == "front"

    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_normalize_date_filter(self, provider):
        dates = pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-03"])
        result = {
            "Open": pd.DataFrame({"301107.SZ": [20.0, 20.1, 20.2]}, index=dates),
            "High": pd.DataFrame({"301107.SZ": [20.5, 20.6, 20.7]}, index=dates),
            "Low": pd.DataFrame({"301107.SZ": [19.5, 19.6, 19.7]}, index=dates),
            "Close": pd.DataFrame({"301107.SZ": [20.3, 20.4, 20.5]}, index=dates),
            "Volume": pd.DataFrame({"301107.SZ": [1000, 1100, 1200]}, index=dates),
            "Amount": pd.DataFrame({"301107.SZ": [20000, 21000, 22000]}, index=dates),
        }
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.FRONT,
            start_date=20240702,
        )
        bars = provider._normalize_wide_table(
            result, ["301107.SZ"], "1d", "front", req
        )
        assert len(bars) == 2
        assert bars[0].trade_date == 20240702

    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_normalize_skips_nan_close(self, provider):
        dates = pd.DatetimeIndex(["2024-07-01", "2024-07-02"])
        result = {
            "Open": pd.DataFrame({"301107.SZ": [20.0, np.nan]}, index=dates),
            "High": pd.DataFrame({"301107.SZ": [20.5, np.nan]}, index=dates),
            "Low": pd.DataFrame({"301107.SZ": [19.5, np.nan]}, index=dates),
            "Close": pd.DataFrame({"301107.SZ": [20.3, np.nan]}, index=dates),
            "Volume": pd.DataFrame({"301107.SZ": [1000, 0]}, index=dates),
            "Amount": pd.DataFrame({"301107.SZ": [20000, 0]}, index=dates),
        }
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.FRONT,
        )
        bars = provider._normalize_wide_table(
            result, ["301107.SZ"], "1d", "front", req
        )
        assert len(bars) == 1

    def test_provider_version(self, provider):
        assert "tdxquant" in provider.provider_version()
        assert "1.0.3" in provider.provider_version()

    def test_batch_size_capped(self, tmp_path):
        p = TdxQuantProvider(tdx_root=tmp_path, batch_size=100)
        assert p._batch_size == 20
