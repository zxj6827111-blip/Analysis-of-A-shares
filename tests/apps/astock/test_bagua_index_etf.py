# -*- coding: utf-8 -*-
"""Tests for index (沪深指数) and ETF 卦象查询 support."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore, SymbolRecord
from wtpy.apps.astock.data.providers.base import MarketBar
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service import bagua_query as bq
from wtpy.apps.astock.service import index_etf as ie
from wtpy.apps.astock.service import stock_names


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def make_cfg(**overrides) -> SimpleNamespace:
    base = dict(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        market_data_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Classification / normalization
# ---------------------------------------------------------------------------


def test_classify_symbol():
    assert ie.classify_symbol("sh000001") == "index"   # 上证指数
    assert ie.classify_symbol("sz399001") == "index"   # 深证成指
    assert ie.classify_symbol("sh000300") == "index"   # 沪深300
    assert ie.classify_symbol("sh000688") == "index"   # 科创50
    assert ie.classify_symbol("SSE.IDX.000300") == "index"
    assert ie.classify_symbol("SZSE.IDX.399006") == "index"
    assert ie.classify_symbol("sh510300") == "etf"     # 沪深300ETF
    assert ie.classify_symbol("sh588000") == "etf"     # 科创50ETF
    assert ie.classify_symbol("sz159915") == "etf"     # 创业板ETF
    assert ie.classify_symbol("SZSE.ETF.159915") == "etf"
    # bare codes: unambiguous index/ETF segments only
    assert ie.classify_symbol("399001") == "index"
    assert ie.classify_symbol("510300") == "etf"
    assert ie.classify_symbol("159915") == "etf"
    # stocks (bare 000001 stays SZSE stock, never SSE index)
    assert ie.classify_symbol("000001") == ""
    assert ie.classify_symbol("600000") == ""
    assert ie.classify_symbol("sz000001") == ""
    assert ie.classify_symbol("SSE.STK.600000") == ""
    assert ie.classify_symbol("") == ""


def test_to_index_etf_std_code():
    assert ie.to_index_etf_std_code("sh000001") == "SSE.IDX.000001"
    assert ie.to_index_etf_std_code("sz399001") == "SZSE.IDX.399001"
    assert ie.to_index_etf_std_code("sh510300") == "SSE.ETF.510300"
    assert ie.to_index_etf_std_code("sz159915") == "SZSE.ETF.159915"
    assert ie.to_index_etf_std_code("SSE.IDX.000300") == "SSE.IDX.000300"
    assert ie.to_index_etf_std_code("SZSE.ETF.159919") == "SZSE.ETF.159919"
    assert ie.to_index_etf_std_code("600000") == ""
    assert ie.to_index_etf_std_code("000001") == ""
    assert ie.to_index_etf_std_code("sh000688") == "SSE.IDX.000688"


def test_display_code_index_etf():
    assert ie.display_code("SSE.IDX.000001") == "sh000001"
    assert ie.display_code("SZSE.IDX.399001") == "sz399001"
    assert ie.display_code("SSE.ETF.510300") == "sh510300"
    assert ie.display_code("SZSE.ETF.159915") == "sz159915"


def test_list_etf_std_codes_filters_index(tmp_path):
    """全市场 ETF 枚举（list_etf_std_codes）只保留 .ETF. 段。

    指数（SSE.IDX.* / SZSE.IDX.*）与股票不得混入 ETF 池；结果按标准代码
    排序返回。cfg 参数只用到 cfg.tdx_root。
    """
    sh = tmp_path / "vipdoc" / "sh" / "lday"
    sz = tmp_path / "vipdoc" / "sz" / "lday"
    sh.mkdir(parents=True)
    sz.mkdir(parents=True)
    for name in ("sh000001.day", "sh510050.day", "sh600000.day"):
        (sh / name).write_bytes(b"")  # 上证指数 / 沪深300ETF / 浦发银行
    for name in ("sz399001.day", "sz159915.day"):
        (sz / name).write_bytes(b"")  # 深证成指 / 创业板ETF

    cfg = make_cfg(tdx_root=tmp_path)
    codes = ie.list_etf_std_codes(cfg)
    # sorted(set(out))：字母序，"SSE.ETF.510050" 先于 "SZSE.ETF.159915"
    assert codes == ["SSE.ETF.510050", "SZSE.ETF.159915"]
    assert all(".ETF." in c for c in codes)
    assert not any(".IDX." in c for c in codes)

    # 目录缺失/为空 -> 空列表（不抛异常）
    assert ie.list_etf_std_codes(make_cfg(tdx_root=tmp_path / "missing")) == []
    empty = tmp_path / "empty"
    (empty / "vipdoc" / "sh" / "lday").mkdir(parents=True)
    assert ie.list_etf_std_codes(make_cfg(tdx_root=empty)) == []


def test_normalize_query_code_via_bagua_query():
    assert bq.normalize_query_code("sh000001") == "SSE.IDX.000001"
    assert bq.normalize_query_code("sz399001") == "SZSE.IDX.399001"
    assert bq.normalize_query_code("sh510300") == "SSE.ETF.510300"
    assert bq.normalize_query_code("sz159915") == "SZSE.ETF.159915"
    assert bq.normalize_query_code("600000") == "SSE.STK.600000"
    assert bq.normalize_query_code("000001") == "SZSE.STK.000001"
    assert bq.display_code("SSE.IDX.000001") == "sh000001"
    assert bq.display_code("SSE.ETF.510300") == "sh510300"


def test_resolve_index_etf_name():
    assert ie.resolve_index_etf_name("SSE.IDX.000001") == "上证指数"
    assert ie.resolve_index_etf_name("SZSE.IDX.399001") == "深证成指"
    assert ie.resolve_index_etf_name("SSE.ETF.510300") == "沪深300ETF"
    assert ie.resolve_index_etf_name("SZSE.ETF.159915") == "创业板ETF"
    assert ie.resolve_index_etf_name("SSE.STK.600000") == ""


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


def test_watchlist_presets(tmp_path):
    tdx = tmp_path / "vipdoc"
    # sh000001.day exists; sh510300.day missing
    sh_lday = tdx / "sh" / "lday"
    sh_lday.mkdir(parents=True)
    rec = struct.pack("<IIIIIfII", 20260723, 300000, 310000, 290000, 305000, 1.0, 1, 0)
    (sh_lday / "sh000001.day").write_bytes(rec)
    cfg = make_cfg(tdx_root=tmp_path)

    all_items = ie.watchlist(cfg, kind="all")
    assert len(all_items) >= 19
    by_code = {it["code"]: it for it in all_items}
    idx = by_code["sh000001"]
    assert idx["type"] == "index"
    assert idx["std_code"] == "SSE.IDX.000001"
    assert idx["available"] is True
    assert idx["last_date"] == 20260723
    etf = by_code["sh510300"]
    assert etf["type"] == "etf"
    assert etf["available"] is False
    assert etf["last_date"] is None

    kinds = {it["type"] for it in ie.watchlist(cfg, kind="index")}
    assert kinds == {"index"}
    kinds = {it["type"] for it in ie.watchlist(cfg, kind="etf")}
    assert kinds == {"etf"}
    with pytest.raises(ValueError):
        ie.watchlist(cfg, kind="bogus")


# ---------------------------------------------------------------------------
# query_bagua integration for index / ETF
# ---------------------------------------------------------------------------


def test_query_bagua_index(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20260723, 3800.0, 3810.0, 3790.0, 3805.0, 1.0, 1.0)]
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda _cfg, _std: bars)
    cfg = make_cfg()

    out = bq.query_bagua(cfg, code="sh000001", date="2026-07-23", period="DAY")
    assert out["ok"] is True
    assert out["symbol_type"] == "index"
    assert out["std_code"] == "SSE.IDX.000001"
    assert out["code"] == "sh000001"
    assert out["name"] == "上证指数"
    assert out["display"] == "sh000001 上证指数"
    assert out["adjust"] == "raw"
    assert out["bar"]["open"] == 3800.0
    assert out["bar"]["close"] == 3805.0
    assert any("未复权" in n for n in out["notes"])
    assert out["bagua"]["upper_id"] is not None
    assert out["summary"]["full_name"]


def test_query_bagua_etf_forces_raw(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20260723, 4.78, 4.82, 4.75, 4.80, 1.0, 1.0)]
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda _cfg, _std: bars)
    cfg = make_cfg()

    out = bq.query_bagua(
        cfg, code="sh510300", date="2026-07-23", period="DAY", adjust="tushare_qfq"
    )
    assert out["ok"] is True
    assert out["symbol_type"] == "etf"
    assert out["adjust"] == "raw"
    assert out["std_code"] == "SSE.ETF.510300"
    assert out["name"] == "沪深300ETF"
    assert any("tushare_qfq" in n for n in out["notes"])
    assert any("不适用" in n for n in out["notes"])


def test_query_bagua_tdx_front_disabled(monkeypatch):
    """Explicit tdx_front raises a clear disabled-source error (even ETF)."""
    from wtpy.apps.astock.service.bagua_query import SourceDisabledError

    bars = [DayBar(20260723, 4.78, 4.82, 4.75, 4.80, 1.0, 1.0)]
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda _cfg, _std: bars)
    cfg = make_cfg()

    with pytest.raises(SourceDisabledError, match="已停用"):
        bq.query_bagua(
            cfg, code="sh510300", date="2026-07-23", period="DAY", adjust="tdx_front"
        )


def test_query_bagua_index_week_month(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [
        DayBar(20260720, 3800.0, 3810.0, 3790.0, 3805.0, 1.0, 1.0),
        DayBar(20260721, 3805.0, 3815.0, 3795.0, 3808.0, 1.0, 1.0),
        DayBar(20260722, 3808.0, 3820.0, 3800.0, 3812.0, 1.0, 1.0),
        DayBar(20260723, 3812.0, 3825.0, 3805.0, 3818.0, 1.0, 1.0),
    ]
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda _cfg, _std: bars)
    cfg = make_cfg()

    for per in ("WEEK", "MONTH"):
        out = bq.query_bagua(cfg, code="sz399001", date="2026-07-23", period=per)
        assert out["ok"] is True
        assert out["period"] == per
        assert out["bar"]["end_date"] == 20260723
        assert out["bagua"]["upper_id"] is not None


def test_query_bagua_index_no_bars(monkeypatch):
    monkeypatch.setattr(
        bq, "load_index_etf_day_bars", lambda _cfg, _std: (_ for _ in ()).throw(FileNotFoundError("no bars"))
    )
    cfg = make_cfg()
    with pytest.raises(FileNotFoundError):
        bq.query_bagua(cfg, code="sz399001", date="2026-07-23", period="DAY")


def test_query_bagua_index_warehouse_first(monkeypatch):
    """Warehouse dataset wins: adjust_meta must reflect tushare/none dataset."""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    warehouse_bars = [DayBar(20260731, 3820.0, 3830.0, 3800.0, 3825.0, 1.0, 1.0)]
    calls = {}

    def _fake_dataset(_cfg, code, source_key, asof=None):
        calls["code"], calls["key"], calls["asof"] = code, source_key, asof
        meta = {
            "dataset_id": "tushare_none_1d_20260731_xyz",
            "dataset_source": "tushare",
            "dataset_adjustment": "none",
            "dataset_status": "ready",
            "dataset_cutoff": 20260731,
            "symbol_first_date": 19901219,
            "symbol_last_date": 20260731,
            "symbol_row_count": 8680,
            "covers_asof": True,
            "session_indexed": True,
        }
        return warehouse_bars, meta

    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_dataset)
    cfg = make_cfg()

    out = bq.query_bagua(cfg, code="sh000001", date="2026-07-31", period="DAY")
    assert calls == {"code": "SSE.IDX.000001", "key": "raw", "asof": 20260731}
    assert out["ok"] is True
    assert out["bar"]["close"] == 3825.0
    am = out["adjust_meta"]
    assert am["dataset_source"] == "tushare"
    assert am["dataset_adjustment"] == "none"
    assert am["dataset_id"] == "tushare_none_1d_20260731_xyz"
    assert am["model"] == "warehouse"
    assert am["legacy_fallback"] is False
    assert "Tushare" in am["price_format"]
    assert any("数据仓库" in n for n in out["notes"])


def test_query_bagua_index_warehouse_miss_falls_back_tdx(monkeypatch):
    """Warehouse miss (no index/ETF datasets) falls back to TDX day files."""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20260723, 3800.0, 3810.0, 3790.0, 3805.0, 1.0, 1.0)]

    def _fake_dataset(_cfg, code, source_key, asof=None):
        raise FileNotFoundError("symbol not in warehouse")

    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_dataset)
    monkeypatch.setattr(bq, "load_index_etf_day_bars", lambda _cfg, _std: bars)
    cfg = make_cfg()

    out = bq.query_bagua(cfg, code="sh000001", date="2026-07-23", period="DAY")
    assert out["ok"] is True
    am = out["adjust_meta"]
    assert am["dataset_source"] == "legacy_tdx_day"
    assert am["model"] == "legacy_day_file"
    assert am["legacy_fallback"] is True
    assert "通达信" in out["notes"][0]


def test_query_bagua_index_etf_warehouse_with_formal_pair(tmp_path, monkeypatch):
    """Formal L1/L2 pair exists -> index/ETF must still resolve from the
    latest ready tushare/none warehouse surface (regression), while stock
    queries stay locked to the formal L2 composite (fail-closed)."""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    from types import SimpleNamespace as _NS

    from wtpy.apps.astock.data import tushare_product as tp
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    store = DatasetStore(tmp_path / "market_data")

    def publish(dataset_id, source, adjustment, symbol, dates, *,
                data_policy=None, raw_dataset_id=None):
        bars = [
            MarketBar(
                symbol=symbol,
                trade_date=date,
                period="1d",
                open=10.0,
                high=11.0,
                low=9.0,
                close=float(date % 100),
                volume=1000.0,
                amount=10000.0,
            )
            for date in dates
        ]
        sha = store.store_bars(symbol, bars)
        provenance = {}
        if data_policy:
            provenance["data_policy"] = data_policy
        store.publish(DatasetManifest(
            dataset_id=dataset_id,
            source=source,
            adjustment=adjustment,
            period="1d",
            status="building",
            data_cutoff_date=max(dates),
            symbols=[
                SymbolRecord(
                    symbol=symbol,
                    blob_sha256=sha,
                    row_count=len(bars),
                    quality="ok",
                    first_date=min(dates),
                    last_date=max(dates),
                )
            ],
            symbol_count=1,
            row_count=len(bars),
            raw_dataset_id=raw_dataset_id,
            provenance=provenance,
        ))

    # Formal L2 composite: stocks only (index/ETF are NOT part of the pair).
    formal_l2 = "internal_composite_none_1d_formal_t1"
    publish(formal_l2, "internal", "composite_none", "SZSE.STK.000001",
            list(range(20240102, 20240102 + 130)),
            data_policy="tushare_only_v1")
    # Index/ETF symbols live in separately synced tushare/none datasets.
    publish("tushare_none_1d_index_t1", "tushare", "none", "SSE.IDX.000001",
            list(range(20240102, 20240102 + 130)))
    # A fresher tushare/none stock set must NOT win over the formal L2.
    publish("tushare_none_1d_stock_fresher_t1", "tushare", "none",
            "SZSE.STK.000001", list(range(20240201, 20240201 + 130)))

    fake_pair = _NS(
        l1_dataset_id="internal_composite_tushare_factor_qfq_1d_formal_t1",
        l2_dataset_id=formal_l2,
    )
    monkeypatch.setattr(tp, "resolve_active_tushare_product_pair",
                        lambda store, **k: fake_pair)

    cfg = make_cfg(market_data_root=tmp_path / "market_data",
                   tdx_root=tmp_path / "nope")

    # index: warehouse hit from tushare/none, no legacy fallback
    out = bq.query_bagua(cfg, code="sh000001", date="2024-01-05", period="DAY")
    assert out["ok"] is True
    am = out["adjust_meta"]
    assert am["model"] == "warehouse"
    assert am["dataset_source"] == "tushare"
    assert am["dataset_adjustment"] == "none"
    assert am["dataset_id"] == "tushare_none_1d_index_t1"
    assert am["legacy_fallback"] is False

    # stock: stays locked to the formal L2 composite
    out2 = bq.query_bagua(cfg, code="sz000001", date="2024-01-05", period="DAY")
    assert out2["ok"] is True
    am2 = out2["adjust_meta"]
    assert am2["dataset_source"] == "internal"
    assert am2["dataset_adjustment"] == "composite_none"
    assert am2["dataset_id"] == formal_l2
    assert am2["bootstrap_fallback"] is False
    assert am2["session_indexed"] is True

    # session-level meta: tushare/none is a non-formal fallback for index/ETF,
    # while the stock stays on the formal surface
    session = bq.BaguaPlaneSession(cfg, "raw")
    _, smeta = session.load_symbol("sh000001", asof=20240105)
    assert smeta["dataset_id"] == "tushare_none_1d_index_t1"
    assert smeta["bootstrap_fallback"] is True
    _, smeta2 = session.load_symbol("sz000001", asof=20240105)
    assert smeta2["dataset_id"] == formal_l2
    assert smeta2["bootstrap_fallback"] is False


def test_watchlist_warehouse_availability(tmp_path):
    """watchlist availability must reflect warehouse data (tushare/none)."""
    md = tmp_path / "market_data"
    store = DatasetStore(md)
    bars = [
        MarketBar(
            symbol="SSE.IDX.000001", trade_date=20260731, period="1d",
            open=3820.0, high=3830.0, low=3800.0, close=3825.0,
            volume=1.0, amount=1.0, source="tushare", adjustment="none",
        )
    ]
    sha = store.store_bars("SSE.IDX.000001", bars)
    rec = SymbolRecord(
        symbol="SSE.IDX.000001", blob_sha256=sha,
        first_date=20260731, last_date=20260731, row_count=1, quality="ok",
    )
    m = DatasetManifest(
        dataset_id="tushare_none_1d_20260731_test",
        source="tushare",
        adjustment="none",
        period="1d",
        status="ready",
        snapshot_date=20260731,
        data_cutoff_date=20260731,
        provider_version="tushare_test",
        sync_run_id="run_test",
        created_at="2026-07-31T00:00:00",
        symbols=[rec],
    )
    store.publish(m)

    cfg = make_cfg(market_data_root=md, tdx_root=tmp_path / "nope")
    by_code = {it["code"]: it for it in ie.watchlist(cfg, kind="all")}
    assert by_code["sh000001"]["available"] is True
    assert by_code["sh000001"]["last_date"] == 20260731
    # not in warehouse and no TDX file -> unavailable
    assert by_code["sh510300"]["available"] is False
    assert by_code["sh510300"]["last_date"] is None


def test_batch_query_mixed_index_stock(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    stock_bars = [DayBar(20260723, 10.0, 10.5, 9.8, 10.2, 1.0, 1.0)]
    idx_bars = [DayBar(20260723, 3800.0, 3810.0, 3790.0, 3805.0, 1.0, 1.0)]

    def _fake_dataset(_cfg, _code, source_key, asof=None):
        raise FileNotFoundError("no warehouse (test)")

    def _fake_legacy(_cfg, _code):
        return stock_bars

    def _fake_index_etf(_cfg, _std):
        return idx_bars

    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_dataset)
    monkeypatch.setattr(bq, "load_day_bars", _fake_legacy)
    monkeypatch.setattr(bq, "load_index_etf_day_bars", _fake_index_etf)
    cfg = make_cfg()

    out = bq.batch_query_bagua(
        cfg,
        codes=["600000", "sh000001", "sh510300"],
        date="2026-07-23",
        period="DAY",
        adjust="raw",
    )
    assert out["ok_count"] == 3
    types = {r["code"]: r["symbol_type"] for r in out["results"]}
    assert types["sh600000"] == "stock"
    assert types["sh000001"] == "index"
    assert types["sh510300"] == "etf"


# ---------------------------------------------------------------------------
# Constituents (成分股) from TDX local files
# ---------------------------------------------------------------------------


def _write_fake_tdx(tmp_path, spec_lines=None, block_lines=None):
    hq = tmp_path / "T0002" / "hq_cache"
    hq.mkdir(parents=True, exist_ok=True)
    if spec_lines is not None:
        (hq / "specetfdata.txt").write_text("\n".join(spec_lines), encoding="gbk")
    if block_lines is not None:
        (hq / "infoharbor_block.dat").write_text("\n".join(block_lines), encoding="gbk")
    return make_cfg(tdx_root=tmp_path)


def _fake_stock_names(cfg, disp, std_code=""):
    return {"sh600000": "浦发银行", "sz000001": "平安银行"}.get(disp, "")


def test_load_spec_etf_map(tmp_path):
    cfg = _write_fake_tdx(
        tmp_path,
        spec_lines=[
            "1,510300,000300,1,800300,20260512,",
            "0,159915,399006,1,399006,20260512,",
            "0,159919,300,1,399300,20260512,",   # index code not zero-padded
            "junk line without commas",
            "2,510500,000905,1,",                # bad market code -> skipped
            "1,510050,",                          # missing index -> skipped
        ],
    )
    spec = ie.load_spec_etf_map(cfg)
    assert spec["510300"] == "000300"
    assert spec["159915"] == "399006"
    assert spec["159919"] == "000300"  # zfilled
    assert "510500" not in spec
    assert "510050" not in spec
    # empty/missing file -> {}
    cfg2 = make_cfg(tdx_root=tmp_path / "missing")
    assert ie.load_spec_etf_map(cfg2) == {}


def test_load_block_constituents_names_and_members(tmp_path):
    cfg = _write_fake_tdx(
        tmp_path,
        block_lines=[
            "#ZS_沪深300,300,000300,20050408,20260512,,",
            "0#000001,0#000063,1#600000,1#600519",
            "#ZS_上证50,50,000016,20040102,20260512,,",
            "1#600000,1#600016",
            "#普通自选板块,5,999999,20200101,20260512,,",
            "0#000001",
        ],
    )
    blocks = ie.load_block_constituents(cfg)
    # ZS_ is 3 chars: name must be stripped, not off-by-one
    assert set(blocks) == {"沪深300", "上证50"}
    assert blocks["沪深300"] == [
        (0, "000001"),
        (0, "000063"),
        (1, "600000"),
        (1, "600519"),
    ]
    assert blocks["上证50"] == [(1, "600000"), (1, "600016")]
    # non-ZS block must not appear
    assert "普通自选板块" not in blocks
    # missing file -> {}
    cfg2 = make_cfg(tdx_root=tmp_path / "missing")
    assert ie.load_block_constituents(cfg2) == {}


def test_index_constituents(tmp_path, monkeypatch):
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _write_fake_tdx(
        tmp_path,
        block_lines=[
            "#ZS_沪深300,300,000300,20050408,20260512,,",
            "0#000001,0#000063,1#600000,1#600519",
            "#ZS_中证500,500,000905,20070115,20260512,,",
            "0#000001,1#600000,0#002415,1#601318",
        ],
    )
    out = ie.index_constituents(cfg, "SSE.IDX.000300")
    assert out["ok"] is True
    assert out["symbol_type"] == "index"
    assert out["code"] == "sh000300"
    assert out["name"] == "沪深300"
    assert out["count"] == 4
    assert out["note"] == ""
    assert out["source"] == "tdx_infoharbor_block:ZS_沪深300"
    codes = [(c["code"], c["name"]) for c in out["constituents"]]
    assert codes == [
        ("sz000001", "平安银行"),
        ("sz000063", ""),
        ("sh600000", "浦发银行"),
        ("sh600519", ""),
    ]
    # limit slices
    assert len(ie.index_constituents(cfg, "SSE.IDX.000300", limit=2)["constituents"]) == 2
    # block missing -> helpful note, empty constituents
    cfg2 = _write_fake_tdx(
        tmp_path,
        block_lines=["#ZS_上证50,50,000016,20040102,20260512,,", "1#600000"],
    )
    out2 = ie.index_constituents(cfg2, "SSE.IDX.000300")
    assert out2["count"] == 0
    assert "未收录" in out2["note"]


def test_etf_constituents(tmp_path, monkeypatch):
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _write_fake_tdx(
        tmp_path,
        spec_lines=[
            "1,510300,000300,1,800300,20260512,",
            "0,159915,399006,1,399006,20260512,",
            "1,510500,000905,1,800905,20260512,",
            "1,510880,000015,1,800015,20260512,",
        ],
        block_lines=[
            "#ZS_沪深300,300,000300,20050408,20260512,,",
            "0#000001,1#600000",
            "#ZS_创业板指,100,399006,20100601,20260512,,",
            "0#300001,0#300002",
            "#ZS_上证红利,50,000015,20050104,20260512,,",
            "1#600015,1#600028",
        ],
    )
    out = ie.etf_constituents(cfg, "SSE.ETF.510300")
    assert out["ok"] is True
    assert out["symbol_type"] == "etf"
    assert out["name"] == "沪深300ETF"
    assert out["tracked_index"] == "000300"
    assert out["tracked_index_name"] == "沪深300"
    assert out["count"] == 2
    assert [c["code"] for c in out["constituents"]] == ["sz000001", "sh600000"]
    assert out["note"] == ""

    # ETF without spec mapping -> note
    out2 = ie.etf_constituents(cfg, "SSE.ETF.512100")
    assert out2["count"] == 0
    assert out2["tracked_index"] == ""
    assert "specetfdata 未收录" in out2["note"]

    # ETF whose tracked index block is missing -> note
    out3 = ie.etf_constituents(cfg, "SSE.ETF.510500")
    assert out3["tracked_index"] == "000905"
    assert out3["tracked_index_name"] == "中证500"
    assert out3["count"] == 0
    assert "ZS_ 板块未收录" in out3["note"]


def test_resolve_constituents_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(stock_names, "resolve_stock_name", _fake_stock_names)
    cfg = _write_fake_tdx(
        tmp_path,
        spec_lines=["0,159915,399006,1,399006,20260512,"],
        block_lines=["#ZS_创业板指,100,399006,20100601,20260512,,", "0#300001"],
    )
    out = ie.resolve_constituents(cfg, "SZSE.ETF.159915")
    assert out["symbol_type"] == "etf"
    assert out["count"] == 1
    out2 = ie.resolve_constituents(cfg, "SZSE.IDX.399006")
    assert out2["symbol_type"] == "index"
    assert out2["count"] == 1
    with pytest.raises(ValueError):
        ie.resolve_constituents(cfg, "SSE.STK.600000")


@pytest.mark.skipif(
    not Path("D:/通达信/T0002/hq_cache").exists(),
    reason="local TDX installation required",
)
def test_constituents_real_tdx_data():
    """On machines with TDX installed, the block names must resolve (guards the ZS_ slice)."""
    cfg = make_cfg(tdx_root=Path("D:/通达信"))
    out = ie.index_constituents(cfg, "SSE.IDX.000300")
    assert out["count"] > 200, out["note"]
    etf = ie.etf_constituents(cfg, "SSE.ETF.510300")
    assert etf["count"] > 200, etf["note"]
