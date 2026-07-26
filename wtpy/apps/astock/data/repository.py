"""MarketDataRepository – read-only access to fixed local datasets.

Backtest tasks must ONLY use this repository. Providers are never called
during a backtest run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from .dataset_store import DatasetManifest, DatasetStore
from .providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketBar,
    WeeklyBarMode,
)
from .tdx_reader import DayBar


class DatasetNotFoundError(Exception):
    pass


class DatasetNotReadyError(Exception):
    pass


class MarketDataRepository:
    """Reads bars exclusively from local immutable datasets."""

    def __init__(self, store: DatasetStore):
        self._store = store

    @classmethod
    def from_root(cls, root: Path | str) -> "MarketDataRepository":
        return cls(DatasetStore(root))

    @staticmethod
    def _symbol_variants(symbol: str) -> List[str]:
        """Return all known format variants for a symbol.

        Handles: SSE.STK.600000 <-> 600000.SH <-> sh600000 <-> 600000
                 SZSE.STK.000001 <-> 000001.SZ <-> sz000001 <-> 000001
                 BSE.STK.430047 <-> 430047.BJ <-> bj430047 <-> 430047
        """
        variants = [symbol]
        parts = symbol.split(".")
        if len(parts) == 3:
            exch, _, code = parts
            suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exch)
            if suffix:
                variants.append(f"{code}.{suffix}")
                variants.append(f"{suffix.lower()}{code}")
            variants.append(code)
        elif len(parts) == 2:
            code, suffix = parts
            suffix_upper = suffix.upper()
            exch = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix_upper)
            if exch:
                variants.append(f"{exch}.STK.{code}")
                variants.append(f"{suffix.lower()}{code}")
            variants.append(code)
        elif len(symbol) == 8 and symbol[:2].lower() in ("sh", "sz", "bj") and symbol[2:].isdigit():
            prefix_map = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}
            suffix_map = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
            code = symbol[2:]
            pfx = symbol[:2].lower()
            variants.append(f"{prefix_map[pfx]}.STK.{code}")
            variants.append(f"{code}.{suffix_map[pfx]}")
            variants.append(code)
        elif symbol.isdigit() and len(symbol) == 6:
            if symbol.startswith(("5", "6", "9")):
                variants.append(f"SSE.STK.{symbol}")
                variants.append(f"{symbol}.SH")
                variants.append(f"sh{symbol}")
            elif symbol.startswith(("4", "8")):
                variants.append(f"BSE.STK.{symbol}")
                variants.append(f"{symbol}.BJ")
                variants.append(f"bj{symbol}")
            else:
                variants.append(f"SZSE.STK.{symbol}")
                variants.append(f"{symbol}.SZ")
                variants.append(f"sz{symbol}")
        return variants

    def _find_symbol_record(self, manifest: DatasetManifest, symbol: str):
        """Find a symbol record trying all format variants."""
        for variant in self._symbol_variants(symbol):
            for s in manifest.symbols:
                if s.symbol == variant:
                    return s
        return None

    def list_datasets(
        self,
        *,
        source: Optional[str] = None,
        adjustment: Optional[str] = None,
        period: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[DatasetManifest]:
        results = []
        for ds_id in self._store.list_manifests():
            m = self._store.load_manifest(ds_id)
            if m is None:
                continue
            if source and m.source != source:
                continue
            if adjustment and m.adjustment != adjustment:
                continue
            if period and m.period != period:
                continue
            if status and m.status != status:
                continue
            results.append(m)
        return results

    def get_dataset(self, dataset_id: str) -> DatasetManifest:
        m = self._store.load_manifest(dataset_id)
        if m is None:
            raise DatasetNotFoundError(f"Dataset not found: {dataset_id}")
        return m

    def resolve_latest_ready(
        self,
        *,
        source: str,
        adjustment: str,
        period: str,
    ) -> DatasetManifest:
        """Find the latest ready dataset matching criteria.

        Raises DatasetNotFoundError if none exists.
        Partial datasets are never selected.
        """
        candidates = self.list_datasets(
            source=source,
            adjustment=adjustment,
            period=period,
            status="ready",
        )
        if not candidates:
            raise DatasetNotFoundError(
                f"No ready dataset for source={source} adjustment={adjustment} "
                f"period={period}. Run sync first."
            )
        candidates.sort(
            key=lambda m: (m.data_cutoff_date or 0, m.created_at or ""),
            reverse=True,
        )
        return candidates[0]

    def load_bars(
        self,
        *,
        dataset_id: str,
        symbol: Optional[str] = None,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        allow_partial: bool = False,
    ) -> List[MarketBar]:
        """Load bars from a specific dataset.

        If symbol is None, loads all symbols in the dataset.
        By default only status=ready datasets can be loaded.
        Set allow_partial=True only for audit tooling, never for backtest.
        """
        manifest = self.get_dataset(dataset_id)
        allowed = ("ready",) if not allow_partial else ("ready", "partial")
        if manifest.status not in allowed:
            raise DatasetNotReadyError(
                f"Dataset {dataset_id} status={manifest.status}, cannot load"
            )

        bars: List[MarketBar] = []
        targets = manifest.symbols
        if symbol:
            rec = self._find_symbol_record(manifest, symbol)
            if rec is None:
                raise DatasetNotFoundError(
                    f"Symbol {symbol} not in dataset {dataset_id}"
                )
            targets = [rec]

        for sym_rec in targets:
            if not sym_rec.blob_sha256:
                continue
            arrays = self._store.load_bars(sym_rec.blob_sha256)
            n = len(arrays["trade_date"])
            for i in range(n):
                td = int(arrays["trade_date"][i])
                if start_date is not None and td < start_date:
                    continue
                if end_date is not None and td > end_date:
                    continue
                bars.append(
                    MarketBar(
                        symbol=sym_rec.symbol,
                        trade_date=td,
                        period=manifest.period,
                        open=float(arrays["open"][i]),
                        high=float(arrays["high"][i]),
                        low=float(arrays["low"][i]),
                        close=float(arrays["close"][i]),
                        volume=float(arrays["volume"][i]),
                        amount=float(arrays["amount"][i]),
                        source=manifest.source,
                        adjustment=manifest.adjustment,
                        anchor_date=manifest.anchor_date,
                        snapshot_date=manifest.snapshot_date,
                        data_cutoff_date=manifest.data_cutoff_date,
                        provider_version=manifest.provider_version,
                    )
                )
        return bars

    def load_day_bars(
        self,
        *,
        dataset_id: str,
        symbol: str,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> List[DayBar]:
        """Load bars as legacy DayBar for compatibility with existing engine."""
        market_bars = self.load_bars(
            dataset_id=dataset_id,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        return [
            DayBar(
                date=b.trade_date,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                amount=b.amount,
                volume=b.volume,
            )
            for b in market_bars
        ]

    def validate_dataset(self, dataset_id: str) -> Dict:
        """Validate dataset integrity: all blobs present, row counts match."""
        manifest = self.get_dataset(dataset_id)
        issues = []
        for sym in manifest.symbols:
            if not sym.blob_sha256:
                if sym.error:
                    continue
                issues.append(f"{sym.symbol}: missing blob_sha256")
                continue
            if not self._store.blob_exists(sym.blob_sha256):
                issues.append(f"{sym.symbol}: blob {sym.blob_sha256[:12]} missing")
                continue
            arrays = self._store.load_bars(sym.blob_sha256)
            actual_rows = len(arrays["trade_date"])
            if actual_rows != sym.row_count:
                issues.append(
                    f"{sym.symbol}: row_count mismatch "
                    f"(manifest={sym.row_count}, actual={actual_rows})"
                )
        return {
            "dataset_id": dataset_id,
            "status": manifest.status,
            "symbol_count": manifest.symbol_count,
            "issues": issues,
            "valid": len(issues) == 0,
        }
