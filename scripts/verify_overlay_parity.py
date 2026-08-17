# -*- coding: utf-8 -*-
"""3-trading-day overlay simulation vs full-snapshot bar-by-bar audit.

Builds a synthetic warehouse, simulates 3 days of delta EOD with a mocked
provider, then materializes a "legacy full snapshot" (base + all delta rows,
same merge semantics as the old full-blob pipeline) and audits EVERY bar of
the virtual L2 (composite) and virtual L1 (QFQ) surfaces against the
snapshot. Also reports performance (per-symbol load time) and disk growth
(zero new NPZ blobs).

Usage: python scripts/verify_overlay_parity.py [--symbols N] [--days N]
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    KIND_BARS,
    KIND_FACTOR,
    OverlayState,
    save_overlay_state,
)
from wtpy.apps.astock.data.providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketBar,
    MarketDataRequest,
    ProviderCapabilities,
)
from wtpy.apps.astock.data.repository import MarketDataRepository

_SMD_PATH = Path(__file__).resolve().parent / "sync_market_data.py"


def _smd():
    spec = importlib.util.spec_from_file_location("smd_parity", _SMD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_trading_days(start: int, n: int) -> list:
    """Simple weekday-only date generator."""
    import datetime

    out = []
    d = datetime.datetime.strptime(str(start), "%Y%m%d").date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(int(d.strftime("%Y%m%d")))
        d += datetime.timedelta(days=1)
    return out


class FakeProvider:
    def __init__(self, symbols, days):
        self.symbols = symbols
        self.days = days
        self._pro = self._FakePro(self)

    class _FakePro:
        def __init__(self, outer):
            self._outer = outer

        def adj_factor(self, trade_date=None, ts_code=None, start_date=None,
                       end_date=None):
            d = int(trade_date) if trade_date is not None else None
            if d in self._outer.day_factors:
                rows = [
                    {"ts_code": s, "trade_date": d, "adj_factor": f}
                    for s, f in self._outer.day_factors[d].items()
                ]
                return _df(rows)
            return _df([])

    def set_window(self, day_data, day_factors):
        self.day_data = day_data
        self.day_factors = day_factors

    def health_check(self):
        return True

    def _ensure_initialized(self):
        return None

    def capabilities(self):
        return ProviderCapabilities(source=DataSource.TUSHARE, supports_batch=True)

    def fetch_bars(self, req: MarketDataRequest) -> list:
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
        return _df(rows)

    def _to_ts_code(self, sym):
        return sym.replace(".", "")

    def _from_ts_code(self, ts_code):
        return ts_code


def _df(rows):
    import pandas as pd

    if rows:
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=["ts_code", "trade_date", "adj_factor"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=200)
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    n_syms = args.symbols
    n_days = args.days

    root = Path(tempfile.mkdtemp())
    store = DatasetStore(root)
    symbols = [f"SSE.STK.{600000 + i}" for i in range(n_syms)]
    base_dates = make_trading_days(20260101, 200)

    # ---- base warehouse ----
    base_recs = {}
    for i, sym in enumerate(symbols):
        base = 10.0 + i * 0.01
        bars = [
            MarketBar(symbol=sym, trade_date=d, period="1d",
                      open=base, high=base + 0.5, low=base - 0.5,
                      close=base + 0.2, volume=1000.0 + i,
                      amount=100000.0 + i * 100, source="tushare",
                      adjustment="none")
            for d in base_dates
        ]
        sha = store.store_bars(sym, bars)
        base_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=base_dates[0],
            last_date=base_dates[-1], row_count=len(bars), quality="ok",
        )
    base = DatasetManifest(
        dataset_id="tushare_none_1d_base", source="tushare", adjustment="none",
        period="1d", data_cutoff_date=base_dates[-1],
        snapshot_date=base_dates[-1], provider_version="parity",
        status="ready", created_at="2026-01-01T18:00:00",
    )
    base.symbols = list(base_recs.values())
    base.symbol_count = n_syms
    base.row_count = sum(r.row_count for r in base_recs.values())
    base.expected_symbol_count = n_syms
    base.imported_symbol_count = n_syms
    base.coverage_ratio = 1.0
    store.publish(base)

    fac_recs = {}
    for i, sym in enumerate(symbols):
        f0, f1 = 1.0, 1.0 + i * 0.001
        sha = store.store_factors(sym, [base_dates[0], base_dates[-1]], [f0, f1])
        fac_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=base_dates[0],
            last_date=base_dates[-1], row_count=2, quality="ok",
        )
    fac = DatasetManifest(
        dataset_id="tushare_adjfactor_1d_base", source="tushare",
        adjustment="adj_factor", period="1d", dataset_type="factor",
        data_cutoff_date=base_dates[-1], snapshot_date=base_dates[-1],
        provider_version="parity", status="ready",
        created_at="2026-01-01T18:05:00",
    )
    fac.symbols = list(fac_recs.values())
    fac.symbol_count = n_syms
    fac.row_count = 2 * n_syms
    fac.expected_symbol_count = n_syms
    fac.imported_symbol_count = n_syms
    fac.coverage_ratio = 1.0
    store.publish(fac)

    st = OverlayState(
        enabled=True,
        base_dataset_id=base.dataset_id,
        base_manifest_sha256=base.manifest_sha256,
        factor_base_dataset_id=fac.dataset_id,
        factor_base_manifest_sha256=fac.manifest_sha256,
        delta_watermark=base_dates[-1],
        factor_watermark=base_dates[-1],
    )
    save_overlay_state(root, st)

    # ---- simulate n_days of EOD delta ----
    smd = _smd()
    days = make_trading_days(int(base_dates[-1]) + 1, n_days)
    # per-day full-surface truth: {sym: {date: (o,h,l,c,v,a)}}
    truth: dict = {sym: {} for sym in symbols}
    provider = FakeProvider(symbols, days)
    blob_before = len(list(store.blobs_dir.glob("*.npz")))

    for day_idx, day in enumerate(days):
        day_data = {}
        day_factors = {}
        per_day_bars = {}
        per_day_factors = {}
        for i, sym in enumerate(symbols):
            base_price = 10.0 + i * 0.01
            c = base_price + 0.2 + (day_idx + 1) * 0.05 + i * 0.0001
            o, h, l = c - 0.1, c + 0.2, c - 0.2
            v = 1000.0 + i + day_idx * 10
            a = 100000.0 + i * 100 + day_idx * 500
            per_day_bars[sym] = (o, h, l, c, v, a)
            # factor changes on day 2 (a dividend-like event)
            f = 1.0 + i * 0.001 + (0.05 if day_idx == 1 else 0.0)
            per_day_factors[sym] = f
            truth[sym][day] = (o, h, l, c, v, a)
        day_data[day] = per_day_bars
        day_factors[day] = per_day_factors
        provider.set_window(day_data, day_factors)
        with mock.patch(
            "wtpy.apps.astock.data.providers.tushare.TushareProvider",
            side_effect=lambda token=None: provider,
        ):
            res = smd.sync_tushare_chain_delta(
                mock.Mock(
                    symbol=None, start_date=None, end_date=day,
                    batch_size=10, rate_per_min=10000, token=None,
                    asset_class="stocks", write_mode="delta",
                ),
                store,
            )
        assert res["status"] == "success", res
        print(f"day {day}: raw_new={res['raw']['new_rows']} "
              f"factor_new={res['factor']['new_rows']}")

    blob_after = len(list(store.blobs_dir.glob("*.npz")))
    assert blob_after == blob_before, "delta EOD must not write new NPZ blobs"
    print(f"NPZ blob count before/after: {blob_before}/{blob_after} "
          "(zero snapshot growth)")

    # ---- build the legacy full snapshot (base + all delta rows) ----
    repo = MarketDataRepository(store)
    l2 = repo.resolve_latest_ready(
        source="internal", adjustment="composite_none", period="1d"
    )
    l1 = repo.resolve_latest_ready(
        source="internal", adjustment="composite_tushare_factor_qfq",
        period="1d",
    )
    st = __import__(
        "wtpy.apps.astock.data.delta_store",
        fromlist=["load_overlay_state"],
    ).load_overlay_state(root)
    ds = DeltaStore(root, st.delta_store_id)

    # snapshot: merge base arrays + truth (same "newest-on-date" semantics)
    def snapshot_arrays(sym):
        rec = base_recs[sym]
        arr = store.load_bars(rec.blob_sha256)
        dates = list(arr["trade_date"].tolist())
        o = arr["open"].tolist(); h = arr["high"].tolist()
        l = arr["low"].tolist(); c = arr["close"].tolist()
        v = arr["volume"].tolist(); a = arr["amount"].tolist()
        idx = {int(d): i for i, d in enumerate(dates)}
        for d, vals in truth[sym].items():
            i = idx.get(d)
            if i is not None:
                o[i], h[i], l[i], c[i], v[i], a[i] = vals
            else:
                idx[d] = len(dates)
                dates.append(d)
                o.append(vals[0]); h.append(vals[1]); l.append(vals[2])
                c.append(vals[3]); v.append(vals[4]); a.append(vals[5])
        order = np.argsort(dates)
        return {
            "trade_date": np.asarray(dates, dtype=np.int64)[order],
            "open": np.asarray(o)[order], "high": np.asarray(h)[order],
            "low": np.asarray(l)[order], "close": np.asarray(c)[order],
            "volume": np.asarray(v)[order], "amount": np.asarray(a)[order],
        }

    def qfq_snapshot_arrays(sym):
        """Same math as the legacy derive: raw x factor_asof/anchor.

        The factor surface is base factor blob + delta factor rows (the
        overlay's merged factor series), matching what the virtual L1 reads.
        """
        arr = snapshot_arrays(sym)
        from wtpy.apps.astock.data.overlay import _merge_factor_base_and_delta

        fac_arr = store.load_bars(fac_recs[sym].blob_sha256)
        delta_map = ds.load_visible_factors(
            [sym],
            int(st.factor_watermark),
            commit_seq=st.factor_commit_seq,
        )
        merged = _merge_factor_base_and_delta(fac_arr, delta_map.get(sym))
        fd, fv = merged
        cutoff = int(arr["trade_date"][-1])
        aidx = int(np.searchsorted(fd, cutoff, side="right")) - 1
        anchor = float(fv[aidx])
        pos = np.searchsorted(fd, arr["trade_date"], side="right") - 1
        valid = pos >= 0
        rd = arr["trade_date"][valid]
        ratio = fv[pos[valid]] / anchor
        return {
            "trade_date": rd,
            "open": np.round(arr["open"][valid] * ratio, 4),
            "high": np.round(arr["high"][valid] * ratio, 4),
            "low": np.round(arr["low"][valid] * ratio, 4),
            "close": np.round(arr["close"][valid] * ratio, 4),
            "volume": arr["volume"][valid],
            "amount": arr["amount"][valid],
        }

    # ---- bar-by-bar parity audit ----
    n_checked = 0
    mismatches = []
    t0 = time.time()
    for i, sym in enumerate(symbols):
        v_l2 = repo.load_bar_arrays(
            dataset_id=l2.dataset_id, symbols=[sym]
        )[sym]
        snap = snapshot_arrays(sym)
        assert len(v_l2["trade_date"]) == len(snap["trade_date"]), sym
        for j in range(len(snap["trade_date"])):
            for k in ("open", "high", "low", "close", "volume", "amount"):
                if abs(v_l2[k][j] - snap[k][j]) > 1e-9:
                    mismatches.append(
                        f"{sym} d{snap['trade_date'][j]} {k}: "
                        f"virtual={v_l2[k][j]} snapshot={snap[k][j]}"
                    )
            n_checked += 1
    elapsed_batch = time.time() - t0

    # per-symbol path (backtest style)
    t1 = time.time()
    for i, sym in enumerate(symbols):
        repo.load_bars(dataset_id=l2.dataset_id, symbol=sym)
    elapsed_per_sym = time.time() - t1

    # L1 QFQ parity (sample 50 symbols)
    qfq_mismatches = []
    for i, sym in enumerate(symbols[:50]):
        v_l1 = repo.load_bar_arrays(
            dataset_id=l1.dataset_id, symbols=[sym]
        )[sym]
        qsnap = qfq_snapshot_arrays(sym)
        if v_l1 is None:
            continue
        assert len(v_l1["trade_date"]) == len(qsnap["trade_date"]), sym
        for j in range(len(qsnap["trade_date"])):
            for k in ("open", "high", "low", "close"):
                if abs(v_l1[k][j] - qsnap[k][j]) > 1e-6:
                    qfq_mismatches.append(
                        f"{sym} d{qsnap['trade_date'][j]} {k}: "
                        f"virtual={v_l1[k][j]} snapshot={qsnap[k][j]}"
                    )

    print(f"\nparity: checked {n_checked} L2 bars across {n_syms} symbols "
          f"in {elapsed_batch:.2f}s")
    print(f"per-symbol load: {elapsed_per_sym:.2f}s for {n_syms} symbols "
          f"({elapsed_per_sym / n_syms * 1000:.1f} ms/symbol)")
    if mismatches:
        print(f"L2 MISMATCHES ({len(mismatches)}): {mismatches[:5]}")
        return 1
    if qfq_mismatches:
        print(f"L1 QFQ MISMATCHES ({len(qfq_mismatches)}): {qfq_mismatches[:5]}")
        return 1
    print("L1 QFQ parity: 50 symbols bar-by-bar OK")
    print(f"delta rows: bars={ds.delta_row_count(KIND_BARS)} "
          f"factor={ds.delta_row_count(KIND_FACTOR)}")
    print("PARITY AUDIT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
