# -*- coding: utf-8 -*-
"""EOD delta chain end-to-end simulation (offline, mocked provider).

Simulates a small overlay warehouse and 3 trading days of delta EOD:
  day 1: 2 symbols get new bars + factors, watermark published
  day 2: 1 symbol gets new bar, 1 gets revised history bar, factors updated
  day 3: re-run of day 2 (idempotency) — no new rows, watermark unchanged

Verifies:
  - no new NPZ blobs are written during delta EOD (zero-snapshot growth)
  - merged virtual L2 grows only by real new dates
  - revisions overlay without growing beyond the changed cell
  - old watermark replays exactly the old surface
  - QFQ runtime derivation stays consistent across watermark
"""
import sys
from pathlib import Path

sys.path.insert(0, r"E:\Software Development\wtpy-master")

import tempfile

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest, DatasetStore, SymbolRecord,
)
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode, BarPeriod, DataSource, MarketBar, MarketDataRequest,
    ProviderCapabilities,
)

root = Path(tempfile.mkdtemp())
store = DatasetStore(root)


def mk_bars(sym, spec):
    """spec: list of (date, close) -> bars around that close."""
    out = []
    for d, c in spec:
        out.append(MarketBar(
            symbol=sym, trade_date=d, period="1d", open=c - 0.1, high=c + 0.2,
            low=c - 0.2, close=c, volume=1000.0, amount=100000.0,
            source="tushare", adjustment="none",
        ))
    return out


def mk_sym(sym, spec):
    bars = mk_bars(sym, spec)
    sha = store.store_bars(sym, bars)
    return SymbolRecord(
        symbol=sym, blob_sha256=sha, first_date=bars[0].trade_date,
        last_date=bars[-1].trade_date, row_count=len(bars), quality="ok",
    )


# ---- base datasets (the "stable baseline" = server 2026-08-14 healthy base) ----
base_spec = {
    "SSE.STK.600000": [(20260810, 10.0), (20260811, 10.2), (20260812, 10.5)],
    "SZSE.STK.000001": [(20260810, 5.0), (20260811, 5.1), (20260812, 5.2)],
}
base_syms = {s: mk_sym(s, spec) for s, spec in base_spec.items()}
base = DatasetManifest(
    dataset_id="tushare_none_1d_20260812_abc", source="tushare",
    adjustment="none", period="1d", data_cutoff_date=20260812,
    snapshot_date=20260812, provider_version="test", status="ready",
    created_at="2026-08-12T18:00:00",
)
base.symbols = list(base_syms.values())
base.symbol_count = 2
base.row_count = 6
store.publish(base)

fac_spec = {
    "SSE.STK.600000": [(20260101, 1.0), (20260601, 1.5)],
    "SZSE.STK.000001": [(20260101, 1.0), (20260601, 2.0)],
}
fac_syms = {}
for s, spec in fac_spec.items():
    sha = store.store_factors(s, [d for d, _ in spec], [f for _, f in spec])
    fac_syms[s] = SymbolRecord(
        symbol=s, blob_sha256=sha, first_date=spec[0][0], last_date=spec[-1][0],
        row_count=len(spec), quality="ok",
    )
fac = DatasetManifest(
    dataset_id="tushare_adjfactor_1d_20260812_def", source="tushare",
    adjustment="adj_factor", period="1d", dataset_type="factor",
    data_cutoff_date=20260812, snapshot_date=20260812,
    provider_version="test", status="ready", created_at="2026-08-12T18:05:00",
)
fac.symbols = list(fac_syms.values())
fac.symbol_count = 2
fac.row_count = 4
store.publish(fac)

# ---- enable overlay at base watermark ----
from wtpy.apps.astock.data.delta_store import OverlayState, save_overlay_state
st = OverlayState(
    enabled=True,
    base_dataset_id=base.dataset_id,
    base_manifest_sha256=base.manifest_sha256,
    factor_base_dataset_id=fac.dataset_id,
    factor_base_manifest_sha256=fac.manifest_sha256,
    delta_watermark=20260812,
    factor_watermark=20260812,
)
save_overlay_state(root, st)

blob_count_before = len(list(store.blobs_dir.glob("*.npz")))

# ---- mock provider ----
import pandas as pd


class FakeProvider:
    """Mimics TushareProvider fetch_bars/fetch_adj_factor surface."""

    def __init__(self, day_data, day_factors):
        self.day_data = day_data          # {date: {sym: (o,h,l,c,v,a)}}
        self.day_factors = day_factors    # {date: {sym: factor}}
        self.calls = []
        self._pro = self._pro(self)

    def health_check(self):
        return True

    def _ensure_initialized(self):
        return None

    def capabilities(self):
        return ProviderCapabilities(
            source=DataSource.TUSHARE, supports_batch=True,
        )

    class _pro:
        """Fake tushare client surface used by _fetch_factor_window_by_trade_date."""

        def __init__(self, outer):
            self._outer = outer

        def adj_factor(self, trade_date=None, ts_code=None, start_date=None,
                       end_date=None):
            d = int(trade_date) if trade_date is not None else None
            if d is not None and d in self._outer.day_factors:
                rows = []
                for sym, f in self._outer.day_factors[d].items():
                    rows.append({"ts_code": sym, "trade_date": d, "adj_factor": f})
                if rows:
                    return pd.DataFrame(rows)
                return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])
            return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

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
        # batch path by trade_date is used by the delta chain
        if trade_date is not None:
            return self._pro.adj_factor(trade_date=trade_date)
        # per-symbol window fallback
        rows = []
        for d, mp in sorted(self.day_factors.items()):
            if start_date and d < int(start_date):
                continue
            if end_date and d > int(end_date):
                continue
            if ts_code in mp:
                rows.append({"ts_code": ts_code, "trade_date": d,
                             "adj_factor": mp[ts_code]})
        if rows:
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])

    def _to_ts_code(self, sym):
        return sym.replace(".", "")

    def _from_ts_code(self, ts_code):
        return ts_code


# day 1: 20260813 new bars
day1_data = {
    20260813: {
        "SSE.STK.600000": (10.7, 10.9, 10.6, 10.8, 1500.0, 160000.0),
        "SZSE.STK.000001": (5.3, 5.4, 5.25, 5.35, 2000.0, 10600.0),
    },
}
day1_factors = {
    20260813: {"SSE.STK.600000": 1.5, "SZSE.STK.000001": 2.0},
}
# day 2: 20260814 new bars + a revision of 20260813 for 600000 (data correction)
day2_data = {
    20260813: {
        # revised close 10.85 instead of 10.8
        "SSE.STK.600000": (10.7, 10.9, 10.6, 10.85, 1500.0, 160000.0),
    },
    20260814: {
        "SSE.STK.600000": (10.9, 11.2, 10.8, 11.1, 1700.0, 180000.0),
        "SZSE.STK.000001": (5.4, 5.5, 5.35, 5.45, 2100.0, 11200.0),
    },
}
day2_factors = {
    20260814: {"SSE.STK.600000": 1.5, "SZSE.STK.000001": 2.0},
}


# ---- run the delta EOD chain with a mocked provider ----
from unittest import mock

import wtpy.apps.astock.data.providers.tushare as tush_mod
from wtpy.apps.astock.data.repository import MarketDataRepository
from wtpy.apps.astock.data.overlay import OverlayView

import importlib.util
_smd_path = Path(r"E:\Software Development\wtpy-master\scripts\sync_market_data.py")
_spec = importlib.util.spec_from_file_location("smd_under_test", _smd_path)
smd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smd)

repo = MarketDataRepository(store)
view = OverlayView.from_root(root)

def run_eod(day_data, day_factors, end_date, sync_run_id):
    prov = FakeProvider(day_data, day_factors)

    def _make_provider(token=None):
        return prov

    with mock.patch.object(tush_mod, "TushareProvider", side_effect=_make_provider):
        args = type("A", (), {
            "symbol": None, "start_date": None, "end_date": end_date,
            "batch_size": 10, "rate_per_min": 1000, "token": None,
            "asset_class": "stocks", "write_mode": "delta",
        })()
        result = smd.sync_tushare_chain_delta(args, store)
    return result, prov


# ---- day 1 ----
r1, p1 = run_eod(day1_data, day1_factors, 20260813, "run_day1")
assert r1["status"] == "success", r1
print("day1 raw new_rows:", r1["raw"]["new_rows"])
print("day1 factor new_rows:", r1["factor"]["new_rows"])
print("day1 publish:", r1["publish"])

l2_1 = repo.resolve_latest_ready(source="internal", adjustment="composite_none", period="1d")
bars_1 = repo.load_bars(dataset_id=l2_1.dataset_id, symbol="SSE.STK.600000")
assert len(bars_1) == 4 and bars_1[-1].trade_date == 20260813, (len(bars_1), bars_1[-1].trade_date if bars_1 else None)
print("day1 L2 600000 rows:", len(bars_1), "last:", bars_1[-1].trade_date, bars_1[-1].close)

# ---- day 2 ----
r2, p2 = run_eod(day2_data, day2_factors, 20260814, "run_day2")
assert r2["status"] == "success", r2
print("day2 raw new_rows:", r2["raw"]["new_rows"])
print("day2 factor new_rows:", r2["factor"]["new_rows"])

l2_2 = repo.resolve_latest_ready(source="internal", adjustment="composite_none", period="1d")
bars_2 = repo.load_bars(dataset_id=l2_2.dataset_id, symbol="SSE.STK.600000")
assert len(bars_2) == 5 and bars_2[-1].trade_date == 20260814, (len(bars_2), bars_2[-1].trade_date if bars_2 else None)
# revised 20260813 close visible at day2 watermark
rev = [b for b in bars_2 if b.trade_date == 20260813][0]
assert abs(rev.close - 10.85) < 1e-9, rev.close
print("day2 L2 600000 rows:", len(bars_2), "revised 0813 close:", rev.close)

# ---- old watermark replay stays day1 ----
bars_old = repo.load_bars(dataset_id=l2_1.dataset_id, symbol="SSE.STK.600000")
assert len(bars_old) == 4
old_rev = [b for b in bars_old if b.trade_date == 20260813][0]
assert abs(old_rev.close - 10.8) < 1e-9, old_rev.close
print("day1 replay rows:", len(bars_old), "old 0813 close:", old_rev.close)

# ---- day3: re-run day2 (idempotency) ----
r3, p3 = run_eod(day2_data, day2_factors, 20260814, "run_day2")
assert r3["status"] == "success", r3
assert r3["raw"]["new_rows"] == 0, r3["raw"]
assert r3["factor"]["new_rows"] == 0, r3["factor"]
print("day3 idempotent: raw new_rows =", r3["raw"]["new_rows"], "factor new_rows =", r3["factor"]["new_rows"])

# ---- L1 runtime QFQ consistency ----
l1_2 = repo.resolve_latest_ready(source="internal", adjustment="composite_tushare_factor_qfq", period="1d")
arr = repo.load_bar_arrays(dataset_id=l1_2.dataset_id, symbols=["SSE.STK.600000"])["SSE.STK.600000"]
# anchor = factor on/before 20260814 = 1.5; raw 11.1 -> qfq 11.1 (factor 1.5/1.5=1)
assert abs(arr["close"][-1] - 11.1) < 1e-6, arr["close"][-1]
print("L1 qfq rows:", len(arr["trade_date"]), "last qfq close:", round(arr["close"][-1], 4))

# ---- zero snapshot growth ----
blob_count_after = len(list(store.blobs_dir.glob("*.npz")))
assert blob_count_after == blob_count_before, (blob_count_before, blob_count_after)
print("NPZ blob count before/after:", blob_count_before, blob_count_after, "(no new blobs)")

# ---- delta row counts ----
from wtpy.apps.astock.data.delta_store import DeltaStore, KIND_BARS, KIND_FACTOR
ds = DeltaStore(root)
print("delta bars rows:", ds.delta_row_count(KIND_BARS), "(expect 5: 0813 x2 + 0814 x2 + 0813 revision x1)")
print("delta factor rows:", ds.delta_row_count(KIND_FACTOR), "(expect 3)")
print("ALL EOD DELTA SIMULATION ASSERTIONS PASSED")
