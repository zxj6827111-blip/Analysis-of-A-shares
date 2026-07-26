"""Tests for Tushare normalization."""
import pytest
from unittest.mock import patch, MagicMock

from wtpy.apps.astock.data.providers.tushare import TushareProvider
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    InvalidSymbol,
    MarketDataRequest,
    PermissionDenied,
    RateLimited,
)


@pytest.fixture
def provider():
    p = TushareProvider(token="fake_token_for_test")
    p._initialized = True
    p._pro = MagicMock()
    p._ts = MagicMock()
    return p


class TestTushareNormalization:
    def test_capabilities(self, provider):
        caps = provider.capabilities()
        assert caps.source == DataSource.TUSHARE
        assert AdjustmentMode.NONE in caps.adjustments
        assert AdjustmentMode.QFQ in caps.adjustments
        assert BarPeriod.DAY in caps.periods
        assert caps.requires_client_online is False
        assert caps.supports_delisted is True

    def test_rejects_front_adjustment(self, provider):
        req = MarketDataRequest(
            symbols=["600000.SH"],
            adjustment=AdjustmentMode.FRONT,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_rejects_week_period(self, provider):
        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.WEEK,
        )
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(req)

    def test_to_ts_code_from_std(self):
        assert TushareProvider._to_ts_code("SSE.STK.600000") == "600000.SH"
        assert TushareProvider._to_ts_code("SZSE.STK.000001") == "000001.SZ"
        assert TushareProvider._to_ts_code("BSE.STK.430047") == "430047.BJ"

    def test_to_ts_code_from_dotted(self):
        assert TushareProvider._to_ts_code("600000.SH") == "600000.SH"
        assert TushareProvider._to_ts_code("000001.SZ") == "000001.SZ"

    def test_to_ts_code_from_bare(self):
        assert TushareProvider._to_ts_code("600000") == "600000.SH"
        assert TushareProvider._to_ts_code("000001") == "000001.SZ"

    def test_from_ts_code(self):
        assert TushareProvider._from_ts_code("600000.SH") == "SSE.STK.600000"
        assert TushareProvider._from_ts_code("000001.SZ") == "SZSE.STK.000001"
        assert TushareProvider._from_ts_code("430047.BJ") == "BSE.STK.430047"

    def test_parse_date(self):
        assert TushareProvider._parse_date("20240101") == 20240101
        assert TushareProvider._parse_date(None) is None
        assert TushareProvider._parse_date("bad") is None

    def test_dataframe_to_bars(self, provider):
        import pandas as pd
        df = pd.DataFrame({
            "trade_date": ["20240101", "20240102"],
            "open": [10.0, 10.5],
            "high": [11.0, 11.5],
            "low": [9.5, 10.0],
            "close": [10.5, 11.0],
            "vol": [1000.0, 1100.0],
            "amount": [10500.0, 11500.0],
        })
        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
        )
        bars = provider._dataframe_to_bars(df, "600000.SH", req, AdjustmentMode.NONE)
        assert len(bars) == 2
        assert bars[0].symbol == "SSE.STK.600000"
        assert bars[0].trade_date == 20240101
        assert bars[0].source == "tushare"
        assert bars[0].adjustment == "none"

    def test_dataframe_to_bars_date_filter(self, provider):
        import pandas as pd
        df = pd.DataFrame({
            "trade_date": ["20240101", "20240102", "20240103"],
            "open": [10.0, 10.5, 11.0],
            "high": [11.0, 11.5, 12.0],
            "low": [9.5, 10.0, 10.5],
            "close": [10.5, 11.0, 11.5],
            "vol": [1000.0, 1100.0, 1200.0],
            "amount": [10500.0, 11500.0, 12500.0],
        })
        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.QFQ,
            start_date=20240102,
            end_date=20240102,
        )
        bars = provider._dataframe_to_bars(df, "600000.SH", req, AdjustmentMode.QFQ)
        assert len(bars) == 1
        assert bars[0].trade_date == 20240102
        assert bars[0].adjustment == "qfq"

    def test_provider_version(self, provider):
        v = provider.provider_version()
        assert "tushare" in v

    def test_no_token_raises(self):
        p = TushareProvider(token=None)
        with patch("wtpy.apps.astock.data.providers.tushare.TushareProvider._ensure_initialized") as mock_init:
            from wtpy.apps.astock.data.providers.base import AuthenticationError
            mock_init.side_effect = AuthenticationError("no token")
            with pytest.raises(AuthenticationError):
                p.fetch_bars(MarketDataRequest(symbols=["600000.SH"]))

    def test_token_not_in_logs(self, provider, caplog):
        import logging
        provider._token = "super_secret_token_12345"
        with caplog.at_level(logging.DEBUG):
            try:
                provider._call_with_retry(
                    lambda **kw: (_ for _ in ()).throw(Exception("network error")),
                    ts_code="600000.SH",
                )
            except Exception:
                pass
        assert "super_secret_token_12345" not in caplog.text

    def test_token_not_in_error_messages(self):
        p = TushareProvider(token="my_secret_token_xyz")
        p._initialized = True
        p._pro = MagicMock()
        p._pro.daily.side_effect = Exception("connection refused")
        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
        )
        try:
            p.fetch_bars(req)
        except Exception as e:
            assert "my_secret_token_xyz" not in str(e)


@pytest.mark.live_tushare
class TestTushareLive:
    """Live tests requiring real Tushare token. Run with: pytest -m live_tushare"""

    def test_live_daily_fetch(self):
        provider = TushareProvider()
        if not provider.health_check():
            pytest.skip("Tushare API not available")
        req = MarketDataRequest(
            symbols=["600000.SH"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
            start_date=20240101,
            end_date=20240131,
        )
        bars = provider.fetch_bars(req)
        assert len(bars) > 0
        assert bars[0].source == "tushare"

    def test_live_universe_delisted(self):
        provider = TushareProvider()
        if not provider.health_check():
            pytest.skip("Tushare API not available")
        entries = provider.fetch_universe(include_delisted=True)
        delisted = [e for e in entries if e.status == "delisted"]
        assert len(delisted) > 0
