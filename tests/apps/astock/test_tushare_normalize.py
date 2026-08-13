"""Tests for Tushare normalization."""
import pytest
from unittest.mock import patch, MagicMock

from wtpy.apps.astock.data.providers.tushare import TushareProvider, _symbol_kind
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

    def test_to_ts_code_index_etf(self):
        assert TushareProvider._to_ts_code("sh000001") == "000001.SH"
        assert TushareProvider._to_ts_code("sz399001") == "399001.SZ"
        assert TushareProvider._to_ts_code("SSE.IDX.000001") == "000001.SH"
        assert TushareProvider._to_ts_code("SZSE.IDX.399001") == "399001.SZ"
        assert TushareProvider._to_ts_code("sh510300") == "510300.SH"
        assert TushareProvider._to_ts_code("SSE.ETF.510300") == "510300.SH"
        assert TushareProvider._to_ts_code("SZSE.ETF.159915") == "159915.SZ"
        assert TushareProvider._to_ts_code("sh600000") == "600000.SH"

    def test_from_ts_code(self):
        assert TushareProvider._from_ts_code("600000.SH") == "SSE.STK.600000"
        assert TushareProvider._from_ts_code("000001.SZ") == "SZSE.STK.000001"
        assert TushareProvider._from_ts_code("430047.BJ") == "BSE.STK.430047"

    def test_index_etf_ts_code_to_symbol(self):
        conv = TushareProvider._index_etf_ts_code_to_symbol
        assert conv("000001.SH", "IDX") == "SSE.IDX.000001"
        assert conv("399006.SZ", "IDX") == "SZSE.IDX.399006"
        assert conv("510300.SH", "ETF") == "SSE.ETF.510300"
        assert conv("159915.SZ", "ETF") == "SZSE.ETF.159915"
        # wrong segments / kinds rejected
        assert conv("000300.SH", "ETF") == ""
        assert conv("510300.SH", "IDX") == ""
        assert conv("600000.SH", "ETF") == ""
        assert conv("000001.SH", "bogus") == ""
        assert conv("junk", "IDX") == ""
        assert conv("000001.HK", "IDX") == ""
        assert conv("399006.SZ", "IDX") != ""

    def test_fetch_bars_dispatch_index_etf(self, provider):
        import pandas as pd
        df = pd.DataFrame({
            "trade_date": ["20260731"],
            "open": [3800.0], "high": [3810.0], "low": [3790.0],
            "close": [3805.0], "vol": [100.0], "amount": [1000000.0],
        })
        req = MarketDataRequest(
            symbols=["SSE.IDX.000001"],
            period=BarPeriod.DAY,
            adjustment=AdjustmentMode.NONE,
        )
        provider._pro.index_daily.return_value = df
        bars = provider.fetch_bars(req)
        assert len(bars) == 1
        assert bars[0].symbol == "SSE.IDX.000001"
        assert bars[0].source == "tushare"
        # Full history is split into year-chunks (Tushare 6000-row/call cap).
        calls = provider._pro.index_daily.call_args_list
        assert len(calls) > 1
        assert calls[0].kwargs["start_date"] == "19900101"
        assert calls[-1].kwargs["end_date"] == str(int(pd.Timestamp.today().strftime("%Y%m%d")))
        assert all(int(c.kwargs["start_date"]) <= int(c.kwargs["end_date"]) for c in calls)
        # A short explicit window stays a single call.
        provider._pro.index_daily.reset_mock()
        provider._pro.index_daily.return_value = df
        provider.fetch_bars(
            MarketDataRequest(
                symbols=["SSE.IDX.000001"],
                period=BarPeriod.DAY,
                adjustment=AdjustmentMode.NONE,
                start_date=20260701,
                end_date=20260731,
            )
        )
        provider._pro.index_daily.assert_called_once()

        provider._pro.fund_daily.return_value = df
        bars2 = provider.fetch_bars(
            MarketDataRequest(
                symbols=["SSE.ETF.510300"],
                period=BarPeriod.DAY,
                adjustment=AdjustmentMode.NONE,
            )
        )
        assert bars2[0].symbol == "SSE.ETF.510300"
        assert provider._pro.fund_daily.call_count > 1

        # stocks still hit daily
        provider._pro.daily.return_value = df
        bars3 = provider.fetch_bars(
            MarketDataRequest(
                symbols=["600000.SH"],
                period=BarPeriod.DAY,
                adjustment=AdjustmentMode.NONE,
            )
        )
        assert bars3[0].symbol == "600000.SH"

        # QFQ rejected for index/ETF (no 复权)
        with pytest.raises(InvalidSymbol):
            provider.fetch_bars(
                MarketDataRequest(
                    symbols=["SSE.IDX.000001"],
                    period=BarPeriod.DAY,
                    adjustment=AdjustmentMode.QFQ,
                )
            )

    def test_fetch_index_etf_universe(self, provider):
        import pandas as pd
        provider._pro.index_basic.return_value = pd.DataFrame({
            "ts_code": ["000001.SH", "399001.SZ", "930001.CSI", "000300.SH"],
            "name": ["上证指数", "深证成指", "中证800", "沪深300"],
            "market": ["SSE", "SZSE", "CSI", "SSE"],
        })
        provider._pro.fund_basic.return_value = pd.DataFrame({
            "ts_code": ["510300.SH", "510050.SH", "159915.SZ", "161725.SZ",
                        "180801.SH"],
            "name": ["沪深300ETF", "上证50ETF", "创业板ETF", "招商中证白酒",
                     "某REIT"],
            "market": ["E", "E", "E", "E", "E"],
            "fund_type": ["股票型", "股票型", "股票型", "混合型", "REITs"],
        })
        entries = provider.fetch_index_etf_universe()
        syms = {e.symbol for e in entries}
        assert syms == {
            "SSE.IDX.000001",
            "SSE.IDX.000300",
            "SZSE.IDX.399001",
            "SSE.ETF.510300",
            "SSE.ETF.510050",
            "SZSE.ETF.159915",
            "SZSE.ETF.161725",
        }
        by = {e.symbol: e for e in entries}
        assert by["SSE.IDX.000001"].name == "上证指数"
        assert by["SSE.IDX.000001"].exchange == "SSE"
        assert by["SSE.IDX.000001"].status == "listed"
        assert by["SSE.IDX.000001"].source == "tushare"
        # market='E' must be passed so the 15000-row cap never drops big ETFs
        provider._pro.fund_basic.assert_called_once_with(
            market="E", list_status="L"
        )
        # CSI index, REIT (SH 180801) excluded
        assert "930001.CSI" not in syms
        assert "SSE.ETF.180801" not in syms

    def test_symbol_kind(self):
        assert _symbol_kind("SSE.STK.600000") == "stock"
        assert _symbol_kind("600000.SH") == "stock"
        assert _symbol_kind("sh600000") == "stock"
        assert _symbol_kind("430047.BJ") == "stock"
        assert _symbol_kind("SSE.IDX.000001") == "index"
        assert _symbol_kind("sh000001") == "index"
        assert _symbol_kind("000001.SH") == "index"
        assert _symbol_kind("SZSE.IDX.399001") == "index"
        assert _symbol_kind("sz399001") == "index"
        assert _symbol_kind("399001.SZ") == "index"
        assert _symbol_kind("SSE.ETF.510300") == "etf"
        assert _symbol_kind("sh510300") == "etf"
        assert _symbol_kind("510300.SH") == "etf"
        assert _symbol_kind("SZSE.ETF.159915") == "etf"
        assert _symbol_kind("sz159915") == "etf"
        assert _symbol_kind("159915.SZ") == "etf"

    def test_fetch_bars_multi_symbol_labels_each_bar(self, provider):
        import pandas as pd
        df = pd.DataFrame({
            "trade_date": ["20260731"],
            "open": [1.0], "high": [2.0], "low": [0.5],
            "close": [1.5], "vol": [100.0], "amount": [1000.0],
        })
        provider._pro.daily.return_value = df
        bars = provider.fetch_bars(
            MarketDataRequest(
                symbols=["600000.SH", "000001.SZ"],
                period=BarPeriod.DAY,
                adjustment=AdjustmentMode.NONE,
            )
        )
        assert [b.symbol for b in bars] == ["600000.SH", "000001.SZ"]

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
        bars = provider._dataframe_to_bars(df, req, AdjustmentMode.NONE)
        assert len(bars) == 2
        assert bars[0].symbol == "600000.SH"
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
        bars = provider._dataframe_to_bars(df, req, AdjustmentMode.QFQ)
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


class TestTushareThrottle:
    """全局限速：EOD 股票链 none+qfq 5000+ 只逐只调用无节流会触发 Tushare
    500/min 限流并拖垮 adj_factor 链（2026-08-13 事故根因）。"""

    def test_default_rate_from_constant(self, monkeypatch):
        monkeypatch.delenv("TUSHARE_RATE_PER_MIN", raising=False)
        p = TushareProvider(token="x")
        assert p._rate_per_min == 300

    def test_env_rate_overrides_default(self, monkeypatch):
        monkeypatch.setenv("TUSHARE_RATE_PER_MIN", "120")
        p = TushareProvider(token="x")
        assert p._rate_per_min == 120

    def test_explicit_rate_wins(self, monkeypatch):
        monkeypatch.setenv("TUSHARE_RATE_PER_MIN", "120")
        p = TushareProvider(token="x", rate_per_min=60)
        assert p._rate_per_min == 60

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TUSHARE_RATE_PER_MIN", "not-a-number")
        p = TushareProvider(token="x")
        assert p._rate_per_min == 300

    def test_throttle_paces_calls_to_budget(self, monkeypatch):
        """_throttle must sleep so the long-run rate never exceeds rate_per_min."""
        import time as _time

        p = TushareProvider(token="x", rate_per_min=120)  # interval = 0.5s
        fake = {"now": 1000.0}
        sleeps = []
        monkeypatch.setattr(_time, "time", lambda: fake["now"])
        monkeypatch.setattr(
            _time, "sleep",
            lambda s: (sleeps.append(s), fake.__setitem__("now", fake["now"] + s)),
        )
        p._throttle()  # first call: last_call_ts=0, now=1000 -> no wait
        assert sleeps == []
        p._throttle()  # second call immediately after: must wait ~0.5s
        assert len(sleeps) == 1
        assert abs(sleeps[0] - 0.5) < 1e-6

    def test_throttle_disabled_at_zero(self, monkeypatch):
        import time as _time

        p = TushareProvider(token="x", rate_per_min=0)
        monkeypatch.setattr(_time, "time", lambda: 0.0)
        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        p._throttle()
        assert sleeps == []

    def test_call_with_retry_throttles_before_each_attempt(self, provider, monkeypatch):
        """_call_with_retry must call _throttle before every attempt."""
        import time as _time

        provider._rate_per_min = 0
        throttled = {"n": 0}
        monkeypatch.setattr(provider, "_throttle", lambda: throttled.__setitem__("n", throttled["n"] + 1))
        provider._pro.daily.side_effect = Exception("limit 频率超限")
        monkeypatch.setattr(_time, "sleep", lambda s: None)  # skip backoff sleep
        with pytest.raises(Exception):
            provider._call_with_retry(provider._pro.daily, ts_code="600000.SH")
        # MAX_RETRIES(3) attempts, each preceded by one throttle
        assert throttled["n"] == 3


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
