# -*- coding: utf-8 -*-
"""Tests for symbol canonical normalization and legacy dataset compatibility."""

import pytest
from pathlib import Path

from wtpy.apps.astock.data.repository import MarketDataRepository
from scripts.sync_market_data import _normalize_symbol


class TestNormalizeSymbol:
    """Test _normalize_symbol handles all input formats correctly."""

    def test_canonical_passthrough(self):
        assert _normalize_symbol("SSE.STK.600000") == "SSE.STK.600000"
        assert _normalize_symbol("SZSE.STK.000001") == "SZSE.STK.000001"
        assert _normalize_symbol("BSE.STK.430047") == "BSE.STK.430047"

    def test_dot_suffix_format(self):
        assert _normalize_symbol("600000.SH") == "SSE.STK.600000"
        assert _normalize_symbol("000001.SZ") == "SZSE.STK.000001"
        assert _normalize_symbol("430047.BJ") == "BSE.STK.430047"

    def test_prefix_format(self):
        assert _normalize_symbol("sh600000") == "SSE.STK.600000"
        assert _normalize_symbol("sz000001") == "SZSE.STK.000001"
        assert _normalize_symbol("bj430047") == "BSE.STK.430047"

    def test_prefix_format_uppercase(self):
        assert _normalize_symbol("SH600000") == "SSE.STK.600000"
        assert _normalize_symbol("SZ000001") == "SZSE.STK.000001"
        assert _normalize_symbol("BJ430047") == "BSE.STK.430047"

    def test_bare_6digit_shanghai(self):
        assert _normalize_symbol("600000") == "SSE.STK.600000"
        assert _normalize_symbol("601088") == "SSE.STK.601088"
        assert _normalize_symbol("500001") == "SSE.STK.500001"
        assert _normalize_symbol("900001") == "SSE.STK.900001"

    def test_bare_6digit_shenzhen(self):
        assert _normalize_symbol("000001") == "SZSE.STK.000001"
        assert _normalize_symbol("301107") == "SZSE.STK.301107"
        assert _normalize_symbol("002001") == "SZSE.STK.002001"

    def test_bare_6digit_beijing(self):
        assert _normalize_symbol("430047") == "BSE.STK.430047"
        assert _normalize_symbol("830001") == "BSE.STK.830001"

    def test_unknown_format_passthrough(self):
        assert _normalize_symbol("UNKNOWN") == "UNKNOWN"
        assert _normalize_symbol("12345") == "12345"


class TestRepositorySymbolVariants:
    """Test MarketDataRepository._symbol_variants covers all formats."""

    def test_canonical_generates_all_variants(self):
        variants = MarketDataRepository._symbol_variants("SSE.STK.600000")
        assert "SSE.STK.600000" in variants
        assert "600000.SH" in variants
        assert "sh600000" in variants
        assert "600000" in variants

    def test_dot_suffix_generates_all_variants(self):
        variants = MarketDataRepository._symbol_variants("000001.SZ")
        assert "000001.SZ" in variants
        assert "SZSE.STK.000001" in variants
        assert "sz000001" in variants
        assert "000001" in variants

    def test_prefix_generates_all_variants(self):
        variants = MarketDataRepository._symbol_variants("sh600000")
        assert "sh600000" in variants
        assert "SSE.STK.600000" in variants
        assert "600000.SH" in variants
        assert "600000" in variants

    def test_bare_6digit_generates_all_variants(self):
        variants = MarketDataRepository._symbol_variants("600000")
        assert "600000" in variants
        assert "SSE.STK.600000" in variants
        assert "600000.SH" in variants
        assert "sh600000" in variants

    def test_beijing_stock_variants(self):
        variants = MarketDataRepository._symbol_variants("BSE.STK.430047")
        assert "BSE.STK.430047" in variants
        assert "430047.BJ" in variants
        assert "bj430047" in variants
        assert "430047" in variants

    def test_no_cross_market_confusion(self):
        sh_variants = MarketDataRepository._symbol_variants("SSE.STK.600000")
        sz_variants = MarketDataRepository._symbol_variants("SZSE.STK.000001")
        assert not any(v in sh_variants for v in sz_variants if v.isdigit())


class TestLegacyDatasetCompatibility:
    """Test that old datasets with sh/sz symbols can be read via canonical symbols."""

    def test_find_symbol_record_legacy_sh(self, tmp_path):
        from wtpy.apps.astock.data.dataset_store import DatasetManifest, SymbolRecord, DatasetStore

        store = DatasetStore(tmp_path)
        manifest = DatasetManifest(
            dataset_id="test_legacy",
            source="tdx_local",
            adjustment="none",
            period="1d",
            status="ready",
            symbols=[
                SymbolRecord(symbol="sh600000", blob_sha256="abc123", row_count=100),
                SymbolRecord(symbol="sz000001", blob_sha256="def456", row_count=200),
            ],
        )

        repo = MarketDataRepository(store)
        rec = repo._find_symbol_record(manifest, "SSE.STK.600000")
        assert rec is not None
        assert rec.symbol == "sh600000"

        rec2 = repo._find_symbol_record(manifest, "SZSE.STK.000001")
        assert rec2 is not None
        assert rec2.symbol == "sz000001"

    def test_find_symbol_record_canonical(self, tmp_path):
        from wtpy.apps.astock.data.dataset_store import DatasetManifest, SymbolRecord, DatasetStore

        store = DatasetStore(tmp_path)
        manifest = DatasetManifest(
            dataset_id="test_canonical",
            source="tushare",
            adjustment="qfq",
            period="1d",
            status="ready",
            symbols=[
                SymbolRecord(symbol="SSE.STK.601088", blob_sha256="abc123", row_count=100),
            ],
        )

        repo = MarketDataRepository(store)
        rec = repo._find_symbol_record(manifest, "601088.SH")
        assert rec is not None
        assert rec.symbol == "SSE.STK.601088"

        rec2 = repo._find_symbol_record(manifest, "sh601088")
        assert rec2 is not None
        assert rec2.symbol == "SSE.STK.601088"
