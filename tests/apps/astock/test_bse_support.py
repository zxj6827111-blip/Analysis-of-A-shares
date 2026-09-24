# -*- coding: utf-8 -*-
"""北交所（BSE）数据支持：代码归一、显示、涨跌幅、名单元数据。"""

from __future__ import annotations

from types import SimpleNamespace

from wtpy.apps.astock.data.limit_rules import (
    DefaultAShareLimitRule,
    LimitContext,
    infer_board,
)
from wtpy.apps.astock.service.bagua_query import (
    display_code,
    normalize_query_code,
)


def _ctx(std_code: str) -> LimitContext:
    return LimitContext(
        std_code=std_code, date=20260924, prev_close=10.0,
        open=10.0, high=10.0, low=10.0, close=10.0,
    )


def test_normalize_bse_passthrough_and_variants():
    assert normalize_query_code("BSE.STK.920001") == "BSE.STK.920001"
    assert normalize_query_code("920001.BJ") == "BSE.STK.920001"
    assert normalize_query_code("bj430047") == "BSE.STK.430047"
    # 标准 SSE/SZSE 不变
    assert normalize_query_code("600000.SH") == "SSE.STK.600000"
    assert normalize_query_code("SSE.STK.600000") == "SSE.STK.600000"


def test_display_code_bse_prefix():
    assert display_code("BSE.STK.920001") == "bj920001"
    assert display_code("BSE.STK.430047") == "bj430047"
    assert display_code("SSE.STK.600000") == "sh600000"
    assert display_code("SSE.IDX.000001") == "sh000001"


def test_bse_limit_is_30pct_regardless_of_st():
    rule = DefaultAShareLimitRule()
    assert rule.limit_pct(_ctx("BSE.STK.920001")) == 0.30
    ctx_st = _ctx("BSE.STK.830799")
    ctx_st.is_st = True
    # 北交所无 ST 5% 特例：*ST 也是 30%
    assert rule.limit_pct(ctx_st) == 0.30
    # 猜不到 BSE.* 前缀时按代码段兜底（43/83/87/92）
    assert rule.limit_pct(_ctx("SZSE.STK.430047")) == 0.30
    # 沪深口径不受影响
    assert rule.limit_pct(_ctx("SSE.STK.600000")) == 0.10
    ctx30 = _ctx("SZSE.STK.300999")
    assert rule.limit_pct(ctx30) == 0.20
    ctx_st2 = _ctx("SSE.STK.600000")
    ctx_st2.is_st = True
    assert rule.limit_pct(ctx_st2) == 0.05


def test_infer_board_bse():
    assert infer_board("BSE.STK.920001") == "bse"
    assert infer_board("BSE.STK.430047") == "bse"
    assert infer_board("SZSE.STK.300001") == "chinext"
    assert infer_board("SSE.STK.688001") == "star"
    assert infer_board("SSE.STK.600000") == "main"


def test_fetch_symbol_meta_includes_bse(monkeypatch, tmp_path):
    """名称/上市日期元数据缓存必须含北交所（否则北交所票名称列整列空）。"""
    from wtpy.apps.astock.service import bagua_query as bq

    captured = {}

    class _FakeProvider:
        def __init__(self, token=None):
            pass

        def fetch_universe(self, *, include_delisted=False, include_bse=False):
            captured["include_bse"] = include_bse
            return [
                SimpleNamespace(
                    symbol="BSE.STK.920001", name="凯达华", list_date=20260923,
                    source="tushare", status="listed",
                ),
                SimpleNamespace(
                    symbol="SSE.STK.600000", name="浦发银行", list_date=19991110,
                    source="tushare", status="listed",
                ),
            ]

        def fetch_index_etf_universe(self, end_date=None):
            return []

    from wtpy.apps.astock.data.providers import tushare as ts_mod

    monkeypatch.setattr(ts_mod, "TushareProvider", _FakeProvider)
    stocks, etfs, stock_names, etf_names = bq._fetch_symbol_meta_from_tushare(
        SimpleNamespace(storage_root=tmp_path)
    )
    assert captured.get("include_bse") is True
    assert stocks.get("920001") == 20260923
    assert stock_names.get("920001") == "凯达华"
    assert stocks.get("600000") == 19991110
