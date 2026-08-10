"""Tests for content-addressed blob storage."""
import pytest
import numpy as np

from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.providers.base import MarketBar


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


def _make_bars(symbol, n=5):
    return [
        MarketBar(
            symbol=symbol,
            trade_date=20240101 + i,
            period="1d",
            open=10.0 + i,
            high=11.0 + i,
            low=9.0 + i,
            close=10.5 + i,
            volume=1000.0 + i,
            amount=10000.0 + i,
        )
        for i in range(n)
    ]


class TestContentAddressing:
    def test_same_content_same_hash(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha1 = store.store_bars("SSE.STK.600000", bars)
        sha2 = store.store_bars("SSE.STK.600000", bars)
        assert sha1 == sha2

    def test_different_content_different_hash(self, store):
        bars1 = _make_bars("SSE.STK.600000", n=5)
        bars2 = _make_bars("SSE.STK.600000", n=10)
        sha1 = store.store_bars("SSE.STK.600000", bars1)
        sha2 = store.store_bars("SSE.STK.600000", bars2)
        assert sha1 != sha2

    def test_blob_stored_once(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)
        store.store_bars("SSE.STK.600000", bars)
        blob_files = list(store.blobs_dir.glob("*.npz"))
        assert len(blob_files) == 1

    def test_load_bars_roundtrip(self, store):
        bars = _make_bars("SZSE.STK.000001", n=3)
        sha = store.store_bars("SZSE.STK.000001", bars)
        arrays = store.load_bars(sha)
        assert len(arrays["trade_date"]) == 3
        assert arrays["open"][0] == pytest.approx(10.0)
        assert arrays["close"][2] == pytest.approx(12.5)

    def test_load_nonexistent_blob(self, store):
        with pytest.raises(FileNotFoundError):
            store.load_bars("nonexistent_sha")

    def test_blob_exists(self, store):
        bars = _make_bars("SSE.STK.600000")
        sha = store.store_bars("SSE.STK.600000", bars)
        assert store.blob_exists(sha) is True
        assert store.blob_exists("fake_sha") is False

    def test_blob_sha_set(self, store):
        sha = store.store_bars("SSE.STK.600000", _make_bars("SSE.STK.600000"))
        sha2 = store.store_bars("SZSE.STK.000001", _make_bars("SZSE.STK.000001", n=3))
        # in-flight write marker must not be reported as a blob
        (store.blobs_dir / f"{sha}.npz.tmp").write_bytes(b"x")
        s = store.blob_sha_set()
        assert sha in s
        assert sha2 in s
        assert "fake_sha" not in s
        assert f"{sha}.npz.tmp" not in s
        # same store: second call hits the cache and stays consistent
        assert store.blob_sha_set() == s
        # empty blobs dir -> empty set (dir is auto-created by __init__)
        empty = DatasetStore(store.root / "empty_md")
        assert empty.blob_sha_set() == set()

    def test_empty_bars_returns_empty_sha(self, store):
        sha = store.store_bars("SSE.STK.600000", [])
        assert sha == ""

    def test_precision_preserved(self, store):
        bars = [
            MarketBar(
                symbol="SSE.STK.600000",
                trade_date=20240101,
                period="1d",
                open=20.15,
                high=20.25,
                low=19.15,
                close=19.65,
                volume=1234567.0,
                amount=24567890.12,
            )
        ]
        sha = store.store_bars("SSE.STK.600000", bars)
        arrays = store.load_bars(sha)
        assert arrays["open"][0] == pytest.approx(20.15)
        assert arrays["high"][0] == pytest.approx(20.25)
        assert arrays["low"][0] == pytest.approx(19.15)
        assert arrays["close"][0] == pytest.approx(19.65)
