"""Tests for atomic dataset publishing."""
import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.providers.base import MarketBar


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


def _make_bars(symbol, n=3):
    return [
        MarketBar(
            symbol=symbol, trade_date=20240101 + i, period="1d",
            open=10.0, high=11.0, low=9.0, close=10.5,
            volume=1000.0, amount=10000.0,
        )
        for i in range(n)
    ]


class TestAtomicPublish:
    def test_publish_ready_when_all_ok(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)
        m = DatasetManifest(
            dataset_id="test_ready",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
            symbol_count=1,
            row_count=3,
        )
        result = store.publish(m)
        assert result.status == "ready"
        loaded = store.load_manifest("test_ready")
        assert loaded.status == "ready"

    def test_publish_partial_when_errors(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)
        m = DatasetManifest(
            dataset_id="test_partial",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
            symbols=[
                SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok"),
                SymbolRecord(symbol="SZSE.STK.000001", blob_sha256="", row_count=0, quality="error", error="timeout"),
            ],
            symbol_count=2,
            row_count=3,
        )
        result = store.publish(m)
        assert result.status == "partial"

    def test_publish_fails_integrity_missing_blob(self, store):
        m = DatasetManifest(
            dataset_id="test_integrity_fail",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256="fake_sha", row_count=3, quality="ok")],
            symbol_count=1,
            row_count=3,
        )
        with pytest.raises(ValueError, match="Integrity check failed"):
            store.publish(m)
        loaded = store.load_manifest("test_integrity_fail")
        assert loaded.status == "failed"

    def test_partial_not_selected_as_latest_ready(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)

        m_ready = DatasetManifest(
            dataset_id="ds_ready",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
            data_cutoff_date=20260720,
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
            symbol_count=1,
            row_count=3,
        )
        store.publish(m_ready)

        m_partial = DatasetManifest(
            dataset_id="ds_partial",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
            data_cutoff_date=20260724,
            symbols=[
                SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok"),
                SymbolRecord(symbol="SZSE.STK.000001", blob_sha256="", quality="error", error="fail"),
            ],
            symbol_count=2,
            row_count=3,
        )
        store.publish(m_partial)

        from wtpy.apps.astock.data.repository import MarketDataRepository
        repo = MarketDataRepository(store)
        latest = repo.resolve_latest_ready(
            source="tdxquant", adjustment="front", period="1d"
        )
        assert latest.dataset_id == "ds_ready"

    def test_old_ready_preserved_after_partial(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)

        m_old = DatasetManifest(
            dataset_id="old_ready",
            source="tushare",
            adjustment="qfq",
            period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha, row_count=3, quality="ok")],
            symbol_count=1,
            row_count=3,
        )
        store.publish(m_old)

        m_new = DatasetManifest(
            dataset_id="new_partial",
            source="tushare",
            adjustment="qfq",
            period="1d",
            status="building",
            symbols=[SymbolRecord(symbol="SSE.STK.600000", blob_sha256="", quality="error", error="network")],
            symbol_count=1,
            row_count=0,
        )
        store.publish(m_new)

        old = store.load_manifest("old_ready")
        assert old.status == "ready"
        new = store.load_manifest("new_partial")
        assert new.status == "partial"
