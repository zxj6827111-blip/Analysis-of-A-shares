# -*- coding: utf-8 -*-
"""EOD delta chain tests (offline, mocked TushareProvider).

Covers the plan's EOD write-layer acceptance:
  - day-1 delta: new bars + factors committed, watermark published
  - day-2 delta: revision of a history bar overlays; old watermark replays
  - idempotent re-run: same window adds zero rows
  - zero new NPZ snapshot blobs across the whole delta EOD
  - overlay publish requires a passing health check
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    DeltaWriteError,
    KIND_BARS,
    KIND_FACTOR,
    load_overlay_state,
)
from wtpy.apps.astock.data.providers.base import (
    DataNotDownloaded,
    DataSource,
    MarketBar,
    MarketDataRequest,
    ProviderCapabilities,
    ProviderError,
)
from wtpy.apps.astock.data.repository import MarketDataRepository

from .conftest import build_overlay_warehouse

_SMD = None


def _sync_module():
    global _SMD
    if _SMD is None:
        path = Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py"
        spec = importlib.util.spec_from_file_location("smd_under_test", path)
        _SMD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SMD)
    return _SMD


class FakeProvider:
    """Mimics the TushareProvider surface the delta chain uses."""

    def __init__(self, day_data, day_factors):
        self.day_data = day_data
        self.day_factors = day_factors
        self.calls = []
        self._pro = self._FakePro(self)

    class _FakePro:
        def __init__(self, outer):
            self._outer = outer

        def adj_factor(self, trade_date=None, ts_code=None, start_date=None,
                       end_date=None):
            d = int(trade_date) if trade_date is not None else None
            if d is not None and d in self._outer.day_factors:
                rows = [
                    {"ts_code": s, "trade_date": d, "adj_factor": f}
                    for s, f in self._outer.day_factors[d].items()
                ]
                return pd.DataFrame(rows) if rows else pd.DataFrame(
                    columns=["ts_code", "trade_date", "adj_factor"]
                )
            return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

    def health_check(self):
        return True

    def _ensure_initialized(self):
        return None

    def capabilities(self):
        return ProviderCapabilities(source=DataSource.TUSHARE, supports_batch=True)

    def fetch_bars(self, req: MarketDataRequest) -> list:
        self.calls.append(("bars", req.start_date, req.end_date))
        out = []
        for sym in req.symbols:
            for d in range(int(req.start_date or 0), int(req.end_date or 0) + 1):
                rec = self.day_data.get(d, {}).get(sym)
                if rec is None:
                    continue
                o, h, l, c, v, a = rec
                out.append(MarketBar(
                    symbol=sym, trade_date=d, period="1d", open=o, high=h,
                    low=l, close=c, volume=v, amount=a,
                    source="tushare", adjustment="none",
                ))
        return out

    def fetch_adj_factor(self, ts_code=None, trade_date=None, start_date=None,
                         end_date=None):
        if trade_date is not None:
            return self._pro.adj_factor(trade_date=trade_date)
        rows = []
        for d, mp in sorted(self.day_factors.items()):
            if start_date and d < int(start_date):
                continue
            if end_date and d > int(end_date):
                continue
            if ts_code in mp:
                rows.append({"ts_code": ts_code, "trade_date": d,
                             "adj_factor": mp[ts_code]})
        return pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["ts_code", "trade_date", "adj_factor"]
        )

    def _to_ts_code(self, sym):
        return sym.replace(".", "")

    def _from_ts_code(self, ts_code):
        return ts_code


@pytest.fixture
def fake_tushare():
    return None


def _run_chain(tmp_path, provider, end_date):
    smd = _sync_module()
    store = DatasetStore(tmp_path)
    # the sync script imports TushareProvider lazily inside the chain
    # functions, so patch the real class in the provider module
    import wtpy.apps.astock.data.providers.tushare as tush_mod

    with mock.patch.object(
        tush_mod, "TushareProvider",
        side_effect=lambda token=None: provider,
    ):
        args = mock.Mock(
            symbol=None, start_date=None, end_date=end_date, batch_size=10,
            rate_per_min=1000, token=None, asset_class="stocks",
            write_mode="delta",
        )
        return smd.sync_tushare_chain_delta(args, store), store


class TestEodDeltaChain:
    def test_full_chain_day1_day2_idempotency(self, tmp_path):
        build_overlay_warehouse(tmp_path)
        smd = _sync_module()
        day1_data = {
            20240109: {
                "SSE.STK.600000": (10.7, 10.9, 10.6, 10.8, 1500.0, 160000.0),
                "SZSE.STK.000001": (5.3, 5.4, 5.25, 5.35, 2000.0, 10600.0),
            },
        }
        day1_factors = {20240109: {"SSE.STK.600000": 1.5, "SZSE.STK.000001": 2.0}}
        day2_data = {
            20240109: {
                # revision of the 600000 bar from day1
                "SSE.STK.600000": (10.7, 10.9, 10.6, 10.85, 1500.0, 160000.0),
            },
            20240110: {
                "SSE.STK.600000": (10.9, 11.2, 10.8, 11.1, 1700.0, 180000.0),
                "SZSE.STK.000001": (5.4, 5.5, 5.35, 5.45, 2100.0, 11200.0),
            },
        }
        day2_factors = {20240110: {"SSE.STK.600000": 1.5, "SZSE.STK.000001": 2.0}}

        store = DatasetStore(tmp_path)
        blobs_before = len(list(store.blobs_dir.glob("*.npz")))

        # day 1
        r1, _ = _run_chain(tmp_path, FakeProvider(day1_data, day1_factors), 20240109)
        assert r1["status"] == "success", r1
        assert r1["raw"]["new_rows"] == 2
        assert r1["factor"]["new_rows"] == 2
        assert r1["publish"]["delta_watermark"] == 20240109

        repo = MarketDataRepository(store)
        l2_1 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        bars = repo.load_bars(dataset_id=l2_1.dataset_id, symbol="SSE.STK.600000")
        assert len(bars) == 7 and bars[-1].trade_date == 20240109
        assert abs(bars[-1].close - 10.8) < 1e-9

        # day 2 (revision + new day)
        r2, _ = _run_chain(tmp_path, FakeProvider(day2_data, day2_factors), 20240110)
        assert r2["status"] == "success", r2
        assert r2["raw"]["new_rows"] == 3  # revision + 2 new bars

        l2_2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        bars2 = repo.load_bars(dataset_id=l2_2.dataset_id, symbol="SSE.STK.600000")
        assert len(bars2) == 8 and bars2[-1].trade_date == 20240110
        rev = [b for b in bars2 if b.trade_date == 20240109][0]
        assert abs(rev.close - 10.85) < 1e-9

        # old watermark replay keeps day1 values
        bars_old = repo.load_bars(dataset_id=l2_1.dataset_id, symbol="SSE.STK.600000")
        assert len(bars_old) == 7
        rev_old = [b for b in bars_old if b.trade_date == 20240109][0]
        assert abs(rev_old.close - 10.8) < 1e-9

        # day 3: re-run of day2 -> zero new rows (idempotent)
        r3, _ = _run_chain(tmp_path, FakeProvider(day2_data, day2_factors), 20240110)
        assert r3["status"] == "success"
        assert r3["raw"]["new_rows"] == 0
        assert r3["factor"]["new_rows"] == 0

        # zero snapshot growth
        blobs_after = len(list(store.blobs_dir.glob("*.npz")))
        assert blobs_after == blobs_before

    def test_requested_holiday_publishes_observed_trading_day(
        self, tmp_path
    ):
        build_overlay_warehouse(tmp_path)
        data = {
            20240110: {
                "SSE.STK.600000": (
                    10.9, 11.2, 10.8, 11.1, 1700.0, 180000.0
                )
            }
        }
        factors = {20240110: {"SSE.STK.600000": 1.5}}
        result, _ = _run_chain(
            tmp_path, FakeProvider(data, factors), 20240113
        )
        assert result["status"] == "success", result
        assert result["raw"]["requested_cutoff"] == 20240113
        assert result["raw"]["cutoff"] == 20240110
        assert result["factor"]["cutoff"] == 20240110
        state = load_overlay_state(tmp_path)
        assert state.delta_watermark == 20240110
        assert state.factor_watermark == 20240110

    def test_publish_requires_healthy_delta(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        ds = DeltaStore(tmp_path)
        from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

        writer = DeltaEodWriter(store, delta=ds)
        # health check against a watermark with no committed batch fails
        with pytest.raises(DeltaWriteError):
            writer.publish(delta_watermark=20250101, require_health=True)

    def test_publish_rejects_watermark_regression(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

        writer = DeltaEodWriter(store)
        with pytest.raises(DeltaWriteError, match="watermark regression"):
            writer.publish(delta_watermark=20240107, require_health=False)

    def test_health_gate_blocks_publish_keeps_batch_invisible(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        ds = DeltaStore(tmp_path)
        from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

        writer = DeltaEodWriter(store, delta=ds)
        # commit a batch at wm 20240109, then attempt to publish to a FUTURE
        # watermark whose batch was never committed -> health fails -> the
        # registry stays at the old watermark (batch invisible)
        base = store.load_manifest(load_overlay_state(tmp_path).base_dataset_id)
        writer.commit_bars(
            sync_run_id="t", source="tushare", base_dataset_id=base.dataset_id,
            cutoff=20240109,
            rows={"SSE.STK.600000": [(20240109, 10.8, 11.0, 10.7, 10.9,
                                      1000.0, 100000.0)]},
        )
        with pytest.raises(DeltaWriteError):
            writer.publish(delta_watermark=20240110, require_health=True)
        st = load_overlay_state(tmp_path)
        assert st.delta_watermark == 20240108  # unchanged -> batch invisible


class TestEodDeltaEdgeCases:
    def test_complete_chain_lock_rejects_concurrent_run(self, tmp_path):
        build_overlay_warehouse(tmp_path)
        from wtpy.apps.astock.data.sync_lock import SyncTaskLock

        lock = SyncTaskLock(
            tmp_path,
            source="tushare",
            adjustment="overlay_chain",
            period="1d",
            sync_run_id="held",
        )
        lock.acquire()
        try:
            result, _ = _run_chain(
                tmp_path, FakeProvider({}, {}), 20240109
            )
        finally:
            lock.release()
        assert result["status"] == "failed"
        assert result["error"] == "concurrent_overlay_chain"

    def test_non_batch_provider_treats_suspended_symbol_as_empty(self):
        smd = _sync_module()

        class NonBatchProvider:
            def __init__(self):
                self.calls = []

            def capabilities(self):
                return ProviderCapabilities(
                    source=DataSource.TUSHARE,
                    supports_batch=False,
                    max_batch_size=1,
                )

            def fetch_bars(self, request):
                assert len(request.symbols) == 1
                symbol = request.symbols[0]
                self.calls.append(symbol)
                if symbol == "SZSE.STK.000001":
                    raise DataNotDownloaded("suspended")
                return [MarketBar(
                    symbol=symbol,
                    trade_date=20240109,
                    period="1d",
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=1000.0,
                    amount=100000.0,
                    source="tushare",
                    adjustment="none",
                )]

        provider = NonBatchProvider()
        rows, failed = smd._fetch_raw_window_rows(
            provider,
            ["SSE.STK.600000", "SZSE.STK.000001"],
            start_date=20240101,
            end_date=20240109,
            batch_size=10,
        )
        assert provider.calls == ["SSE.STK.600000", "SZSE.STK.000001"]
        assert list(rows) == ["SSE.STK.600000"]
        assert failed == {}

    def test_provider_error_blocks_raw_commit_and_watermark(self, tmp_path):
        build_overlay_warehouse(tmp_path)

        class PartialFailureProvider(FakeProvider):
            def fetch_bars(self, request):
                if len(request.symbols) > 1:
                    raise ProviderError("batch request failed")
                if request.symbols[0] == "SZSE.STK.000001":
                    raise ProviderError("symbol request failed")
                return super().fetch_bars(request)

        data = {
            20240109: {
                "SSE.STK.600000": (
                    10.7, 10.9, 10.6, 10.8, 1500.0, 160000.0
                )
            }
        }
        provider = PartialFailureProvider(
            data,
            {20240109: {"SSE.STK.600000": 1.5}},
        )
        result, _ = _run_chain(tmp_path, provider, 20240109)

        assert result["status"] == "failed"
        assert result["error"] == "raw_provider_failed"
        assert result["failed_symbol_count"] == 1
        assert "SZSE.STK.000001" in result["failed_symbols"]
        assert DeltaStore(tmp_path).delta_row_count(KIND_BARS) == 0
        state = load_overlay_state(tmp_path)
        assert state.delta_watermark == 20240108
        assert state.factor_watermark == 20240108

    def test_empty_factor_window_does_not_advance_watermarks(self, tmp_path):
        build_overlay_warehouse(tmp_path)
        day_data = {
            20240109: {
                "SSE.STK.600000": (
                    10.7, 10.9, 10.6, 10.8, 1500.0, 160000.0
                )
            }
        }
        result, _ = _run_chain(
            tmp_path, FakeProvider(day_data, {}), 20240109
        )
        assert result["status"] == "failed"
        assert result["error"] == "factor_window_empty"
        state = load_overlay_state(tmp_path)
        assert state.delta_watermark == 20240108
        assert state.factor_watermark == 20240108
