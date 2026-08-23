# -*- coding: utf-8 -*-
"""Tests for MarketDataRepository symbol-variant resolution with index/ETF codes."""

from __future__ import annotations

from wtpy.apps.astock.data.repository import MarketDataRepository

V = MarketDataRepository._symbol_variants


def test_variants_stock_unchanged():
    assert "SSE.STK.600000" in V("600000.SH")
    assert "SSE.STK.600000" in V("sh600000")
    assert "SZSE.STK.000001" in V("000001.SZ")
    assert "SZSE.STK.000001" in V("sz000001")
    assert "BSE.STK.430047" in V("430047.BJ")
    assert "BSE.STK.430047" in V("bj430047")


def test_variants_index():
    assert "SSE.IDX.000001" in V("SSE.IDX.000001")
    assert "000001.SH" in V("SSE.IDX.000001")
    assert "sh000001" in V("SSE.IDX.000001")
    # .SH + 000xxx is an SSE index, never a stock
    assert "SSE.IDX.000001" in V("000001.SH")
    assert "SSE.IDX.000001" in V("sh000001")
    assert "SSE.STK.000001" not in V("000001.SH")
    assert "SSE.IDX.000300" in V("000300.SH")
    assert "SSE.IDX.000300" in V("sh000300")
    # SZSE 399xxx indices
    assert "SZSE.IDX.399001" in V("399001.SZ")
    assert "SZSE.IDX.399001" in V("sz399001")
    assert "SZSE.IDX.399006" in V("399006.SZ")
    assert "SZSE.STK.399001" not in V("399001.SZ")


def test_variants_etf():
    assert "SSE.ETF.510300" in V("510300.SH")
    assert "SSE.ETF.510300" in V("sh510300")
    assert "SSE.ETF.588000" in V("sh588000")
    assert "SZSE.ETF.159915" in V("159915.SZ")
    assert "SZSE.ETF.159915" in V("sz159915")
    # 新段：沪 52/530/551、深 158（服务器实测已有真实行情）
    assert "SSE.ETF.520740" in V("520740.SH")
    assert "SSE.ETF.520740" in V("520740")
    assert "SSE.ETF.530060" in V("530060.SH")
    assert "SSE.ETF.530060" in V("530060")
    assert "SSE.ETF.551060" in V("551060.SH")
    assert "SSE.ETF.551900" in V("551900.SH")
    assert "SZSE.ETF.158012" in V("158012.SZ")
    assert "SZSE.ETF.158012" in V("158012")
    # 550xxx 保持非 ETF：不得因 551 新段被连带误收
    assert "SSE.ETF.550001" not in V("550001.SH")
    assert "SSE.ETF.550001" not in V("550001")
    # 161725 是 LOF：点形式/裸代码不得再展开成 .ETF. 规范形态
    # （旧规则把整个深市 15/16/18 段当 ETF，是 LOF 污染的源头之一；
    #  已入库的旧污染记录仍可用规范形态 SZSE.ETF.161725 直接查询）
    assert "SZSE.ETF.161725" not in V("161725.SZ")
    assert "SZSE.ETF.161725" not in V("161725")
    assert "SSE.ETF.501300" not in V("501300.SH")
    # ETFs never become stocks
    assert "SSE.STK.510300" not in V("510300.SH")


def test_variants_roundtrip_lookup():
    """A manifest record under one spelling must be found from any spelling."""
    rec = "SSE.IDX.000001"
    # bare 000001 is deliberately kept a stock (SZSE 000001 平安银行),
    # so only the exchange-explicit spellings participate.
    for probe in ("SSE.IDX.000001", "000001.SH", "sh000001"):
        assert rec in V(probe), probe


def test_symbol_kind_std():
    k = MarketDataRepository._symbol_kind_std
    assert k("000001", "SH") == "IDX"
    assert k("600000", "SH") == "STK"
    assert k("510300", "SH") == "ETF"
    assert k("399001", "SZ") == "IDX"
    assert k("000001", "SZ") == "STK"
    assert k("159915", "SZ") == "ETF"
    assert k("430047", "BJ") == "STK"
