# -*- coding: utf-8 -*-
"""sync_local_vendor_full checkpoint lifecycle (regression).

The checkpoint must be consumed (deleted) once the dataset reaches a
terminal publish; a leftover checkpoint would fail the next plain
--source local_vendor --mode full run with
checkpoint_exists_use_resume_or_fresh.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from wtpy.apps.astock.data.dataset_store import DatasetStore
from wtpy.apps.astock.data.providers.base import MarketBar

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

_MODULE = None


def _script():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "sync_local_vendor_full_test", SYNC_SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def _args(incoming: Path) -> SimpleNamespace:
    return SimpleNamespace(
        incoming_root=str(incoming),
        start_date=None, end_date=None, anchor_date=None,
        symbol="SSE.STK.600000,SSE.STK.600004",
        universe_file=None, chunk_size=500,
        resume=False, fresh=False,
        allow_no_data_file=None, adjustment="none", period="1d",
        log_path=None, report_path=None,
    )


def test_local_vendor_full_removes_checkpoint_after_success(tmp_path, monkeypatch):
    """A successful full sync must delete its checkpoint (no NameError)."""
    smd = _script()
    store = DatasetStore(tmp_path / "market_data")
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    bars = [
        MarketBar(
            symbol=sym, trade_date=date, period="1d",
            open=10.0, high=11.0, low=9.0, close=10.5,
            volume=1000.0, amount=10000.0,
        )
        for sym in ("SSE.STK.600000", "SSE.STK.600004")
        for date in range(20240102, 20240102 + 30)
    ]

    class FakeProvider:
        def health_check(self):
            return True

        def available_years(self):
            return [2024]

        def provider_version(self):
            return "fake_local_vendor_v1"

        def fetch_bars_zipfirst(self, symbols, **kwargs):
            return {s: [b for b in bars if b.symbol == s] for s in symbols}

    from wtpy.apps.astock.data.providers import local_vendor as lv_mod

    monkeypatch.setattr(lv_mod, "LocalVendorProvider", lambda incoming: FakeProvider())

    result = smd.sync_local_vendor_full(_args(incoming), store)
    assert result["status"] == "success"
    assert result["dataset_status"] == "ready"
    ck = store.sync_logs_dir / "checkpoint_local_vendor_none_1d.json"
    assert not ck.exists(), "checkpoint must be consumed after a terminal publish"


def test_local_vendor_full_checkpoint_fresh_restarts(tmp_path, monkeypatch):
    """A leftover checkpoint without --resume/--fresh fails the run; --fresh
    discards it (checkpoint plumbing still works after the fix)."""
    smd = _script()
    store = DatasetStore(tmp_path / "market_data")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    ck_path = store.sync_logs_dir / "checkpoint_local_vendor_none_1d.json"
    ck_path.write_text('{"created_at": "2026-01-01T00:00:00", "completed_chunks": {}}',
                       encoding="utf-8")

    class FakeProvider:
        def health_check(self):
            return True

        def available_years(self):
            return [2024]

        def provider_version(self):
            return "fake_local_vendor_v1"

        def fetch_bars_zipfirst(self, symbols, **kwargs):
            return {}

    from wtpy.apps.astock.data.providers import local_vendor as lv_mod

    monkeypatch.setattr(lv_mod, "LocalVendorProvider", lambda incoming: FakeProvider())

    args = _args(incoming)
    args.fresh = True
    result = smd.sync_local_vendor_full(args, store)
    assert result["status"] == "success"
    assert not ck_path.exists()
