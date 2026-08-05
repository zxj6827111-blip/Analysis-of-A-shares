# -*- coding: utf-8 -*-
"""Tests for bagua single-stock query service."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.bagua.calculator import BaguaCalculator
from wtpy.apps.astock.data.tdx_reader import DayBar
from wtpy.apps.astock.service import bagua_query as bq


JSON_PATH = (
    Path(__file__).resolve().parents[3]
    / "wtpy"
    / "apps"
    / "astock"
    / "bagua"
    / "bagua_384.json"
)


def test_normalize_period_and_code():
    assert bq.normalize_period("日") == "DAY"
    assert bq.normalize_period("按月") == "MONTH"
    assert bq.normalize_period("WEEK") == "WEEK"
    assert bq.normalize_query_code("600000") == "SSE.STK.600000"
    assert bq.normalize_query_code("sh600000") == "SSE.STK.600000"
    assert bq.normalize_query_code("000001") == "SZSE.STK.000001"
    assert bq.normalize_query_code("600000.SH") == "SSE.STK.600000"
    assert bq.normalize_query_code("000001.SZ") == "SZSE.STK.000001"
    assert bq.normalize_query_code("920001.BJ") == "BSE.STK.920001"
    assert bq.normalize_query_code("000001.SH") == "SSE.IDX.000001"
    assert bq.normalize_query_code("399001.SZ") == "SZSE.IDX.399001"
    assert bq.display_code("SSE.STK.600000") == "sh600000"


def test_parse_ymd():
    assert bq._parse_ymd("2024-01-15") == 20240115
    assert bq._parse_ymd(20240115) == 20240115
    assert bq._parse_ymd("2024/01/15") == 20240115


def test_find_day_bar_fallback():
    bars = [
        DayBar(20240102, 10, 11, 9, 10.5, 1, 1),
        DayBar(20240103, 10.5, 11, 10, 10.8, 1, 1),
        DayBar(20240105, 10.8, 11.2, 10.5, 11, 1, 1),
    ]
    bar, exact = bq._find_day_bar(bars, 20240103)
    assert exact and bar.date == 20240103
    bar2, exact2 = bq._find_day_bar(bars, 20240104)  # holiday
    assert not exact2 and bar2.date == 20240103


def test_query_bagua_with_mock_bars(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [
        DayBar(
            date=20240103,
            open=6.27,
            high=7.33,
            low=5.90,
            close=5.90,
            amount=1.0,
            volume=1.0,
        ),
    ]

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
    )
    monkeypatch.setattr(bq, "load_day_bars", lambda _cfg, _code: bars)

    out = bq.query_bagua(cfg, code="600000", date="2024-01-03", period="DAY")
    assert out["ok"] is True
    assert out["period"] == "DAY"
    assert out["code"] == "sh600000"
    assert out["bar"]["open"] == 6.27
    assert out["bagua"]["upper_id"] == 7
    assert out["bagua"]["lower_id"] == 6
    assert out["bagua"]["yao_order"] == 3
    assert out["summary"]["yao_order"] == 3
    assert "山水" in (out["summary"]["full_name"] or out["bagua"].get("full_name") or "")


def test_calculator_same_as_query(monkeypatch):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")
    calc = BaguaCalculator.from_json(JSON_PATH)
    r = calc.calculate(open_price=6.27, high_price=7.33, low_price=5.90, close_price=5.90)
    bars = [
        DayBar(20240103, 6.27, 7.33, 5.90, 5.90, 1, 1),
    ]
    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
    )
    monkeypatch.setattr(bq, "load_day_bars", lambda *_a, **_k: bars)
    out = bq.query_bagua(cfg, code="sz000001", date=20240103, period="DAY")
    assert out["bagua"]["full_name"] == r.full_name
    assert out["bagua"]["yao_name"] == r.yao_name
    assert "name" in out
    assert "display" in out


def test_source_match_pairs():
    assert bq._source_match_pairs("tdx_front") == [("tdxquant", "front")]
    pairs = bq._source_match_pairs("tushare_qfq")
    assert pairs[0] == ("tushare", "qfq")
    assert ("internal", "tushare_factor_qfq") in pairs
    raw_pairs = bq._source_match_pairs("raw")
    assert raw_pairs[0] == ("local_vendor", "none")
    assert ("tushare", "none") in raw_pairs


def test_query_bagua_tdx_and_tushare_from_warehouse(monkeypatch):
    """Both front-adjusted sources resolve via warehouse multi-dataset scan."""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars_tdx = [DayBar(20240103, 10.0, 11.0, 9.5, 10.5, 1, 1)]
    bars_ts = [DayBar(20240103, 10.1, 11.1, 9.6, 10.6, 1, 1)]

    def _fake_load(_cfg, _code, source_key, asof=None):
        if source_key == "tdx_front":
            return bars_tdx, {
                "dataset_id": "tdxquant_front_1d_old",
                "dataset_source": "tdxquant",
                "dataset_adjustment": "front",
                "dataset_status": "ready",
                "covers_asof": True,
                "candidate_datasets": 2,
            }
        return bars_ts, {
            "dataset_id": "tushare_qfq_1d_new",
            "dataset_source": "tushare",
            "dataset_adjustment": "qfq",
            "dataset_status": "ready",
            "covers_asof": True,
            "candidate_datasets": 3,
        }

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        market_data_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_load)

    out_tdx = bq.query_bagua(
        cfg, code="600000", date="2024-01-03", period="DAY", adjust="tdx_front"
    )
    assert out_tdx["ok"] is True
    assert out_tdx["adjust"] == "tdx_front"
    assert out_tdx["bar"]["close"] == 10.5
    assert out_tdx["adjust_meta"]["dataset_id"] == "tdxquant_front_1d_old"
    assert out_tdx["adjust_meta"]["candidate_datasets"] == 2

    out_ts = bq.query_bagua(
        cfg, code="600000", date="2024-01-03", period="DAY", adjust="tushare_qfq"
    )
    assert out_ts["ok"] is True
    assert out_ts["adjust"] == "tushare_qfq"
    assert out_ts["bar"]["close"] == 10.6
    assert out_ts["adjust_meta"]["dataset_source"] == "tushare"
    assert "Tushare" in (out_ts["algorithm"].get("price_format") or "")


def test_query_bagua_raw_from_warehouse(monkeypatch):
    """未复权走仓库 local_vendor/none，不再依赖旧 day 文件优先路径。"""
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars_raw = [DayBar(20240103, 9.0, 9.5, 8.8, 9.2, 1, 1)]

    def _fake_load(_cfg, _code, source_key, asof=None):
        assert source_key == "raw"
        return bars_raw, {
            "dataset_id": "localvendor_none_1d_20260726",
            "dataset_source": "local_vendor",
            "dataset_adjustment": "none",
            "dataset_status": "ready",
            "covers_asof": True,
            "candidate_datasets": 1,
        }

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=Path("."),
        tdx_root=Path("."),
        market_data_root=Path("."),
        forecast_root=Path("."),
        forecast_weekly_dir=Path("."),
        universe_path=Path("."),
        adj_root=Path("."),
    )
    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_load)

    out = bq.query_bagua(cfg, code="600000", date="2024-01-03", period="DAY", adjust="raw")
    assert out["ok"] is True
    assert out["adjust"] == "raw"
    assert out["price_plane"] == "L2_trade_price"
    assert out["bar"]["close"] == 9.2
    assert out["adjust_meta"]["dataset_source"] == "local_vendor"
    assert out["adjust_meta"]["dataset_adjustment"] == "none"
    assert "local_vendor" in (out["algorithm"].get("price_format") or "")


def test_bagua_plane_session_indexes_once(tmp_path):
    """Session lists manifests once; multi-symbol loads share the index."""
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    store = DatasetStore(tmp_path / "market_data")
    codes = ["SSE.STK.600000", "SSE.STK.600004", "SZSE.STK.000001"]
    records = []
    total_rows = 0
    for code in codes:
        bars = [
            MarketBar(
                symbol=code,
                trade_date=20240102 + i,
                period="1d",
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                volume=1000.0,
                amount=10000.0,
            )
            for i in range(3)
        ]
        sha = store.store_bars(code, bars)
        records.append(
            SymbolRecord(
                symbol=code,
                blob_sha256=sha,
                row_count=3,
                quality="ok",
                first_date=20240102,
                last_date=20240104,
            )
        )
        total_rows += 3
    m = DatasetManifest(
        dataset_id="tdxquant_front_1d_session_t1",
        source="tdxquant",
        adjustment="front",
        period="1d",
        status="building",
        data_cutoff_date=20240104,
        symbols=records,
        symbol_count=len(records),
        row_count=total_rows,
    )
    store.publish(m)

    cfg = SimpleNamespace(market_data_root=tmp_path / "market_data")
    session = bq.BaguaPlaneSession(cfg, "tdx_front")
    assert session._saw_any_pair is True
    assert len(session._indexed) >= 1

    bars0, meta0 = session.load_symbol("SSE.STK.600000")
    assert len(bars0) == 3
    assert meta0.get("session_indexed") is True
    assert meta0.get("dataset_id") == "tdxquant_front_1d_session_t1"

    bars1, meta1 = session.load_symbol("SZSE.STK.000001", asof=20240103)
    assert len(bars1) == 3
    assert meta1.get("covers_asof") is True

    # Shared load_day_bars_for_plane path with session + date trim
    bars2, meta2 = bq.load_day_bars_for_plane(
        cfg, "SSE.STK.600004", "tdx_front", start=20240103, end=20240104, session=session
    )
    assert len(bars2) == 2
    assert meta2.get("session_indexed") is True

    with pytest.raises(FileNotFoundError):
        session.load_symbol("SSE.STK.999999")



def test_bagua_plane_session_prefers_freshest_stale_candidate(tmp_path):
    """A stale vendor dataset must not hide a newer Tushare increment."""
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    store = DatasetStore(tmp_path / "market_data")
    code = "SZSE.STK.300475"

    def publish(dataset_id, source, dates, *, reverse_record_dates=False):
        bars = [
            MarketBar(
                symbol=code,
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
        sha = store.store_bars(code, bars)
        first_date, last_date = min(dates), max(dates)
        if reverse_record_dates:
            first_date, last_date = last_date, first_date
        record = SymbolRecord(
            symbol=code,
            blob_sha256=sha,
            row_count=len(bars),
            quality="ok",
            first_date=first_date,
            last_date=last_date,
        )
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source=source,
            adjustment="none",
            period="1d",
            status="building",
            data_cutoff_date=max(dates),
            symbols=[record],
            symbol_count=1,
            row_count=len(bars),
        )
        store.publish(manifest)

    publish(
        "localvendor_none_1d_20260717_test",
        "local_vendor",
        [20260701, 20260717],
    )
    publish(
        "tushare_none_1d_20260730_test",
        "tushare",
        [20260701, 20260730],
        reverse_record_dates=True,
    )

    cfg = SimpleNamespace(market_data_root=tmp_path / "market_data")
    session = bq.BaguaPlaneSession(cfg, "raw")

    fresh_bars, fresh_meta = session.load_symbol(code, asof=20260731)
    assert fresh_meta["dataset_source"] == "tushare"
    assert fresh_meta["symbol_effective_last_date"] == 20260730
    assert fresh_bars[-1].date == 20260730

    historical_bars, historical_meta = session.load_symbol(code, asof=20260715)
    assert historical_meta["dataset_source"] == "local_vendor"
    assert historical_bars[-1].date == 20260717


def test_bagua_plane_session_index_bare_code_collision(tmp_path):
    """sh000001 (上证指数) must never resolve to SZSE.STK.000001 (平安银行).

    Both canonical symbols share the bare variant "000001"; the qualified
    variant (SSE.IDX.000001 / 000001.SH / sh000001) must win even when the
    same-coded stock sits in a dataset that ranks higher.
    """
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    store = DatasetStore(tmp_path / "market_data")

    def publish(dataset_id, symbol, dates):
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
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source="tushare",
            adjustment="none",
            period="1d",
            status="ready",
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
        )
        store.publish(manifest)

    # Stock dataset is ready and holds SZSE.STK.000001 — the collision trap.
    publish("tushare_none_1d_stock_test", "SZSE.STK.000001", [20240102, 20240105])
    publish("tushare_none_1d_index_test", "SSE.IDX.000001", [20240102, 20240105])

    cfg = SimpleNamespace(market_data_root=tmp_path / "market_data")
    session = bq.BaguaPlaneSession(cfg, "raw")

    bars, meta = session.load_symbol("sh000001", asof=20240105)
    assert meta["dataset_id"] == "tushare_none_1d_index_test"
    assert [b.date for b in bars] == [20240102, 20240105]

    # 000001.SH form and canonical form resolve the index too.
    bars2, meta2 = session.load_symbol("000001.SH", asof=20240105)
    assert meta2["dataset_id"] == "tushare_none_1d_index_test"
    bars3, meta3 = session.load_symbol("SSE.IDX.000001", asof=20240105)
    assert meta3["dataset_id"] == "tushare_none_1d_index_test"

    # The same-coded stock (sz000001) still resolves to the stock.
    bars4, meta4 = session.load_symbol("sz000001", asof=20240105)
    assert meta4["dataset_id"] == "tushare_none_1d_stock_test"


def test_bagua_plane_session_index_asof_before_inception_no_stock_leak(tmp_path):
    """Index queried before its inception must fail, not leak to the stock.

    When the qualified variant (SSE.IDX.000001) matches a record that does not
    cover the query date, we must NOT fall back to the bare code — otherwise
    an out-of-range historical index query returns 平安银行's bars.
    """
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.providers.base import MarketBar

    store = DatasetStore(tmp_path / "market_data")

    def publish(dataset_id, symbol, dates):
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
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source="tushare",
            adjustment="none",
            period="1d",
            status="ready",
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
        )
        store.publish(manifest)

    publish("tushare_none_1d_stock_test", "SZSE.STK.000001", [20240102, 20240105])
    # Index exists but starts later than the queried date.
    publish("tushare_none_1d_index_test", "SSE.IDX.000001", [20240201, 20240205])

    cfg = SimpleNamespace(market_data_root=tmp_path / "market_data")
    session = bq.BaguaPlaneSession(cfg, "raw")

    with pytest.raises(FileNotFoundError):
        session.load_symbol("sh000001", asof=20240105)

    # Same-code stock queries in-range still work.
    bars, meta = session.load_symbol("sz000001", asof=20240105)
    assert meta["dataset_id"] == "tushare_none_1d_stock_test"


def test_bagua_plane_signal_family_match_helpers():
    """Document product family gates used by backtest reuse (inline mirror)."""
    def matches(plane, src, adj):
        src = (src or "").strip()
        adj = (adj or "").strip()
        if plane == "tdx_front":
            return src == "tdxquant" and adj in ("", "front")
        if plane == "tushare_qfq":
            if src == "tushare" and adj in ("", "qfq", "tushare_factor_qfq", "composite_tushare_factor_qfq"):
                return True
            if src == "internal" and adj in (
                "tushare_factor_qfq", "composite_tushare_factor_qfq", "qfq", "asof_qfq",
            ):
                return True
        return False

    assert matches("tdx_front", "tdxquant", "front")
    assert matches("tdx_front", "tdxquant", "")
    assert not matches("tdx_front", "tushare", "qfq")
    assert matches("tushare_qfq", "tushare", "qfq")
    assert matches("tushare_qfq", "internal", "tushare_factor_qfq")


def test_batch_query_bagua_multi_codes(monkeypatch, tmp_path):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20240103, 6.27, 7.33, 5.90, 5.90, 1.0, 1.0)]

    def _fake_load(_cfg, _code, source_key, asof=None):
        return bars, {
            "dataset_id": "mock",
            "dataset_source": "tdxquant",
            "dataset_adjustment": "front",
            "dataset_status": "ready",
            "covers_asof": True,
            "candidate_datasets": 1,
        }

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=tmp_path,
        tdx_root=tmp_path,
        market_data_root=tmp_path / "md",
        forecast_root=tmp_path,
        forecast_weekly_dir=tmp_path,
        universe_path=tmp_path / "universe.json",
        adj_root=tmp_path,
    )
    monkeypatch.setattr(bq, "_load_dataset_bars", _fake_load)
    # Force no session (market_data missing) so path uses _load_dataset_bars
    monkeypatch.setattr(
        bq,
        "BaguaPlaneSession",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("no md")),
    )

    out = bq.batch_query_bagua(
        cfg,
        codes=["600000", "000001", "600000"],
        date="2024-01-03",
        period="DAY",
        adjust="tdx_front",
    )
    assert out["ok"] is True
    assert out["requested"] == 2  # de-duped
    assert out["ok_count"] == 2
    assert out["error_count"] == 0
    assert len(out["results"]) == 2
    assert out["results"][0]["bagua"]["upper_id"] == 7


def test_batch_query_bagua_all_stocks_limit(monkeypatch, tmp_path):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20240103, 10.0, 11.0, 9.0, 10.5, 1.0, 1.0)]
    monkeypatch.setattr(
        bq,
        "_load_dataset_bars",
        lambda *_a, **_k: (
            bars,
            {
                "dataset_id": "mock",
                "dataset_source": "tdxquant",
                "dataset_adjustment": "front",
                "dataset_status": "ready",
                "covers_asof": True,
                "candidate_datasets": 1,
            },
        ),
    )
    monkeypatch.setattr(
        bq,
        "BaguaPlaneSession",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("no md")),
    )
    def _fake_resolve(cfg, codes=None, *, all_stocks=False):
        assert all_stocks is True
        return ["SSE.STK.600000", "SZSE.STK.000001", "SSE.STK.600519"]

    monkeypatch.setattr(bq, "_resolve_batch_codes", _fake_resolve)

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=tmp_path,
        tdx_root=tmp_path,
        market_data_root=tmp_path / "md",
        forecast_root=tmp_path,
        forecast_weekly_dir=tmp_path,
        universe_path=tmp_path / "universe.json",
        adj_root=tmp_path,
    )
    out = bq.batch_query_bagua(
        cfg,
        all_stocks=True,
        date=20240103,
        period="DAY",
        adjust="tdx_front",
        limit=2,
    )
    assert out["requested"] == 2
    assert out["ok_count"] == 2
    assert out["all_stocks"] is True


def test_export_bagua_multi_period_xlsx(monkeypatch, tmp_path):
    if not JSON_PATH.exists():
        pytest.skip("bagua_384.json missing")

    bars = [DayBar(20240103, 6.27, 7.33, 5.90, 5.90, 1.0, 1.0)]
    monkeypatch.setattr(
        bq,
        "_load_dataset_bars",
        lambda *_a, **_k: (
            bars,
            {
                "dataset_id": "mock",
                "dataset_source": "tdxquant",
                "dataset_adjustment": "front",
                "dataset_status": "ready",
                "covers_asof": True,
                "candidate_datasets": 1,
            },
        ),
    )
    monkeypatch.setattr(
        bq,
        "BaguaPlaneSession",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("no md")),
    )

    cfg = SimpleNamespace(
        bagua_json=JSON_PATH,
        storage_root=tmp_path,
        tdx_root=tmp_path,
        market_data_root=tmp_path / "md",
        forecast_root=tmp_path,
        forecast_weekly_dir=tmp_path,
        universe_path=tmp_path / "universe.json",
        adj_root=tmp_path,
    )
    monkeypatch.setattr(bq, "load_rizhu_map", lambda _p=None: {"600000": "甲子", "000001": "乙丑"})
    path = bq.export_bagua_multi_period_xlsx(
        cfg,
        date="2024-01-03",
        periods=["WEEK", "MONTH"],
        adjust="tdx_front",
        codes=["600000", "000001"],
        all_stocks=False,
    )
    assert path.exists()
    import openpyxl

    wb = openpyxl.load_workbook(path)
    assert "meta" in wb.sheetnames
    assert "stock-all" in wb.sheetnames
    ws = wb["stock-all"]
    headers = [c.value for c in ws[1]]
    assert headers == list(bq._WEEKLY_EXPORT_HEADERS)
    assert ws.max_row >= 2
    # code / 日柱 columns
    assert ws.cell(2, 1).value in ("600000", "000001")
    assert ws.cell(2, 8).value in ("甲子", "乙丑", None, "")
