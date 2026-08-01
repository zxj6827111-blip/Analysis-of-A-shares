"""Gate B3 focused tests: composite_none builder (synthetic stores)."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from wtpy.apps.astock.data.composite_dataset import (
    COMPOSITE_MERGE_RULE_VERSION,
    CompositeOverlapError,
    CompositeParentError,
    build_composite_none,
)
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)


def _arrays(dates, price=10.0):
    n = len(dates)
    return {
        "trade_date": np.array(dates, dtype=np.int64),
        "open": np.full(n, price),
        "high": np.full(n, price * 1.01),
        "low": np.full(n, price * 0.99),
        "close": np.full(n, price),
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 10000.0),
    }


def _publish(store, dataset_id, source, adjustment, symbols_arrays, status="ready"):
    records = []
    rows = 0
    for sym, arrays in symbols_arrays.items():
        sha = store.store_bar_arrays(sym, arrays)
        n = len(arrays["trade_date"])
        records.append(
            SymbolRecord(
                symbol=sym,
                blob_sha256=sha,
                first_date=int(arrays["trade_date"][0]),
                last_date=int(arrays["trade_date"][-1]),
                row_count=n,
            )
        )
        rows += n
    m = DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period="1d",
        status=status,
        created_at="2026-07-27T00:00:00",
    )
    m.symbols = records
    m.symbol_count = len(records)
    m.row_count = rows
    store.save_manifest(m)
    return m


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "md")


@pytest.fixture
def parents(store):
    _publish(
        store,
        "base_ds",
        "local_vendor",
        "none",
        {
            "SSE.STK.600000": _arrays([20200101, 20200102]),
            "SZSE.STK.000001": _arrays([20200101, 20200102, 20200103]),
        },
    )
    _publish(
        store,
        "supp_ds",
        "tushare",
        "none",
        {"SZSE.STK.300104": _arrays([20190101, 20190102])},
    )
    return store


class TestHappyPath:
    def test_compose(self, parents):
        m = build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
        )
        assert m.status == "ready"
        assert m.source == "internal"
        assert m.adjustment == "composite_none"
        assert m.symbol_count == 3
        assert m.row_count == 7
        prov = m.provenance["symbol_provenance"]
        assert prov["SSE.STK.600000"] == "base_ds"
        assert prov["SZSE.STK.300104"] == "supp_ds"
        parents_info = m.provenance["parents"]
        assert [p["dataset_id"] for p in parents_info] == ["base_ds", "supp_ds"]
        assert all(len(p["manifest_file_sha256"]) == 64 for p in parents_info)
        assert (
            m.provenance["composite_merge_rule_version"]
            == COMPOSITE_MERGE_RULE_VERSION
        )

    def test_blobs_shared_not_copied(self, parents):
        blob_count_before = len(list(parents.blobs_dir.glob("*.npz")))
        m = build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
        )
        blob_count_after = len(list(parents.blobs_dir.glob("*.npz")))
        assert blob_count_before == blob_count_after
        base = parents.load_manifest("base_ds")
        base_shas = {s.blob_sha256 for s in base.symbols}
        comp_shas = {s.blob_sha256 for s in m.symbols}
        assert base_shas <= comp_shas

    def test_idempotent_rebuild(self, parents):
        m1 = build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
        )
        m2 = build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
        )
        assert m1.dataset_id == m2.dataset_id

    def test_parents_not_modified(self, parents):
        def sha(p):
            return hashlib.sha256(p.read_bytes()).hexdigest()

        base_path = parents.manifests_dir / "base_ds.json"
        supp_path = parents.manifests_dir / "supp_ds.json"
        before = (sha(base_path), sha(supp_path))
        build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
        )
        assert (sha(base_path), sha(supp_path)) == before

    def test_dry_run_publishes_nothing(self, parents):
        m = build_composite_none(
            parents,
            base_dataset_id="base_ds",
            supplement_dataset_id="supp_ds",
            cutoff=20260717,
            dry_run=True,
        )
        assert m.status == "building"
        assert parents.load_manifest(m.dataset_id) is None


class TestRuleViolations:
    def test_overlap_rejected(self, store):
        _publish(
            store, "base_ds", "local_vendor", "none",
            {"SSE.STK.600000": _arrays([20200101])},
        )
        _publish(
            store, "supp_ds", "tushare", "none",
            {"SSE.STK.600000": _arrays([20190101])},  # same symbol!
        )
        with pytest.raises(CompositeOverlapError, match="splicing"):
            build_composite_none(
                store,
                base_dataset_id="base_ds",
                supplement_dataset_id="supp_ds",
                cutoff=20260717,
            )

    def test_parent_missing(self, store):
        _publish(
            store, "base_ds", "local_vendor", "none",
            {"SSE.STK.600000": _arrays([20200101])},
        )
        with pytest.raises(CompositeParentError, match="missing"):
            build_composite_none(
                store,
                base_dataset_id="base_ds",
                supplement_dataset_id="nope_ds",
                cutoff=20260717,
            )

    def test_parent_not_ready(self, store):
        _publish(
            store, "base_ds", "local_vendor", "none",
            {"SSE.STK.600000": _arrays([20200101])},
        )
        _publish(
            store, "supp_ds", "tushare", "none",
            {"SZSE.STK.300104": _arrays([20190101])},
            status="partial",
        )
        with pytest.raises(CompositeParentError, match="not ready"):
            build_composite_none(
                store,
                base_dataset_id="base_ds",
                supplement_dataset_id="supp_ds",
                cutoff=20260717,
            )

    def test_missing_blob_rejected(self, store):
        _publish(
            store, "base_ds", "local_vendor", "none",
            {"SSE.STK.600000": _arrays([20200101])},
        )
        m = _publish(
            store, "supp_ds", "tushare", "none",
            {"SZSE.STK.300104": _arrays([20190101])},
        )
        # corrupt: delete the supplement blob
        (store.blobs_dir / f"{m.symbols[0].blob_sha256}.npz").unlink()
        with pytest.raises(CompositeParentError, match="blobs missing"):
            build_composite_none(
                store,
                base_dataset_id="base_ds",
                supplement_dataset_id="supp_ds",
                cutoff=20260717,
            )
