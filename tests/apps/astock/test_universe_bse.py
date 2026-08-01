"""Tests for BSE (Beijing Stock Exchange) universe support."""
import pytest
from pathlib import Path

from wtpy.apps.astock.data.universe import (
    AShareUniverse,
    SymbolInfo,
    is_ashare_code,
    is_bse_code,
    to_std_code,
)


class TestBseCodeDetection:
    def test_bse_4_prefix(self):
        assert is_bse_code("bj430047") is True
        assert is_bse_code("430047") is True

    def test_bse_8_prefix(self):
        assert is_bse_code("bj830799") is True
        assert is_bse_code("830799") is True

    def test_non_bse(self):
        assert is_bse_code("sh600000") is False
        assert is_bse_code("sz000001") is False
        assert is_bse_code("600000") is False

    def test_is_ashare_excludes_bse(self):
        assert is_ashare_code("bj430047") is False
        assert is_ashare_code("430047") is False
        assert is_ashare_code("830799") is False


class TestBseStdCode:
    def test_bj_prefix(self):
        assert to_std_code("bj430047") == "BSE.STK.430047"

    def test_bare_4_prefix(self):
        assert to_std_code("430047") == "BSE.STK.430047"

    def test_bare_8_prefix(self):
        assert to_std_code("830799") == "BSE.STK.830799"


class TestBseUniverse:
    def test_from_tdx_dirs_excludes_bse_by_default(self, tmp_path):
        sh_dir = tmp_path / "sh" / "lday"
        sz_dir = tmp_path / "sz" / "lday"
        bj_dir = tmp_path / "bj" / "lday"
        sh_dir.mkdir(parents=True)
        sz_dir.mkdir(parents=True)
        bj_dir.mkdir(parents=True)
        (sh_dir / "sh600000.day").write_bytes(b"\x00" * 32)
        (bj_dir / "bj430047.day").write_bytes(b"\x00" * 32)

        uni = AShareUniverse.from_tdx_dirs(sh_dir, sz_dir, include_bj=False, bj_dir=bj_dir)
        codes = uni.codes()
        assert "SSE.STK.600000" in codes
        assert "BSE.STK.430047" not in codes

    def test_from_tdx_dirs_includes_bse_when_requested(self, tmp_path):
        sh_dir = tmp_path / "sh" / "lday"
        sz_dir = tmp_path / "sz" / "lday"
        bj_dir = tmp_path / "bj" / "lday"
        sh_dir.mkdir(parents=True)
        sz_dir.mkdir(parents=True)
        bj_dir.mkdir(parents=True)
        (sh_dir / "sh600000.day").write_bytes(b"\x00" * 32)
        (bj_dir / "bj430047.day").write_bytes(b"\x00" * 32)

        uni = AShareUniverse.from_tdx_dirs(sh_dir, sz_dir, include_bj=True, bj_dir=bj_dir)
        codes = uni.codes()
        assert "SSE.STK.600000" in codes
        assert "BSE.STK.430047" in codes

    def test_bse_symbol_info(self, tmp_path):
        bj_dir = tmp_path / "bj" / "lday"
        bj_dir.mkdir(parents=True)
        sh_dir = tmp_path / "sh" / "lday"
        sz_dir = tmp_path / "sz" / "lday"
        sh_dir.mkdir(parents=True)
        sz_dir.mkdir(parents=True)
        (bj_dir / "bj830799.day").write_bytes(b"\x00" * 32)

        uni = AShareUniverse.from_tdx_dirs(sh_dir, sz_dir, include_bj=True, bj_dir=bj_dir)
        bse_syms = [s for s in uni.symbols if s.exchange == "BSE"]
        assert len(bse_syms) == 1
        assert bse_syms[0].code == "830799"
        assert bse_syms[0].std_code == "BSE.STK.830799"
