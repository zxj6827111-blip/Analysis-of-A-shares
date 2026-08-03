# -*- coding: utf-8 -*-
"""Tests for sync_market_data.py index/ETF support (offline)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "sync_market_data.py"
)

_sync = None


def _load_module():
    global _sync
    if _sync is None:
        spec = importlib.util.spec_from_file_location(
            "sync_market_data_test_mod", SCRIPT
        )
        _sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sync)
    return _sync


def test_normalize_symbol_index_etf():
    m = _load_module()
    norm = m._normalize_symbol
    assert norm("sh000001") == "SSE.IDX.000001"
    assert norm("000001.SH") == "SSE.IDX.000001"
    assert norm("SSE.IDX.000001") == "SSE.IDX.000001"
    assert norm("sz399001") == "SZSE.IDX.399001"
    assert norm("sh510300") == "SSE.ETF.510300"
    assert norm("510300.SH") == "SSE.ETF.510300"
    assert norm("sz159915") == "SZSE.ETF.159915"
    # stocks unchanged
    assert norm("600000.SH") == "SSE.STK.600000"
    assert norm("sh600000") == "SSE.STK.600000"
    assert norm("000001.SZ") == "SZSE.STK.000001"
    assert norm("600000") == "SSE.STK.600000"
    assert norm("000001") == "SZSE.STK.000001"
    assert norm("430047.BJ") == "BSE.STK.430047"


def _fake_args(**kw):
    base = dict(
        symbol=None, asset_class="index", token=None,
        start_date=None, end_date=None, anchor_date=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_index_etf_symbols_universe_filter(monkeypatch):
    m = _load_module()
    provider = MagicMock()
    provider.fetch_index_etf_universe.return_value = [
        SimpleNamespace(symbol="SSE.IDX.000001"),
        SimpleNamespace(symbol="SZSE.IDX.399001"),
        SimpleNamespace(symbol="SSE.ETF.510300"),
        SimpleNamespace(symbol="SZSE.ETF.159915"),
    ]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="index"), provider)
    assert sorted(got) == ["SSE.IDX.000001", "SZSE.IDX.399001"]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="etf"), provider)
    assert sorted(got) == ["SSE.ETF.510300", "SZSE.ETF.159915"]
    got = m._resolve_index_etf_symbols(_fake_args(asset_class="all"), provider)
    assert len(got) == 4


def test_resolve_index_etf_symbols_symbol_wins(monkeypatch):
    m = _load_module()
    provider = MagicMock()
    provider.fetch_index_etf_universe.side_effect = AssertionError("must not be called")
    got = m._resolve_index_etf_symbols(
        _fake_args(asset_class="index", symbol="sh000001, sh510300"), provider
    )
    assert got == ["sh000001", "sh510300"]


def test_index_etf_configs_none_only():
    m = _load_module()
    configs = m._index_etf_configs()
    assert len(configs) == 1
    adj, period = configs[0]
    assert adj == m.AdjustmentMode.NONE
    assert period == m.BarPeriod.DAY


def test_sync_tushare_index_etf_full(monkeypatch):
    m = _load_module()

    from wtpy.apps.astock.data.providers.base import MarketBar
    from wtpy.apps.astock.data.providers.tushare import TushareProvider

    bar = MarketBar(
        symbol="SSE.IDX.000001", trade_date=20260731, period="1d",
        open=3800.0, high=3810.0, low=3790.0, close=3805.0,
        volume=1.0, amount=1.0, source="tushare", adjustment="none",
    )
    # Patch the real provider class so the function never hits the live API.
    monkeypatch.setattr(TushareProvider, "health_check", lambda self: True)
    monkeypatch.setattr(
        TushareProvider, "fetch_index_etf_universe",
        lambda self: [SimpleNamespace(symbol="SSE.IDX.000001")],
    )
    monkeypatch.setattr(TushareProvider, "fetch_bars", lambda self, req: [bar])

    def _fake_sync_dataset(**kw):
        return {
            "success": 1,
            "total": 1,
            "dataset_id": "tushare_none_1d_20260731_test_ie",
            "no_data": 0,
            "failed": 0,
            "errors": [],
        }

    fake_sync = MagicMock(side_effect=_fake_sync_dataset)
    monkeypatch.setattr(m, "_sync_dataset", fake_sync)
    store = MagicMock()
    result = m.sync_tushare_index_etf_full(_fake_args(), store)
    assert result["status"] == "success"
    assert result["datasets"]["none_1d"]["success"] == 1
    assert "qfq_1d" not in result["datasets"]
    req = fake_sync.call_args.kwargs
    assert req["adjustment"] == m.AdjustmentMode.NONE
    assert req["symbols"] == ["SSE.IDX.000001"]
