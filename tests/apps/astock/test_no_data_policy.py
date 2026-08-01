"""Strict ready policy: failed/no_data block ready unless explicitly allowlisted."""
from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    evaluate_strict_publish,
)


def _rec(sym, quality, error=""):
    return SymbolRecord(symbol=sym, blob_sha256="x" if quality == "ok" else "",
                        row_count=10 if quality == "ok" else 0,
                        quality=quality, error=error)


class TestStrictPolicy:
    def test_all_ok_ready(self):
        recs = [_rec("A", "ok"), _rec("B", "ok")]
        p = evaluate_strict_publish(recs, expected_symbol_count=2)
        assert p["target_status"] == "ready"
        assert p["imported_symbol_count"] == 2
        assert p["coverage_ratio"] == 1.0
        assert p["block_reasons"] == []

    def test_no_data_without_allowlist_blocks(self):
        recs = [_rec("A", "ok"), _rec("B", "no_data", "empty")]
        p = evaluate_strict_publish(recs, expected_symbol_count=2)
        assert p["target_status"] == "partial"
        assert any("no_data_not_allowlisted" in r for r in p["block_reasons"])

    def test_failed_always_blocks(self):
        recs = [_rec("A", "ok"), _rec("B", "error", "parse boom")]
        p = evaluate_strict_publish(
            recs, expected_symbol_count=2,
            no_data_allowlist={"B": "whatever"}, max_allow_count=10,
        )
        assert p["target_status"] == "partial"
        assert p["failed_symbol_count"] == 1

    def test_allowlisted_no_data_within_limit_ready(self):
        recs = [_rec("A", "ok"), _rec("B", "no_data", "empty")]
        p = evaluate_strict_publish(
            recs, expected_symbol_count=2,
            no_data_allowlist={"B": "suspended entire range"}, max_allow_count=1,
        )
        assert p["target_status"] == "ready"
        assert p["warning_symbol_count"] == 1
        assert p["no_data_allowlist"] == [
            {"symbol": "B", "reason": "suspended entire range"}
        ]

    def test_allowlist_over_limit_blocks(self):
        recs = [_rec("A", "ok"), _rec("B", "no_data"), _rec("C", "no_data")]
        p = evaluate_strict_publish(
            recs, expected_symbol_count=3,
            no_data_allowlist={"B": "r", "C": "r"}, max_allow_count=1,
        )
        assert p["target_status"] == "partial"

    def test_ratio_limit(self):
        recs = [_rec("A", "ok")] * 98 + [_rec("B", "no_data"), _rec("C", "no_data")]
        p = evaluate_strict_publish(
            recs, expected_symbol_count=100,
            no_data_allowlist={"B": "r", "C": "r"}, max_allow_ratio=0.02,
        )
        assert p["target_status"] == "ready"
        p2 = evaluate_strict_publish(
            recs, expected_symbol_count=100,
            no_data_allowlist={"B": "r", "C": "r"}, max_allow_ratio=0.01,
        )
        assert p2["target_status"] == "partial"


class TestManifestCoverageFields:
    def test_fields_roundtrip_through_store(self, tmp_path):
        store = DatasetStore(tmp_path / "md")
        m = DatasetManifest(
            dataset_id="localvendor_none_1d_test_cov001",
            source="local_vendor", adjustment="none", period="1d",
            status="building",
            universe_type="vendor_available_historical_union",
            universe_definition_version="v1",
            survivorship_bias=True,
            historical_universe_complete=False,
            delisted_coverage_complete=False,
            coverage_start_year=2000, coverage_end_year=2026,
            known_missing_delisted_count=6,
            known_missing_delisted_symbols=["SZSE.STK.300104"],
            warning_text="该数据集缺少部分历史退市股票，长期全市场回测存在幸存者偏差。",
            recommended_use=["engineering baseline"],
            prohibited_or_discouraged_use=["claiming absence of survivorship bias"],
            expected_symbol_count=2, imported_symbol_count=2,
            excluded_symbol_count=1, no_data_symbol_count=0,
            failed_symbol_count=0, warning_symbol_count=0,
            coverage_ratio=1.0,
        )
        recs = []
        from wtpy.apps.astock.data.providers.base import MarketBar
        bars = [MarketBar(symbol="SSE.STK.600000", trade_date=20240101, period="1d",
                          open=1, high=1, low=1, close=1, volume=1, amount=1)]
        sha = store.store_bars("SSE.STK.600000", bars)
        recs.append(SymbolRecord(symbol="SSE.STK.600000", blob_sha256=sha,
                                 row_count=1, quality="ok"))
        m.symbols = recs
        m.symbol_count = 1
        store.publish(m)
        loaded = store.load_manifest("localvendor_none_1d_test_cov001")
        assert loaded.survivorship_bias is True
        assert loaded.universe_type == "vendor_available_historical_union"
        assert loaded.coverage_start_year == 2000
        assert loaded.coverage_end_year == 2026
        assert loaded.known_missing_delisted_symbols == ["SZSE.STK.300104"]
        assert "幸存者偏差" in loaded.warning_text
        assert loaded.expected_symbol_count == 2
        assert loaded.coverage_ratio == 1.0

    def test_partial_status_preserved_by_publish(self, tmp_path):
        store = DatasetStore(tmp_path / "md")
        m = DatasetManifest(
            dataset_id="localvendor_none_1d_test_cov002",
            source="local_vendor", adjustment="none", period="1d",
            status="partial",
            no_data_symbol_count=3,
        )
        store.publish(m)
        loaded = store.load_manifest("localvendor_none_1d_test_cov002")
        assert loaded.status == "partial"

    def test_old_manifest_without_new_fields_loads(self, tmp_path):
        """Backward compat: manifests written before Gate A load with defaults."""
        store = DatasetStore(tmp_path / "md")
        legacy = {
            "dataset_id": "legacy_ds", "source": "tdxquant", "adjustment": "front",
            "period": "1d", "weekly_bar_mode": "local_aggregate",
            "anchor_date": None, "snapshot_date": None, "data_cutoff_date": None,
            "provider_version": "", "sync_run_id": "", "parent_dataset_id": None,
            "manifest_sha256": "", "symbol_count": 0, "row_count": 0,
            "status": "ready", "created_at": "", "symbols": [],
        }
        import json
        (store.manifests_dir / "legacy_ds.json").write_text(
            json.dumps(legacy), encoding="utf-8")
        loaded = store.load_manifest("legacy_ds")
        assert loaded.survivorship_bias is None
        assert loaded.universe_type == ""
        assert loaded.expected_symbol_count == 0
