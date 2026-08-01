# -*- coding: utf-8 -*-
"""Gate C phase 2: TdxQuantProvider normalization semantics (all mocked).

Covers: fill_data=False mandatory; empty-response classification; affine
front adjustment may yield <=0 prices (preserved); none-mode positivity;
amount 万元->元 scaling; suspension-day NaN skip; date filtering; unique
per-process connection names (crash recovery).
"""
from __future__ import annotations

import os
import types

import pytest

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

import wtpy.apps.astock.data.providers.tdxquant as tdx_mod
from wtpy.apps.astock.data.providers.tdxquant import (
    AMOUNT_UNIT_SCALE,
    TdxQuantProvider,
)
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    MarketDataRequest,
)

pytestmark = pytest.mark.skipif(not HAS_PANDAS, reason="pandas not available")


def _wide(data_by_symbol, dates):
    """Build a tqcenter-style wide response {field: DataFrame}."""
    idx = pd.DatetimeIndex(dates)
    fields = {}
    for f in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        cols = {}
        for sym, rows in data_by_symbol.items():
            cols[sym] = [r.get(f, float("nan")) for r in rows]
        fields[f] = pd.DataFrame(cols, index=idx)
    return fields


class FakeTq:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_market_data(self, **kw):
        self.calls.append(kw)
        return self.response


def _provider(response):
    p = TdxQuantProvider(tdx_root="X:/nonexistent", batch_size=10)
    p._tq = FakeTq(response)
    p._initialized = True
    return p


def _req(symbols, adjustment=AdjustmentMode.FRONT, **kw):
    return MarketDataRequest(symbols=symbols, period=BarPeriod.DAY,
                             adjustment=adjustment, **kw)


class TestFillDataAndEmptyResponse:
    def test_fill_data_false_is_passed(self):
        rows = [{"Open": 1, "High": 2, "Low": 0.5, "Close": 1.5,
                 "Volume": 100, "Amount": 10}]
        p = _provider(_wide({"000001.SZ": rows}, ["2026-07-24"]))
        p.fetch_bars(_req(["000001.SZ"]))
        assert all(c.get("fill_data") is False for c in p._tq.calls)

    def test_empty_dict_single_symbol_is_no_data(self):
        p = _provider({})
        assert p.fetch_bars(_req(["300104.SZ"])) == []

    def test_empty_dict_multi_symbol_falls_back_to_singles(self):
        p = _provider({})
        bars = p.fetch_bars(_req(["000001.SZ", "300104.SZ"]))
        assert bars == []
        # batch attempt + one single attempt per symbol
        sym_lists = [c["stock_list"] for c in p._tq.calls]
        assert ["000001.SZ", "300104.SZ"] in sym_lists
        assert ["000001.SZ"] in sym_lists and ["300104.SZ"] in sym_lists


class TestFrontAffineSemantics:
    def test_front_negative_prices_preserved(self):
        rows = [
            {"Open": -3.10, "High": -3.05, "Low": -3.20, "Close": -3.12,
             "Volume": 1000, "Amount": 10},
            {"Open": 2.0, "High": 2.2, "Low": 1.9, "Close": 2.1,
             "Volume": 1200, "Amount": 12},
        ]
        p = _provider(_wide({"000001.SZ": rows}, ["1995-01-03", "2026-07-24"]))
        bars = p.fetch_bars(_req(["000001.SZ"], AdjustmentMode.FRONT))
        assert len(bars) == 2
        assert bars[0].close == pytest.approx(-3.12)
        assert bars[0].low == pytest.approx(-3.20)

    def test_none_mode_drops_nonpositive_closes(self):
        rows = [
            {"Open": -3.10, "High": -3.05, "Low": -3.20, "Close": -3.12,
             "Volume": 1000, "Amount": 10},
            {"Open": 2.0, "High": 2.2, "Low": 1.9, "Close": 2.1,
             "Volume": 1200, "Amount": 12},
        ]
        p = _provider(_wide({"000001.SZ": rows}, ["1995-01-03", "2026-07-24"]))
        bars = p.fetch_bars(_req(["000001.SZ"], AdjustmentMode.NONE))
        assert [b.trade_date for b in bars] == [20260724]


class TestUnitsAndFiltering:
    def test_amount_scaled_wan_yuan_to_yuan_volume_unchanged(self):
        rows = [{"Open": 28.51, "High": 28.59, "Low": 28.16, "Close": 28.38,
                 "Volume": 1635320.0, "Amount": 4633.74}]
        p = _provider(_wide({"301107.SZ": rows}, ["2026-05-11"]))
        bars = p.fetch_bars(_req(["301107.SZ"]))
        assert bars[0].volume == pytest.approx(1635320.0)
        assert bars[0].amount == pytest.approx(4633.74 * AMOUNT_UNIT_SCALE)
        assert AMOUNT_UNIT_SCALE == 10000.0

    def test_suspension_day_nan_close_emits_no_bar(self):
        dates = ["2026-07-23", "2026-07-24"]
        resp = _wide({
            "AAAAAA.SZ": [
                {"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1, "Amount": 1},
                {"Open": 2, "High": 2, "Low": 2, "Close": 2, "Volume": 2, "Amount": 2},
            ],
            "BBBBBB.SZ": [
                {},  # suspended: all NaN with fill_data=False
                {"Open": 5, "High": 5, "Low": 5, "Close": 5, "Volume": 5, "Amount": 5},
            ],
        }, dates)
        p = _provider(resp)
        bars = p.fetch_bars(_req(["AAAAAA.SZ", "BBBBBB.SZ"]))
        by_sym = {}
        for b in bars:
            by_sym.setdefault(b.symbol, []).append(b.trade_date)
        assert by_sym["AAAAAA.SZ"] == [20260723, 20260724]
        assert by_sym["BBBBBB.SZ"] == [20260724]

    def test_start_end_date_filtering(self):
        rows = [{"Open": i, "High": i, "Low": i, "Close": i,
                 "Volume": 1, "Amount": 1} for i in (1, 2, 3)]
        p = _provider(_wide({"000001.SZ": rows},
                            ["2026-07-22", "2026-07-23", "2026-07-24"]))
        bars = p.fetch_bars(_req(["000001.SZ"],
                                 start_date=20260723, end_date=20260723))
        assert [b.trade_date for b in bars] == [20260723]


class TestConnectionName:
    def test_unique_per_process_connection_name(self, monkeypatch, tmp_path):
        recorded = []

        class DummyTq:
            @classmethod
            def initialize(cls, path):
                recorded.append(path)

        dummy_mod = types.SimpleNamespace(tq=DummyTq)
        monkeypatch.setattr(tdx_mod, "_load_tqcenter", lambda root: dummy_mod)
        p1 = TdxQuantProvider(tdx_root=tmp_path)
        p2 = TdxQuantProvider(tdx_root=tmp_path)
        p1._ensure_initialized()
        p2._ensure_initialized()
        assert len(recorded) == 2
        assert recorded[0] != recorded[1]  # in-process sequence disambiguates
        assert str(os.getpid()) in recorded[0]
        # never the historical fixed name that a killed process leaves stuck
        assert "probe.py" not in recorded[0]


class TestVersionAndCapabilities:
    def test_read_tqcenter_version_missing_file(self, tmp_path):
        assert tdx_mod.read_tqcenter_version(tmp_path) == "unavailable"

    def test_capabilities_bse_supported_delisted_not(self):
        caps = TdxQuantProvider(tdx_root="X:/none").capabilities()
        assert caps.supports_bse is True
        assert caps.supports_delisted is False
