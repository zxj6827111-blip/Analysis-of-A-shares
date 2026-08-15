# -*- coding: utf-8 -*-
"""Overlay virtual views — stable base blobs + DuckDB versioned delta.

In ``overlay_v1`` storage mode the formal product surfaces are no longer
materialized as full-history NPZ blobs every EOD:

  L2 (execution) : virtual composite view — the active base dataset and the
                   delisted base dataset form the stock pool; DuckDB delta
                   rows overlay the same-day data of active symbols.
  L1 (signal)    : virtual QFQ view — merged raw bars x factor series are
                   combined at runtime: qfq = round(raw * factor_asof/anchor,
                   4) with the same missing-factor / leading-gap rules as the
                   legacy derivation.

Reads are versioned: a view resolves to a fixed watermark, so replaying a
backtest after later revisions reproduces the exact bars that were visible
at that watermark (``delta_watermark`` / ``factor_watermark`` on the overlay
registry or the virtual manifest provenance).

Design notes:
  - A virtual manifest carries ``storage_mode="overlay_v1"`` and ``view_type``
    but NO per-symbol blobs; repository load paths detect it and route to the
    merge/derive helpers here.
  - Legacy explicit dataset ids keep reading their original blobs; the virtual
    ids below are only produced by ``latest`` resolution when overlay is on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from .delta_store import (
    DeltaStore,
    KIND_BARS,
    KIND_FACTOR,
    OverlayState,
    load_overlay_state,
)

#: view_type values written on virtual manifests
VIEW_L2_COMPOSITE = "l2_virtual_composite"
VIEW_L1_QFQ = "l1_virtual_qfq"
VIEW_RAW = "raw_virtual"

#: virtual dataset id prefixes (stable across watermarks — watermark lives in
#: the manifest provenance so re-resolution returns the current surface)
VIRTUAL_L2_PREFIX = "overlay_v1_l2_composite"
VIRTUAL_L1_PREFIX = "overlay_v1_l1_qfq"
VIRTUAL_RAW_PREFIX = "overlay_v1_raw"

#: rounding policy identical to the legacy QFQ derivation
QFQ_ROUND_DECIMALS = 4


class OverlayNotReadyError(RuntimeError):
    """The overlay registry is not enabled or its base is missing."""


@dataclass
class OverlayView:
    """Virtual view over one delta store + base datasets.

    The overlay registry is re-read on every access (stat-signature cached
    by :func:`load_overlay_state`), so an EOD publish that advances the
    watermark is visible to a long-running server process on the next
    resolve without a restart.
    """

    store: DatasetStore
    delta: Optional[DeltaStore] = None
    #: merged factor series cache keyed by (symbol, watermark)
    _factor_series_cache: Dict[tuple, Optional[Tuple[np.ndarray, np.ndarray]]] = field(default_factory=dict)
    #: decompressed factor-blob arrays keyed by blob sha256
    _factor_blob_cache: Dict[str, Dict[str, np.ndarray]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.delta is None:
            self.delta = DeltaStore(self.store.root)

    @property
    def overlay(self) -> OverlayState:
        return load_overlay_state(self.store.root)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def from_root(
        cls,
        root,
        *,
        required: bool = True,
    ) -> Optional["OverlayView"]:
        store = DatasetStore(root)
        overlay = load_overlay_state(root)
        if not overlay.enabled:
            if required:
                raise OverlayNotReadyError(
                    "overlay_v1 storage is not enabled on this data root "
                    "(overlay_state.json missing or enabled=false)"
                )
            return None
        view = cls(store=store)
        view.delta = DeltaStore(store.root)
        return view

    @property
    def delta_watermark(self) -> int:
        return int(self.overlay.delta_watermark or 0)

    @property
    def factor_watermark(self) -> int:
        return int(self.overlay.factor_watermark or 0)

    # ------------------------------------------------------------------
    # base resolution
    # ------------------------------------------------------------------
    def _base_manifest(self, dataset_id: str) -> DatasetManifest:
        if not dataset_id:
            raise OverlayNotReadyError("overlay base_dataset_id is empty")
        m = self.store.load_manifest(dataset_id)
        if m is None:
            raise OverlayNotReadyError(
                f"overlay base dataset missing: {dataset_id}"
            )
        return m

    def active_base(self) -> DatasetManifest:
        return self._base_manifest(self.overlay.base_dataset_id)

    def delisted_base(self) -> Optional[DatasetManifest]:
        if not self.overlay.delisted_base_dataset_id:
            return None
        return self._base_manifest(self.overlay.delisted_base_dataset_id)

    def factor_base(self) -> DatasetManifest:
        return self._base_manifest(self.overlay.factor_base_dataset_id)

    def supplement_factor_base(self) -> Optional[DatasetManifest]:
        if not self.overlay.supplement_factor_base_dataset_id:
            return None
        return self._base_manifest(self.overlay.supplement_factor_base_dataset_id)

    # ------------------------------------------------------------------
    # stock pool (active base + delisted base, disjoint by construction —
    # the migration registers a delisted base that is missing from active)
    # ------------------------------------------------------------------
    def pool_symbols(self) -> List[str]:
        base = self.active_base()
        syms = {r.symbol for r in base.symbols if r.blob_sha256}
        delisted = self.delisted_base()
        if delisted is not None:
            for r in delisted.symbols:
                if r.blob_sha256:
                    syms.add(r.symbol)
        return sorted(syms)

    def _pool_records(self) -> List[SymbolRecord]:
        """Merge base + delisted symbol records (base wins on collision)."""
        base = self.active_base()
        records: Dict[str, SymbolRecord] = {}
        for r in base.symbols:
            if r.blob_sha256:
                records[r.symbol] = r
        delisted = self.delisted_base()
        if delisted is not None:
            for r in delisted.symbols:
                if r.blob_sha256 and r.symbol not in records:
                    records[r.symbol] = r
        return [records[s] for s in sorted(records)]

    # ------------------------------------------------------------------
    # virtual manifests
    # ------------------------------------------------------------------
    def l2_virtual_manifest(self) -> DatasetManifest:
        """Virtual L2 (composite) manifest over the active + delisted pool.

        The dataset id embeds the delta watermark so a view is immutable: a
        later delta update produces a NEW virtual manifest id (readers of an
        older id keep replaying the exact watermark they resolved).
        """
        base = self.active_base()
        records = self._pool_records()
        dataset_id = (
            f"{VIRTUAL_L2_PREFIX}_{base.dataset_id}_wm{self.delta_watermark:08d}"
        )
        existing = self.store.load_manifest(dataset_id)
        if existing is not None:
            return existing
        cutoff = int(base.data_cutoff_date or 0)
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source="internal",
            adjustment="composite_none",
            period="1d",
            dataset_type="bars",
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=max(
                cutoff,
                max((int(r.last_date or 0) for r in records), default=0),
            ),
            provider_version="overlay_v1",
            sync_run_id=f"overlay_l2_{time.strftime('%Y%m%dT%H%M%S')}",
            status="ready",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            storage_mode="overlay_v1",
            base_dataset_id=base.dataset_id,
            base_manifest_sha256=base.manifest_sha256,
            delta_store_id=self.overlay.delta_store_id,
            delta_watermark=self.delta_watermark,
            factor_watermark=self.factor_watermark,
            view_type=VIEW_L2_COMPOSITE,
            universe_type=base.universe_type or "overlay_composite",
            survivorship_bias=False,
            historical_universe_complete=True,
            delisted_coverage_complete=True,
            warning_text=(
                "Virtual composite L2 (overlay_v1): base blobs + DuckDB "
                "delta merged at read time. No materialized snapshot."
            ),
            recommended_use=["survivorship-safe backtest execution (L2)"],
            prohibited_or_discouraged_use=[
                "signal generation without adjustment handling",
            ],
            provenance={
                "storage_mode": "overlay_v1",
                "view_type": VIEW_L2_COMPOSITE,
                "delta_store_id": self.overlay.delta_store_id,
                "delta_watermark": self.delta_watermark,
                "base_dataset_id": base.dataset_id,
                "delisted_base_dataset_id": self.overlay.delisted_base_dataset_id,
            },
        )
        manifest.symbols = records
        manifest.symbol_count = len(records)
        manifest.row_count = sum(int(r.row_count or 0) for r in records)
        manifest.expected_symbol_count = len(records)
        manifest.imported_symbol_count = len(records)
        manifest.coverage_ratio = 1.0
        self.store.save_manifest(manifest)
        return manifest

    def l1_virtual_manifest(self) -> DatasetManifest:
        """Virtual L1 (QFQ) manifest referencing the virtual L2 raw parent.

        The anchor (last factor on or before the raw cutoff) is fixed at
        manifest build time from the factor base + delta surface, so a given
        watermark always derives identical QFQ bars.
        """
        raw = self.l2_virtual_manifest()
        fac = self.factor_base()
        dataset_id = (
            f"{VIRTUAL_L1_PREFIX}_{raw.dataset_id}_wm{self.factor_watermark:08d}"
        )
        existing = self.store.load_manifest(dataset_id)
        if existing is not None:
            return existing
        cutoff = max(
            int(raw.data_cutoff_date or 0),
            max((int(r.last_date or 0) for r in fac.symbols if r.last_date),
                default=0),
        )
        records = self._pool_records()
        manifest = DatasetManifest(
            dataset_id=dataset_id,
            source="internal",
            adjustment="composite_tushare_factor_qfq",
            period="1d",
            dataset_type="bars",
            snapshot_date=int(time.strftime("%Y%m%d")),
            data_cutoff_date=cutoff,
            provider_version="overlay_v1_runtime_qfq",
            sync_run_id=f"overlay_l1_{time.strftime('%Y%m%dT%H%M%S')}",
            raw_dataset_id=raw.dataset_id,
            factor_dataset_id=fac.dataset_id,
            status="ready",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            storage_mode="overlay_v1",
            base_dataset_id=self.overlay.base_dataset_id,
            base_manifest_sha256=self.overlay.base_manifest_sha256,
            delta_store_id=self.overlay.delta_store_id,
            delta_watermark=self.delta_watermark,
            factor_watermark=self.factor_watermark,
            view_type=VIEW_L1_QFQ,
            anchor_policy="last_factor_on_or_before_cutoff",
            formula_version="overlay_runtime_qfq_v1",
            price_precision_policy="round_half_even_4dp; compare at 2dp",
            volume_policy="copied_from_raw_shares_no_adjustment",
            amount_policy="copied_from_raw_cny_no_adjustment",
            universe_type=self.active_base().universe_type or "overlay_composite",
            survivorship_bias=False,
            historical_universe_complete=True,
            delisted_coverage_complete=True,
            warning_text=(
                "Virtual QFQ L1 (overlay_v1): raw x factor derived at read "
                "time. Signal use only."
            ),
            recommended_use=["L1 signal computation (front-adjusted, survivorship-safe)"],
            prohibited_or_discouraged_use=[
                "L2 execution prices",
                "claiming Tushare-native qfq data",
            ],
            provenance={
                "storage_mode": "overlay_v1",
                "view_type": VIEW_L1_QFQ,
                "delta_store_id": self.overlay.delta_store_id,
                "delta_watermark": self.delta_watermark,
                "factor_watermark": self.factor_watermark,
                "raw_virtual_dataset_id": raw.dataset_id,
                "factor_base_dataset_id": fac.dataset_id,
                "supplement_factor_base_dataset_id": (
                    self.overlay.supplement_factor_base_dataset_id or ""
                ),
            },
        )
        manifest.symbols = records
        manifest.symbol_count = len(records)
        manifest.row_count = sum(int(r.row_count or 0) for r in records)
        manifest.expected_symbol_count = len(records)
        manifest.imported_symbol_count = len(records)
        manifest.coverage_ratio = 1.0
        self.store.save_manifest(manifest)
        return manifest

    # ------------------------------------------------------------------
    # merged raw reads (base blob + delta overlay, newest-on-date)
    # ------------------------------------------------------------------
    def merged_raw_arrays(
        self,
        symbol: str,
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        watermark: Optional[int] = None,
    ) -> Optional[Dict[str, np.ndarray]]:
        """Merged raw OHLCV arrays for one symbol (base + delta overlay).

        Returns None when the symbol is not in the pool. Rows outside
        [start_date, end_date] are dropped before returning.
        """
        wm = int(watermark if watermark is not None else self.delta_watermark)
        rec = self._pool_record(symbol)
        if rec is None:
            return None
        base_arr = None
        if rec.blob_sha256 and self.store.blob_exists(rec.blob_sha256):
            base_arr = self.store.load_bars(rec.blob_sha256)
        delta_map = self.delta.load_visible_bars([rec.symbol], wm)
        merged = _merge_base_and_delta(base_arr, delta_map.get(rec.symbol))
        if merged is None:
            return None
        return _slice_arrays(merged, start_date, end_date)

    def merged_raw_arrays_batch(
        self,
        symbols: Sequence[str],
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        watermark: Optional[int] = None,
    ) -> Dict[str, Optional[Dict[str, np.ndarray]]]:
        """Batch merged raw arrays — one delta query for all symbols.

        Returns {symbol: merged arrays or None}. Symbols not in the pool are
        absent from the result.
        """
        wm = int(watermark if watermark is not None else self.delta_watermark)
        pool = {r.symbol: r for r in self._pool_records()}
        present = [s for s in symbols if s in pool]
        out: Dict[str, Optional[Dict[str, np.ndarray]]] = {}
        if not present:
            return out
        delta_map = self.delta.load_visible_bars(present, wm)
        for sym in present:
            rec = pool[sym]
            base_arr = None
            if rec.blob_sha256 and self.store.blob_exists(rec.blob_sha256):
                base_arr = self.store.load_bars(rec.blob_sha256)
            merged = _merge_base_and_delta(base_arr, delta_map.get(sym))
            if merged is None:
                out[sym] = None
                continue
            out[sym] = _slice_arrays(merged, start_date, end_date)
        return out

    # ------------------------------------------------------------------
    # merged factor reads
    # ------------------------------------------------------------------
    def _factor_series(
        self, symbol: str, *, watermark: Optional[int] = None
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """(factor_dates, factor_values) for one symbol (base + delta merge).

        Resolution mirrors the legacy rule: main factor base > supplement
        factor base > BSE alias of main factor base. Series are cached
        per (symbol, watermark) so whole-market loops do not re-merge.
        """
        wm = int(watermark if watermark is not None else self.factor_watermark)
        key = (symbol, wm)
        cached = self._factor_series_cache.get(key)
        if cached is not None:
            return cached
        series = self._factor_series_uncached(symbol, watermark=wm)
        if len(self._factor_series_cache) >= 4096:
            self._factor_series_cache.clear()
        if series is not None:
            self._factor_series_cache[key] = series
        return series

    def _factor_series_uncached(
        self, symbol: str, *, watermark: int
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        main = self.factor_base()
        delta_map = self.delta.load_visible_factors([symbol], watermark)
        arr = self._factor_base_arrays(main, symbol)
        merged = _merge_factor_base_and_delta(arr, delta_map.get(symbol))
        if merged is not None:
            return merged
        supp = self.supplement_factor_base()
        if supp is not None and supp.dataset_id != main.dataset_id:
            arr = self._factor_base_arrays(supp, symbol)
            merged = _merge_factor_base_and_delta(arr, delta_map.get(symbol))
            if merged is not None:
                return merged
        alias = self._alias_canonical(symbol)
        if alias and alias != symbol:
            arr = self._factor_base_arrays(main, alias)
            merged = _merge_factor_base_and_delta(arr, delta_map.get(alias))
            if merged is not None:
                return merged
        return None

    def _factor_base_arrays(
        self, fac_manifest: DatasetManifest, symbol: str
    ) -> Optional[Dict[str, np.ndarray]]:
        rec = self._find_record(fac_manifest, symbol)
        if rec is None or not rec.blob_sha256:
            return None
        if not self.store.blob_exists(rec.blob_sha256):
            return None
        cached = self._factor_blob_cache.get(rec.blob_sha256)
        if cached is not None:
            return cached
        arr = self.store.load_bars(rec.blob_sha256)
        if len(self._factor_blob_cache) >= 8192:
            self._factor_blob_cache.clear()
        self._factor_blob_cache[rec.blob_sha256] = arr
        return arr

    def _factor_series_batch(
        self,
        symbols: Sequence[str],
        *,
        watermark: Optional[int] = None,
    ) -> Dict[str, Optional[Tuple[np.ndarray, np.ndarray]]]:
        """Merge factor series for many symbols with one delta query.

        Preloads the factor base blobs once (they are reused across symbols
        through the blob cache), so whole-market QFQ preparation does not
        decompress per symbol.
        """
        wm = int(watermark if watermark is not None else self.factor_watermark)
        main = self.factor_base()
        delta_map = self.delta.load_visible_factors(list(symbols), wm)
        out: Dict[str, Optional[Tuple[np.ndarray, np.ndarray]]] = {}
        for sym in symbols:
            key = (sym, wm)
            cached = self._factor_series_cache.get(key)
            if cached is not None:
                out[sym] = cached
                continue
            arr = self._factor_base_arrays(main, sym)
            merged = _merge_factor_base_and_delta(arr, delta_map.get(sym))
            if merged is None:
                supp = self.supplement_factor_base()
                if supp is not None and supp.dataset_id != main.dataset_id:
                    arr = self._factor_base_arrays(supp, sym)
                    merged = _merge_factor_base_and_delta(
                        arr, delta_map.get(sym)
                    )
                if merged is None:
                    alias = self._alias_canonical(sym)
                    if alias and alias != sym:
                        arr = self._factor_base_arrays(main, alias)
                        merged = _merge_factor_base_and_delta(
                            arr, delta_map.get(alias)
                        )
            if len(self._factor_series_cache) >= 8192:
                self._factor_series_cache.clear()
            self._factor_series_cache[key] = merged
            out[sym] = merged
        return out

    def _alias_canonical(self, symbol: str) -> str:
        """BSE pre-migration alias map (same source as the legacy derive)."""
        try:
            from .pit_universe import PointInTimeUniverse

            for mid in self.store.list_manifests():
                m = self.store.load_manifest(mid, deep_copy=False)
                if m is None or m.status != "ready":
                    continue
                if (m.universe_type or "").startswith("b1_reference"):
                    pit = PointInTimeUniverse.from_root(self.store.root, m.dataset_id)
                    for canon, w in pit.entries.items():
                        if symbol in w.aliases:
                            return canon
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # runtime QFQ derivation (identical math to legacy derive)
    # ------------------------------------------------------------------
    def qfq_arrays(
        self,
        symbol: str,
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        raw_arrays: Optional[Dict[str, np.ndarray]] = None,
        raw_watermark: Optional[int] = None,
        factor_watermark: Optional[int] = None,
    ) -> Optional[Dict[str, np.ndarray]]:
        """Derive QFQ bars for one symbol at read time.

        anchor = last factor on or before the raw cutoff; rows before the
        first factor date (leading gap) are dropped; a symbol with no factor
        series resolves to None (missing-factor semantics preserved).
        """
        raw = raw_arrays
        if raw is None:
            raw = self.merged_raw_arrays(
                symbol, watermark=raw_watermark
            )
        if raw is None or len(raw["trade_date"]) == 0:
            return None
        series = self._factor_series(symbol, watermark=factor_watermark)
        if series is None:
            return None
        fd, fv = series
        if len(fd) == 0:
            return None
        rd = raw["trade_date"]
        cutoff = int(rd[-1])
        aidx = int(np.searchsorted(fd, cutoff, side="right")) - 1
        if aidx < 0:
            return None  # no anchor factor on or before the raw cutoff
        anchor = float(fv[aidx])
        pos = np.searchsorted(fd, rd, side="right") - 1
        valid = pos >= 0
        if not np.any(valid):
            return None
        rdv = rd[valid]
        ratio = fv[pos[valid]] / anchor
        return {
            "trade_date": rdv,
            "open": np.round(raw["open"][valid] * ratio, QFQ_ROUND_DECIMALS),
            "high": np.round(raw["high"][valid] * ratio, QFQ_ROUND_DECIMALS),
            "low": np.round(raw["low"][valid] * ratio, QFQ_ROUND_DECIMALS),
            "close": np.round(raw["close"][valid] * ratio, QFQ_ROUND_DECIMALS),
            "volume": raw["volume"][valid],
            "amount": raw["amount"][valid],
        }

    def qfq_arrays_batch(
        self,
        symbols: Sequence[str],
        *,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        raw_watermark: Optional[int] = None,
        factor_watermark: Optional[int] = None,
    ) -> Dict[str, Optional[Dict[str, np.ndarray]]]:
        """Batch QFQ derivation — merged raw batch + merged factor batch.

        raw_watermark selects the raw base+delta surface; factor_watermark
        selects the factor surface. When either is None the current overlay
        watermark is used.
        """
        raws = self.merged_raw_arrays_batch(
            symbols,
            start_date=start_date,
            end_date=end_date,
            watermark=raw_watermark,
        )
        present = [sym for sym, raw in raws.items() if raw is not None]
        series_map = self._factor_series_batch(
            present, watermark=factor_watermark
        )
        out: Dict[str, Optional[Dict[str, np.ndarray]]] = {}
        for sym, raw in raws.items():
            if raw is None:
                out[sym] = None
                continue
            series = series_map.get(sym)
            if series is None:
                out[sym] = None
                continue
            fd, fv = series
            if len(fd) == 0:
                out[sym] = None
                continue
            rd = raw["trade_date"]
            if len(rd) == 0:
                out[sym] = {k: v for k, v in raw.items()}
                continue
            cutoff = int(rd[-1])
            aidx = int(np.searchsorted(fd, cutoff, side="right")) - 1
            if aidx < 0:
                out[sym] = None
                continue
            anchor = float(fv[aidx])
            pos = np.searchsorted(fd, rd, side="right") - 1
            valid = pos >= 0
            if not np.any(valid):
                out[sym] = None
                continue
            rdv = rd[valid]
            ratio = fv[pos[valid]] / anchor
            out[sym] = {
                "trade_date": rdv,
                "open": np.round(raw["open"][valid] * ratio, QFQ_ROUND_DECIMALS),
                "high": np.round(raw["high"][valid] * ratio, QFQ_ROUND_DECIMALS),
                "low": np.round(raw["low"][valid] * ratio, QFQ_ROUND_DECIMALS),
                "close": np.round(raw["close"][valid] * ratio, QFQ_ROUND_DECIMALS),
                "volume": raw["volume"][valid],
                "amount": raw["amount"][valid],
            }
        return out

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _pool_record(self, symbol: str):
        for rec in self._pool_records():
            if rec.symbol == symbol:
                return rec
        return None

    @staticmethod
    def _find_record(manifest: DatasetManifest, symbol: str):
        for rec in manifest.symbols:
            if rec.symbol == symbol:
                return rec
        return None


# ---------------------------------------------------------------------------
# merge helpers (pure functions for testability)
# ---------------------------------------------------------------------------


def _merge_base_and_delta(
    base_arr: Optional[Dict[str, np.ndarray]],
    delta_map: Optional[Dict[int, Tuple]],
) -> Optional[Dict[str, np.ndarray]]:
    """Base arrays overlaid by delta rows (newest-on-date wins).

    When a delta row exists for a date, its values replace the base row's;
    new dates are appended. Returns None when both are empty.
    """
    if base_arr is None and not delta_map:
        return None
    if base_arr is None:
        return _arrays_from_delta(delta_map)
    if not delta_map:
        return base_arr
    dates = list(base_arr["trade_date"].tolist())
    idx = {int(d): i for i, d in enumerate(dates)}
    o = base_arr["open"].tolist()
    h = base_arr["high"].tolist()
    l = base_arr["low"].tolist()
    c = base_arr["close"].tolist()
    v = base_arr["volume"].tolist()
    a = base_arr["amount"].tolist()
    for td, vals in delta_map.items():
        i = idx.get(int(td))
        if i is not None:
            o[i], h[i], l[i], c[i], v[i], a[i] = vals
        else:
            idx[int(td)] = len(dates)
            dates.append(int(td))
            o.append(vals[0]); h.append(vals[1]); l.append(vals[2])
            c.append(vals[3]); v.append(vals[4]); a.append(vals[5])
    order = np.argsort(dates)
    return {
        "trade_date": np.asarray(dates, dtype=np.int64)[order],
        "open": np.asarray(o, dtype=np.float64)[order],
        "high": np.asarray(h, dtype=np.float64)[order],
        "low": np.asarray(l, dtype=np.float64)[order],
        "close": np.asarray(c, dtype=np.float64)[order],
        "volume": np.asarray(v, dtype=np.float64)[order],
        "amount": np.asarray(a, dtype=np.float64)[order],
    }


def _arrays_from_delta(
    delta_map: Dict[int, Tuple],
) -> Dict[str, np.ndarray]:
    dates = sorted(delta_map)
    o = [delta_map[d][0] for d in dates]
    h = [delta_map[d][1] for d in dates]
    l = [delta_map[d][2] for d in dates]
    c = [delta_map[d][3] for d in dates]
    v = [delta_map[d][4] for d in dates]
    a = [delta_map[d][5] for d in dates]
    return {
        "trade_date": np.asarray(dates, dtype=np.int64),
        "open": np.asarray(o, dtype=np.float64),
        "high": np.asarray(h, dtype=np.float64),
        "low": np.asarray(l, dtype=np.float64),
        "close": np.asarray(c, dtype=np.float64),
        "volume": np.asarray(v, dtype=np.float64),
        "amount": np.asarray(a, dtype=np.float64),
    }


def _merge_factor_base_and_delta(
    base_arr: Optional[Dict[str, np.ndarray]],
    delta_map: Optional[Dict[int, float]],
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Merge factor arrays; delta wins on date; strictly ascending dates."""
    if base_arr is None and not delta_map:
        return None
    dates: List[int] = []
    vals: List[float] = []
    if base_arr is not None:
        dates = list(base_arr["trade_date"].tolist())
        vals = list(base_arr["adj_factor"].tolist())
    if delta_map:
        idx = {int(d): i for i, d in enumerate(dates)}
        for td, f in delta_map.items():
            i = idx.get(int(td))
            if i is not None:
                vals[i] = float(f)
            else:
                idx[int(td)] = len(dates)
                dates.append(int(td))
                vals.append(float(f))
    order = np.argsort(dates)
    fd = np.asarray(dates, dtype=np.int64)[order]
    fv = np.asarray(vals, dtype=np.float64)[order]
    if len(fd) == 0:
        return None
    if not np.all(fv > 0):
        return None
    return fd, fv


def _slice_arrays(
    arr: Dict[str, np.ndarray],
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, np.ndarray]:
    d = arr["trade_date"]
    mask = np.ones(len(d), dtype=bool)
    if start_date is not None:
        mask &= d >= int(start_date)
    if end_date is not None:
        mask &= d <= int(end_date)
    if not np.any(mask):
        # keep an empty but well-formed arrays dict so callers can cheaply
        # iterate zero rows
        return {k: v[mask] for k, v in arr.items()}
    return {k: v[mask] for k, v in arr.items()}
