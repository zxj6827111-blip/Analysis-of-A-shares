"""Regression test: 301107 TdxQuant target week must match client values.

Target week values (verified against TDX client):
  open  = 20.15
  high  = 20.25
  low   = 19.15
  close = 19.65

This test uses mocked data to verify the normalization pipeline preserves
exact precision. Live tests are marked with @pytest.mark.live_tdxquant.
"""
import pytest
from unittest.mock import patch, MagicMock

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    MarketDataRequest,
)
from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.repository import MarketDataRepository


TARGET_WEEK = {
    "open": 20.15,
    "high": 20.25,
    "low": 19.15,
    "close": 19.65,
}


@pytest.fixture
def provider(tmp_path):
    return TdxQuantProvider(tdx_root=tmp_path, batch_size=10)


class Test301107Regression:
    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_normalization_preserves_target_week(self, provider):
        dates = pd.DatetimeIndex(["2024-07-01"])
        result = {
            "Open": pd.DataFrame({"301107.SZ": [TARGET_WEEK["open"]]}, index=dates),
            "High": pd.DataFrame({"301107.SZ": [TARGET_WEEK["high"]]}, index=dates),
            "Low": pd.DataFrame({"301107.SZ": [TARGET_WEEK["low"]]}, index=dates),
            "Close": pd.DataFrame({"301107.SZ": [TARGET_WEEK["close"]]}, index=dates),
            "Volume": pd.DataFrame({"301107.SZ": [1234567.0]}, index=dates),
            "Amount": pd.DataFrame({"301107.SZ": [24567890.0]}, index=dates),
        }
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.WEEK,
            adjustment=AdjustmentMode.FRONT,
        )
        bars = provider._normalize_wide_table(
            result, ["301107.SZ"], "1w", "front", req
        )
        assert len(bars) == 1
        bar = bars[0]
        assert bar.open == pytest.approx(TARGET_WEEK["open"])
        assert bar.high == pytest.approx(TARGET_WEEK["high"])
        assert bar.low == pytest.approx(TARGET_WEEK["low"])
        assert bar.close == pytest.approx(TARGET_WEEK["close"])

    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_roundtrip_through_dataset_store(self, tmp_path):
        from wtpy.apps.astock.data.providers.base import MarketBar

        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="301107.SZ",
                trade_date=20240705,
                period="1w",
                open=TARGET_WEEK["open"],
                high=TARGET_WEEK["high"],
                low=TARGET_WEEK["low"],
                close=TARGET_WEEK["close"],
                volume=1234567.0,
                amount=24567890.0,
                source="tdxquant",
                adjustment="front",
            )
        ]
        sha = store.store_bars("301107.SZ", bars)
        arrays = store.load_bars(sha)
        assert arrays["open"][0] == pytest.approx(TARGET_WEEK["open"])
        assert arrays["high"][0] == pytest.approx(TARGET_WEEK["high"])
        assert arrays["low"][0] == pytest.approx(TARGET_WEEK["low"])
        assert arrays["close"][0] == pytest.approx(TARGET_WEEK["close"])

    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_dataset_repository_preserves_precision(self, tmp_path):
        from wtpy.apps.astock.data.dataset_store import DatasetManifest, SymbolRecord
        from wtpy.apps.astock.data.providers.base import MarketBar

        store = DatasetStore(tmp_path / "market_data")
        bars = [
            MarketBar(
                symbol="301107.SZ",
                trade_date=20240705,
                period="1w",
                open=TARGET_WEEK["open"],
                high=TARGET_WEEK["high"],
                low=TARGET_WEEK["low"],
                close=TARGET_WEEK["close"],
                volume=1234567.0,
                amount=24567890.0,
                source="tdxquant",
                adjustment="front",
            )
        ]
        sha = store.store_bars("301107.SZ", bars)
        m = DatasetManifest(
            dataset_id="test_301107",
            source="tdxquant",
            adjustment="front",
            period="1w",
            status="building",
            symbols=[SymbolRecord(symbol="301107.SZ", blob_sha256=sha, row_count=1, quality="ok")],
            symbol_count=1,
            row_count=1,
        )
        store.publish(m)

        repo = MarketDataRepository(store)
        loaded = repo.load_bars(dataset_id="test_301107", symbol="301107.SZ")
        assert len(loaded) == 1
        assert loaded[0].open == pytest.approx(TARGET_WEEK["open"])
        assert loaded[0].high == pytest.approx(TARGET_WEEK["high"])
        assert loaded[0].low == pytest.approx(TARGET_WEEK["low"])
        assert loaded[0].close == pytest.approx(TARGET_WEEK["close"])

    @pytest.mark.live_tdxquant
    @pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")
    def test_live_301107_front_week(self):
        """Live test: requires TDX client online. Run with: pytest -m live_tdxquant"""
        provider = TdxQuantProvider(tdx_root=r"D:\通达信", batch_size=10)
        if not provider.health_check():
            pytest.skip("TDX client not available")
        req = MarketDataRequest(
            symbols=["301107.SZ"],
            period=BarPeriod.WEEK,
            adjustment=AdjustmentMode.FRONT,
        )
        bars = provider.fetch_bars(req)
        assert len(bars) > 0
        target = [b for b in bars if b.open == pytest.approx(20.15, abs=0.01)]
        assert len(target) >= 1, "Target week not found in live data"
