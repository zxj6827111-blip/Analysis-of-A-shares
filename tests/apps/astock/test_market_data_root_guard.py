"""Production data-root guard + .env loader (Gate A 防呆)."""
from pathlib import Path

import pytest

from wtpy.apps.astock.config import (
    AStockConfig,
    load_env_file,
    market_data_root_guard,
)


@pytest.fixture
def cfg(tmp_path):
    return AStockConfig(
        storage_root=str(tmp_path / "storage"),
        output_root=str(tmp_path / "out"),
        tdx_root=str(tmp_path / "tdx"),
    )


class TestGuard:
    def test_production_without_env_blocked(self, cfg, monkeypatch):
        monkeypatch.setenv("ASTOCK_ENV", "production")
        monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
        monkeypatch.delenv("ASTOCK_ALLOW_INTERNAL_DATA_ROOT", raising=False)
        g = market_data_root_guard(cfg)
        assert g["blocked"] is True
        assert "MARKET_DATA_ROOT" in g["reason"]

    def test_production_internal_root_blocked(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("ASTOCK_ENV", "production")
        monkeypatch.setenv(
            "MARKET_DATA_ROOT", str(Path(cfg.storage_root) / "market_data")
        )
        monkeypatch.delenv("ASTOCK_ALLOW_INTERNAL_DATA_ROOT", raising=False)
        g = market_data_root_guard(cfg)
        assert g["is_internal"] is True
        assert g["blocked"] is True

    def test_production_external_root_ok(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setenv("ASTOCK_ENV", "production")
        monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path / "formal_md"))
        g = market_data_root_guard(cfg)
        assert g["blocked"] is False
        assert g["is_internal"] is False

    def test_development_internal_not_blocked_but_flagged(self, cfg, monkeypatch):
        monkeypatch.setenv("ASTOCK_ENV", "development")
        monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
        g = market_data_root_guard(cfg)
        assert g["blocked"] is False
        assert g["is_internal"] is True

    def test_explicit_override_unblocks(self, cfg, monkeypatch):
        monkeypatch.setenv("ASTOCK_ENV", "production")
        monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
        monkeypatch.setenv("ASTOCK_ALLOW_INTERNAL_DATA_ROOT", "1")
        g = market_data_root_guard(cfg)
        assert g["blocked"] is False

    def test_env_property_default_development(self, cfg, monkeypatch):
        monkeypatch.delenv("ASTOCK_ENV", raising=False)
        assert cfg.astock_env == "development"


class TestLoadEnvFile:
    def test_parses_and_sets_missing_only(self, tmp_path, monkeypatch):
        envf = tmp_path / ".env"
        envf.write_text(
            "# comment\nFOO_GATEA=abc\nBAR_GATEA=\"quoted\"\nEXIST_GATEA=file\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("FOO_GATEA", raising=False)
        monkeypatch.delenv("BAR_GATEA", raising=False)
        monkeypatch.setenv("EXIST_GATEA", "env-wins")
        parsed = load_env_file(envf)
        import os
        assert parsed["FOO_GATEA"] == "abc"
        assert os.environ["FOO_GATEA"] == "abc"
        assert os.environ["BAR_GATEA"] == "quoted"
        assert os.environ["EXIST_GATEA"] == "env-wins"  # existing env wins
        monkeypatch.delenv("FOO_GATEA", raising=False)
        monkeypatch.delenv("BAR_GATEA", raising=False)

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_env_file(tmp_path / "nope.env") == {}
