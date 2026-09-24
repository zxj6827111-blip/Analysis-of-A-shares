# -*- coding: utf-8 -*-
"""delta 链 --include-bse：新票发现 + 全历史播种（北交所首批适用）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.sync_market_data as smd
from wtpy.apps.astock.data.providers import tushare as ts_mod


class _FakeProvider:
    """返回一只沪深旧票 + 一只北交所新票；raw 窗口/全历史按 start_date 区分。"""

    def __init__(self, token=None):
        self.universe_calls = []

    def health_check(self):
        return True

    def fetch_universe(self, *, include_delisted=False, include_bse=False):
        self.universe_calls.append(include_bse)
        entries = [SimpleNamespace(symbol="SSE.STK.600000")]
        if include_bse:
            entries.append(SimpleNamespace(symbol="BSE.STK.920001"))
        return entries

    def capabilities(self):
        from wtpy.apps.astock.data.providers.base import (
            AdjustmentMode,
            BarPeriod,
            DataSource,
            ProviderCapabilities,
        )

        return ProviderCapabilities(
            source=DataSource.TUSHARE,
            adjustments=[AdjustmentMode.NONE],
            periods=[BarPeriod.DAY],
            supports_batch=False,
            max_batch_size=1,
        )

    class _B:  # MarketBar 替身
        def __init__(self, symbol, d):
            self.symbol = symbol
            self.trade_date = d
            self.open = self.high = self.low = self.close = 10.0
            self.volume = 100.0
            self.amount = 1000.0

    def fetch_bars(self, req):
        sym = req.symbols[0]
        # 新票（全历史请求 start_date=None）→ 返回历史行；老票 → 只回窗口行
        if sym == "BSE.STK.920001":
            base = 20210101 if req.start_date is None else None
            if base is None:
                return []
            return [
                self._B("920001.BJ", base),
                self._B("920001.BJ", req.end_date),
            ]
        if req.start_date is None:  # 老票不该被拉全历史
            return []
        return [self._B("600000.SH", req.end_date)]

    def _from_ts_code(self, ts_code):
        return {"920001.BJ": "BSE.STK.920001", "600000.SH": "SSE.STK.600000"}.get(
            ts_code, ts_code
        )

    def _to_ts_code(self, sym):
        return {"BSE.STK.920001": "920001.BJ", "SSE.STK.600000": "600000.SH"}.get(
            sym, sym
        )


class _FakeLock:
    def __init__(self, *a, **k):
        self.recovered_stale = None

    def acquire(self):
        return self

    def release(self):
        pass


class _FakeWriter:
    instances = []

    def __init__(self, store):
        self.committed = []
        _FakeWriter.instances.append(self)

    def commit_bars(self, **kw):
        self.committed.append(kw)
        return {"new_rows": sum(len(v) for v in kw["rows"].values()),
                "skipped_rows": 0}

    def run_locked(self, fn):
        return fn()


def _mk_view(base_symbols, pool_symbols_list, *, base_cutoff=20260924):
    base = SimpleNamespace(
        symbols=[SimpleNamespace(symbol=s, blob_sha256="x") for s in base_symbols],
        dataset_id="base_ds",
        data_cutoff_date=base_cutoff,
    )
    return SimpleNamespace(
        active_base=lambda: base,
        pool_symbols=lambda: sorted(set(pool_symbols_list)),
        delta_watermark=0,
        factor_watermark=0,
    )


def test_delta_raw_discovers_and_seeds_new_bse_symbols(tmp_path, monkeypatch):
    from wtpy.apps.astock.data import delta_writer as dw_mod
    from wtpy.apps.astock.data import sync_lock as lock_mod

    monkeypatch.setattr(smd, "_overlay_or_error", lambda store: _mk_view(
        ["SSE.STK.600000"], ["SSE.STK.600000"],
    ))
    monkeypatch.setattr(ts_mod, "TushareProvider", _FakeProvider)
    monkeypatch.setattr(lock_mod, "SyncTaskLock", _FakeLock)
    monkeypatch.setattr(dw_mod, "DeltaEodWriter", _FakeWriter)
    monkeypatch.setattr(smd, "_overlay_or_error", lambda store: _mk_view(
        ["SSE.STK.600000"], ["SSE.STK.600000"],
    ))

    args = SimpleNamespace(
        token="x", symbol=None, include_bse=True, batch_size=10,
        start_date=None, end_date=20260924,
    )
    res = smd.sync_tushare_incremental_delta(args, SimpleNamespace(root=tmp_path))
    assert res["status"] == "success"
    committed = _FakeWriter.instances[-1].committed[0]
    rows = committed["rows"]
    # 老票只有窗口行；北交所新票带全历史行（2021 起）
    assert set(rows) == {"SSE.STK.600000", "BSE.STK.920001"}
    assert [r[0] for r in rows["SSE.STK.600000"]] == [20260924]
    assert 20210101 in [r[0] for r in rows["BSE.STK.920001"]]
    # 新票清单回传给 factor 链
    assert res["pool_new_symbols"] == ["BSE.STK.920001"]


def test_delta_raw_without_include_bse_keeps_base_pool(tmp_path, monkeypatch):
    from wtpy.apps.astock.data import delta_writer as dw_mod
    from wtpy.apps.astock.data import sync_lock as lock_mod

    monkeypatch.setattr(smd, "_overlay_or_error", lambda store: _mk_view(
        ["SSE.STK.600000"], ["SSE.STK.600000"],
    ))
    monkeypatch.setattr(ts_mod, "TushareProvider", _FakeProvider)
    monkeypatch.setattr(lock_mod, "SyncTaskLock", _FakeLock)
    monkeypatch.setattr(dw_mod, "DeltaEodWriter", _FakeWriter)
    _FakeWriter.instances.clear()

    args = SimpleNamespace(
        token="x", symbol=None, include_bse=False, batch_size=10,
        start_date=None, end_date=20260924,
    )
    res = smd.sync_tushare_incremental_delta(args, SimpleNamespace(root=tmp_path))
    assert res["status"] == "success"
    committed = _FakeWriter.instances[-1].committed[0]
    # 关闭 include_bse：不重拉名单、只拉 base 票（旧行为）
    assert set(committed["rows"]) == {"SSE.STK.600000"}
    assert res["pool_new_symbols"] == []


def test_fresh_universe_failure_falls_back_to_base(tmp_path, monkeypatch):
    class _Boom(_FakeProvider):
        def fetch_universe(self, **kw):
            raise RuntimeError("rate limited")

    from wtpy.apps.astock.data import delta_writer as dw_mod
    from wtpy.apps.astock.data import sync_lock as lock_mod

    monkeypatch.setattr(smd, "_overlay_or_error", lambda store: _mk_view(
        ["SSE.STK.600000"], ["SSE.STK.600000"],
    ))
    monkeypatch.setattr(ts_mod, "TushareProvider", _Boom)
    monkeypatch.setattr(lock_mod, "SyncTaskLock", _FakeLock)
    monkeypatch.setattr(dw_mod, "DeltaEodWriter", _FakeWriter)
    _FakeWriter.instances.clear()

    args = SimpleNamespace(
        token="x", symbol=None, include_bse=True, batch_size=10,
        start_date=None, end_date=20260924,
    )
    # 名单拉取失败：回退 base 票池继续例行窗口同步（不因元数据停更）
    res = smd.sync_tushare_incremental_delta(args, SimpleNamespace(root=tmp_path))
    assert res["status"] == "success"
    assert set(_FakeWriter.instances[-1].committed[0]["rows"]) == {"SSE.STK.600000"}
    assert res["pool_new_symbols"] == []
