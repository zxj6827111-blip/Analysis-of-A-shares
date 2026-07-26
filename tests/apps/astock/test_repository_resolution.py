"""Tests for MarketDataRepository resolution logic."""
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.repository import (
    MarketDataRepository,
    DatasetNotFoundError,
    DatasetNotReadyError,
)
from wtpy.apps.astock.data.providers.base import MarketBar


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


@pytest.fixture
def repo(store):
    return MarketDataRepository(store)


def _publish_ready(store, dataset_id, source, adjustment, period, cutoff):
    bars = [
        MarketBar(
            symbol="SSE.STK.600000", trade_date=20240101 + i, period=period,
            open=10.0, high=11.0, low=9.0, close=10.5,
            volume=1000.0, amount=10000.0,
        )
        for i in range(3)
    ]
    sha = store.store_bars("SSE.STK.600000", bars)
    m = DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period=period,
        status="building",
        data_cutoff_date=cutoff,
        symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
        symbol_count=1,
        row_count=3,
    )
    store.publish(m)
    return m


class TestRepositoryResolution:
    def test_resolve_latest_ready(self, store, repo):
        _publish_ready(store, "ds_old", "tdxquant", "front", "1d", 20260720)
        _publish_ready(store, "ds_new", "tdxquant", "front", "1d", 20260724)
        latest = repo.resolve_latest_ready(source="tdxquant", adjustment="front", period="1d")
        assert latest.dataset_id == "ds_new"

    def test_resolve_no_ready_raises(self, repo):
        with pytest.raises(DatasetNotFoundError):
            repo.resolve_latest_ready(source="tdxquant", adjustment="front", period="1d")

    def test_resolve_filters_by_source(self, store, repo):
        _publish_ready(store, "ds_tdx", "tdxquant", "front", "1d", 20260724)
        _publish_ready(store, "ds_ts", "tushare", "qfq", "1d", 20260724)
        latest = repo.resolve_latest_ready(source="tushare", adjustment="qfq", period="1d")
        assert latest.dataset_id == "ds_ts"

    def test_get_dataset(self, store, repo):
        _publish_ready(store, "ds_get", "tdxquant", "front", "1d", 20260724)
        m = repo.get_dataset("ds_get")
        assert m.dataset_id == "ds_get"

    def test_get_dataset_not_found(self, repo):
        with pytest.raises(DatasetNotFoundError):
            repo.get_dataset("nonexistent")

    def test_list_datasets_filter(self, store, repo):
        _publish_ready(store, "ds1", "tdxquant", "front", "1d", 20260720)
        _publish_ready(store, "ds2", "tushare", "qfq", "1d", 20260724)
        _publish_ready(store, "ds3", "tdxquant", "none", "1d", 20260724)

        tdx_front = repo.list_datasets(source="tdxquant", adjustment="front")
        assert len(tdx_front) == 1
        assert tdx_front[0].dataset_id == "ds1"

        all_tdx = repo.list_datasets(source="tdxquant")
        assert len(all_tdx) == 2

    def test_load_bars(self, store, repo):
        _publish_ready(store, "ds_load", "tdxquant", "front", "1d", 20260724)
        bars = repo.load_bars(dataset_id="ds_load", symbol="SSE.STK.600000")
        assert len(bars) == 3
        assert bars[0].source == "tdxquant"

    def test_load_bars_date_filter(self, store, repo):
        _publish_ready(store, "ds_filter", "tdxquant", "front", "1d", 20260724)
        bars = repo.load_bars(dataset_id="ds_filter", symbol="SSE.STK.600000", start_date=20240102)
        assert len(bars) == 2

    def test_load_day_bars_compat(self, store, repo):
        _publish_ready(store, "ds_compat", "tdxquant", "front", "1d", 20260724)
        day_bars = repo.load_day_bars(dataset_id="ds_compat", symbol="SSE.STK.600000")
        assert len(day_bars) == 3
        assert hasattr(day_bars[0], "date")
        assert hasattr(day_bars[0], "open")

    def test_validate_dataset(self, store, repo):
        _publish_ready(store, "ds_valid", "tdxquant", "front", "1d", 20260724)
        result = repo.validate_dataset("ds_valid")
        assert result["valid"] is True
        assert result["issues"] == []

    def test_load_bars_building_raises(self, store, repo):
        m = DatasetManifest(
            dataset_id="ds_building",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
        )
        store.save_manifest(m)
        with pytest.raises(DatasetNotReadyError):
            repo.load_bars(dataset_id="ds_building")
