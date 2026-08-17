"""Tests for dataset manifest creation and lifecycle."""
import pytest
import json
from pathlib import Path

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
    make_sync_run_id,
    validate_manifest_path,
)


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "market_data")


class TestDatasetManifest:
    def test_create_manifest(self):
        m = DatasetManifest(
            dataset_id="tdxquant_front_1d_20260724_a3f2b1c4d5e6",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="building",
        )
        assert m.dataset_id == "tdxquant_front_1d_20260724_a3f2b1c4d5e6"
        assert m.source == "tdxquant"
        assert m.status == "building"

    def test_to_dict_and_back(self):
        m = DatasetManifest(
            dataset_id="test_ds",
            source="tushare",
            adjustment="qfq",
            period="1d",
            anchor_date=20260724,
            symbol_count=2,
            row_count=100,
            status="ready",
            symbols=[
                SymbolRecord(symbol="SSE.STK.600000", blob_sha256="abc123", row_count=50),
                SymbolRecord(symbol="SZSE.STK.000001", blob_sha256="def456", row_count=50),
            ],
        )
        d = m.to_dict()
        assert d["source"] == "tushare"
        assert len(d["symbols"]) == 2
        m2 = DatasetManifest.from_dict(d)
        assert m2.dataset_id == "test_ds"
        assert len(m2.symbols) == 2

    def test_save_and_load(self, store):
        m = DatasetManifest(
            dataset_id="test_save_load",
            source="tdxquant",
            adjustment="front",
            period="1d",
            status="ready",
            symbol_count=1,
            row_count=10,
        )
        store.save_manifest(m)
        loaded = store.load_manifest("test_save_load")
        assert loaded is not None
        assert loaded.dataset_id == "test_save_load"
        assert loaded.manifest_sha256 != ""

    def test_legacy_manifest_hash_validates_from_persisted_payload(
        self, store
    ):
        m = DatasetManifest(
            dataset_id="legacy_hash",
            source="tushare",
            adjustment="none",
            period="1d",
            status="ready",
        )
        path = store.save_manifest(m)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("delta_commit_seq", None)
        payload.pop("factor_commit_seq", None)
        payload["manifest_sha256"] = ""
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        import hashlib

        payload["manifest_sha256"] = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        assert validate_manifest_path(
            path, expected_sha256=payload["manifest_sha256"]
        )

    def test_load_nonexistent(self, store):
        assert store.load_manifest("nonexistent") is None

    def test_list_manifests(self, store):
        for i in range(3):
            m = DatasetManifest(
                dataset_id=f"ds_{i}",
                source="tdxquant",
                adjustment="front",
                period="1d",
                status="ready",
            )
            store.save_manifest(m)
        ids = store.list_manifests()
        assert len(ids) == 3


class TestMakeDatasetId:
    def test_format(self):
        ds_id = make_dataset_id("tdxquant", "front", "1d", "20260724", "a3f2b1c4d5e6f789")
        assert ds_id == "tdxquant_front_1d_20260724_a3f2b1c4d5e6"

    def test_tushare_format(self):
        ds_id = make_dataset_id("tushare", "qfq", "1d", "anchor20260724", "7f8e9d0c1b2a3456")
        assert ds_id == "tushare_qfq_1d_anchor20260724_7f8e9d0c1b2a"


class TestMakeSyncRunId:
    def test_format(self):
        sr_id = make_sync_run_id("tdxquant")
        assert sr_id.startswith("tdxquant_")
        parts = sr_id.split("_")
        assert len(parts) == 3

    def test_uniqueness(self):
        ids = {make_sync_run_id("tushare") for _ in range(100)}
        assert len(ids) == 100


class TestSymbolRecord:
    def test_to_dict(self):
        r = SymbolRecord(
            symbol="SSE.STK.600000",
            blob_sha256="abc",
            first_date=20200101,
            last_date=20260724,
            row_count=1500,
            quality="ok",
        )
        d = r.to_dict()
        assert d["symbol"] == "SSE.STK.600000"
        assert d["row_count"] == 1500
