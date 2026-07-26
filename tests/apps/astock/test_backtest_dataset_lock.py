"""Tests that dataset_id is locked at task creation and cannot change mid-run."""
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.repository import MarketDataRepository, DatasetNotFoundError
from wtpy.apps.astock.data.providers.base import MarketBar
from wtpy.apps.astock.service.backtest_request import BacktestRequest


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


@pytest.fixture
def repo(store):
    return MarketDataRepository(store)


def _publish(store, dataset_id, source="tdxquant", adjustment="front"):
    bars = [
        MarketBar(
            symbol="SSE.STK.600000", trade_date=20240101 + i, period="1d",
            open=10.0, high=11.0, low=9.0, close=10.5,
            volume=1000.0, amount=10000.0,
        )
        for i in range(3)
    ]
    sha = store.store_bars("SSE.STK.600000", bars)
    m = DatasetManifest(
        dataset_id=dataset_id, source=source, adjustment=adjustment,
        period="1d", status="building", data_cutoff_date=20260724,
        symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
        symbol_count=1, row_count=3,
    )
    store.publish(m)


class TestDatasetLock:
    def test_request_locks_dataset_id(self):
        req = BacktestRequest(
            rule_ids=["rule_a"],
            signal_data_source="tdxquant",
            dataset_id="tdxquant_front_1d_20260724_locked",
        )
        assert req.dataset_id == "tdxquant_front_1d_20260724_locked"
        d = req.to_dict()
        assert d["dataset_id"] == "tdxquant_front_1d_20260724_locked"

    def test_missing_dataset_raises_not_fallback(self, store, repo):
        _publish(store, "ds_exists")
        with pytest.raises(DatasetNotFoundError):
            repo.get_dataset("ds_does_not_exist")

    def test_specified_source_no_dataset_raises(self, repo):
        with pytest.raises(DatasetNotFoundError):
            repo.resolve_latest_ready(
                source="tdxquant", adjustment="front", period="1d"
            )

    def test_repeat_backtest_same_dataset_same_result(self, store, repo):
        _publish(store, "ds_repeat")
        bars1 = repo.load_bars(dataset_id="ds_repeat", symbol="SSE.STK.600000")
        bars2 = repo.load_bars(dataset_id="ds_repeat", symbol="SSE.STK.600000")
        assert len(bars1) == len(bars2)
        for b1, b2 in zip(bars1, bars2):
            assert b1.close == b2.close
            assert b1.trade_date == b2.trade_date

    def test_dataset_immutable_after_publish(self, store, repo):
        _publish(store, "ds_immutable")
        m = repo.get_dataset("ds_immutable")
        assert m.status == "ready"
        bars = repo.load_bars(dataset_id="ds_immutable", symbol="SSE.STK.600000")
        assert len(bars) == 3

    def test_no_provider_call_during_load(self, store, repo):
        _publish(store, "ds_offline")
        from unittest.mock import patch
        from wtpy.apps.astock.data.providers.tdxquant import TdxQuantProvider
        with patch.object(TdxQuantProvider, "fetch_bars") as mock_fetch:
            bars = repo.load_bars(dataset_id="ds_offline", symbol="SSE.STK.600000")
            assert len(bars) == 3
            mock_fetch.assert_not_called()
