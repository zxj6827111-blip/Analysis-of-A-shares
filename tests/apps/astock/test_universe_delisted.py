"""Tests for delisted stock universe support."""
import pytest

from wtpy.apps.astock.data.universe import AShareUniverse, SymbolInfo
from wtpy.apps.astock.data.providers.base import UniverseEntry


class TestDelistedUniverse:
    def test_symbol_info_has_delist_fields(self):
        s = SymbolInfo(
            raw="sh600001",
            std_code="SSE.STK.600001",
            exchange="SSE",
            code="600001",
            status="delisted",
            delist_date=20100601,
        )
        assert s.status == "delisted"
        assert s.delist_date == 20100601

    def test_symbol_info_default_listed(self):
        s = SymbolInfo(raw="sh600000", std_code="SSE.STK.600000", exchange="SSE", code="600000")
        assert s.status == "listed"
        assert s.delist_date is None

    def test_from_tushare_basic_filters_delisted_by_default(self):
        entries = [
            UniverseEntry(symbol="SSE.STK.600000", name="浦发银行", exchange="SSE", status="listed"),
            UniverseEntry(symbol="SSE.STK.600001", name="邯郸钢铁", exchange="SSE", status="delisted", delist_date=20100601),
        ]
        uni = AShareUniverse.from_tushare_basic(entries, include_delisted=False)
        codes = uni.codes()
        assert "SSE.STK.600000" in codes
        assert "SSE.STK.600001" not in codes

    def test_from_tushare_basic_includes_delisted_when_requested(self):
        entries = [
            UniverseEntry(symbol="SSE.STK.600000", name="浦发银行", exchange="SSE", status="listed"),
            UniverseEntry(symbol="SSE.STK.600001", name="邯郸钢铁", exchange="SSE", status="delisted", delist_date=20100601),
        ]
        uni = AShareUniverse.from_tushare_basic(entries, include_delisted=True)
        codes = uni.codes()
        assert "SSE.STK.600000" in codes
        assert "SSE.STK.600001" in codes

    def test_from_tushare_basic_filters_bse_by_default(self):
        entries = [
            UniverseEntry(symbol="SSE.STK.600000", name="浦发银行", exchange="SSE", status="listed"),
            UniverseEntry(symbol="BSE.STK.430047", name="诺思兰德", exchange="BSE", status="listed"),
        ]
        uni = AShareUniverse.from_tushare_basic(entries, include_bse=False)
        codes = uni.codes()
        assert "SSE.STK.600000" in codes
        assert "BSE.STK.430047" not in codes

    def test_from_tushare_basic_includes_bse_when_requested(self):
        entries = [
            UniverseEntry(symbol="SSE.STK.600000", name="浦发银行", exchange="SSE", status="listed"),
            UniverseEntry(symbol="BSE.STK.430047", name="诺思兰德", exchange="BSE", status="listed"),
        ]
        uni = AShareUniverse.from_tushare_basic(entries, include_bse=True)
        codes = uni.codes()
        assert "BSE.STK.430047" in codes

    def test_delisted_symbol_has_dates(self):
        entries = [
            UniverseEntry(
                symbol="SSE.STK.600001",
                name="邯郸钢铁",
                exchange="SSE",
                status="delisted",
                list_date=19980101,
                delist_date=20100601,
            ),
        ]
        uni = AShareUniverse.from_tushare_basic(entries, include_delisted=True)
        sym = uni.symbols[0]
        assert sym.list_date == 19980101
        assert sym.delist_date == 20100601
        assert sym.status == "delisted"

    def test_save_load_roundtrip_with_new_fields(self, tmp_path):
        symbols = [
            SymbolInfo(
                raw="sh600001",
                std_code="SSE.STK.600001",
                exchange="SSE",
                code="600001",
                status="delisted",
                delist_date=20100601,
                source="tushare",
            ),
        ]
        uni = AShareUniverse(symbols)
        path = tmp_path / "universe.json"
        uni.save(path)
        loaded = AShareUniverse.load(path)
        assert len(loaded) == 1
        assert loaded.symbols[0].status == "delisted"
        assert loaded.symbols[0].delist_date == 20100601

    def test_load_old_format_without_new_fields(self, tmp_path):
        import json
        old_data = {
            "count": 1,
            "symbols": [
                {"raw": "sh600000", "std_code": "SSE.STK.600000", "exchange": "SSE", "code": "600000", "name": "", "product": "STK"}
            ],
            "schema_version": 2,
        }
        path = tmp_path / "universe.json"
        path.write_text(json.dumps(old_data), encoding="utf-8")
        loaded = AShareUniverse.load(path)
        assert len(loaded) == 1
        assert loaded.symbols[0].status == "listed"
        assert loaded.symbols[0].delist_date is None
