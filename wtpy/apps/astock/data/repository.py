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
    def _symbol_kind_std(code: str, suffix: str) -> str:
        """Classify a bare code + exchange suffix into IDX/ETF/STK.

        Segment rules mirror Tushare:
          indices  SH 000xxx (sh000001) / SZ 399xxx (sz399006)
          ETFs     SH 51/56/58xxxx / SZ 15/16/18xxxx
          otherwise a stock.
        """
        if suffix == "SH" and code.startswith("000"):
            return "IDX"
        if suffix == "SZ" and code.startswith("399"):
            return "IDX"
        if suffix == "SH" and code.startswith(("51", "56", "58")):
            return "ETF"
        if suffix == "SZ" and code.startswith(("15", "16", "18")):
            return "ETF"
        return "STK"

    @staticmethod
    def _symbol_variants(symbol: str) -> List[str]:
        """Return all known format variants for a symbol.

        Handles: SSE.STK.600000 <-> 600000.SH <-> sh600000 <-> 600000
                 SZSE.STK.000001 <-> 000001.SZ <-> sz000001 <-> 000001
                 BSE.STK.430047 <-> 430047.BJ <-> bj430047 <-> 430047
                 SSE.IDX.000001 <-> 000001.SH <-> sh000001
                 SSE.ETF.510300 <-> 510300.SH <-> sh510300
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
                kind = (
                    "STK"
                    if suffix_upper == "BJ"
                    else MarketDataRepository._symbol_kind_std(code, suffix_upper)
                )
                variants.append(f"{exch}.{kind}.{code}")
                variants.append(f"{suffix.lower()}{code}")
            variants.append(code)
        elif len(symbol) == 8 and symbol[:2].lower() in ("sh", "sz", "bj") and symbol[2:].isdigit():
            prefix_map = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}
            suffix_map = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
            code = symbol[2:]
            pfx = symbol[:2].lower()
            kind = (
                "STK"
                if pfx == "bj"
                else MarketDataRepository._symbol_kind_std(code, suffix_map[pfx])
            )
            variants.append(f"{prefix_map[pfx]}.{kind}.{code}")
            variants.append(f"{code}.{suffix_map[pfx]}")
            variants.append(code)
        elif symbol.isdigit() and len(symbol) == 6:
            # BSE segments: 43/83/87 (historical) + 920 (post-migration).
            # SSE B-shares are 900xxx — bare 92xxxx is always BSE.
            if symbol.startswith(("4", "8")) or symbol.startswith("92"):
                variants.append(f"BSE.STK.{symbol}")
                variants.append(f"{symbol}.BJ")
                variants.append(f"bj{symbol}")
            elif symbol.startswith(("5", "6", "9")):
                variants.append(f"SSE.STK.{symbol}")
                variants.append(f"{symbol}.SH")
                variants.append(f"sh{symbol}")
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
        deep_copy: bool = True,
    ) -> List[DatasetManifest]:
        results = []
        for ds_id in self._store.list_manifests():
            m = self._store.load_manifest(ds_id, deep_copy=deep_copy)
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

    def get_dataset(self, dataset_id: str, *, deep_copy: bool = True) -> DatasetManifest:
        m = self._store.load_manifest(dataset_id, deep_copy=deep_copy)
        if m is None:
            raise DatasetNotFoundError(f"Dataset not found: {dataset_id}")
        return m

    @staticmethod
    def readiness_score(m: DatasetManifest) -> tuple:
        """Rank datasets for product selection (higher is better).

        Preference order:
          1. data_cutoff_date — freshest market coverage
          2. symbol_count — fullest board (blocks tiny subsets)
          3. row_count — real bar/factor volume (blocks empty shells)
          4. created_at — tie-break
        """
        return (
            int(getattr(m, "data_cutoff_date", None) or 0),
            int(getattr(m, "symbol_count", None) or 0),
            int(getattr(m, "row_count", None) or 0),
            getattr(m, "created_at", None) or "",
        )

    def resolve_latest_ready(
        self,
        *,
        source: str,
        adjustment: str,
        period: str,
    ) -> DatasetManifest:
        """Find the best ready dataset matching criteria.

        Preference order (desc): cutoff → symbol_count → row_count → created_at.
        Partial / superseded / failed are never selected.

        Raises DatasetNotFoundError if none exists.
        """
        candidates = self.list_datasets(
            source=source,
            adjustment=adjustment,
            period=period,
            status="ready",
        )
        # Drop empty shells and obvious test stubs (no cutoff + tiny board).
        usable = []
        for m in candidates:
            n_sym = int(m.symbol_count or 0)
            n_row = int(m.row_count or 0)
            if n_sym <= 0:
                continue
            # factor/bar product sets must have real rows once symbol_count is large
            if n_sym >= 50 and n_row <= 0:
                continue
            usable.append(m)
        if not usable:
            usable = candidates
        if not usable:
            raise DatasetNotFoundError(
                f"No ready dataset for source={source} adjustment={adjustment} "
                f"period={period}. Run sync first."
            )
        usable.sort(key=self.readiness_score, reverse=True)
        return usable[0]

    def supersede_dominated_ready(
        self,
        winner: DatasetManifest,
        *,
        min_symbol_ratio: float = 0.5,
    ) -> List[str]:
        """Mark smaller same-family ready sets as superseded after a full publish.

        A candidate is dominated when it shares source/adjustment/period, is
        ready, has fewer symbols than ``winner * min_symbol_ratio`` (or equal
        cutoff with strictly fewer symbols / rows). Returns demoted ids.
        """
        if not winner or (winner.status or "") != "ready":
            return []
        demoted: List[str] = []
        win_n = int(winner.symbol_count or 0)
        win_rows = int(winner.row_count or 0)
        win_cut = int(winner.data_cutoff_date or 0)
        if win_n <= 0:
            return []
        peers = self.list_datasets(
            source=winner.source,
            adjustment=winner.adjustment,
            period=winner.period or "1d",
            status="ready",
        )
        win_syms = {
            r.symbol for r in (winner.symbols or []) if getattr(r, "blob_sha256", None)
        }
        for m in peers:
            if m.dataset_id == winner.dataset_id:
                continue
            n = int(m.symbol_count or 0)
            rows = int(m.row_count or 0)
            cut = int(m.data_cutoff_date or 0)
            tiny = win_n >= 1000 and n < max(50, int(win_n * float(min_symbol_ratio)))
            empty = n >= 50 and rows <= 0 and win_rows > 0
            older_smaller = cut <= win_cut and n < win_n and rows < win_rows
            if not (tiny or empty or older_smaller):
                continue
            # A same-family set with unique symbol coverage is a supplement
            # role (e.g. the delisted-factor set feeding the formal L1), not
            # a dominated duplicate — demoting it would break factor
            # resolution for symbols the winner does not carry.
            cand_syms = {
                r.symbol for r in (m.symbols or []) if getattr(r, "blob_sha256", None)
            }
            if cand_syms and win_syms and not cand_syms <= win_syms:
                continue
            m.status = "superseded"
            prov = dict(getattr(m, "provenance", None) or {})
            prov["superseded_reason"] = (
                f"dominated_by:{winner.dataset_id};"
                f"win_sym={win_n};self_sym={n};win_cut={win_cut};self_cut={cut}"
            )
            prov["previous_status"] = "ready"
            m.provenance = prov
            warn = (getattr(m, "warning_text", None) or "").strip()
            note = f"superseded by {winner.dataset_id}"
            m.warning_text = f"{warn} | {note}" if warn else note
            self._store.save_manifest(m)
            demoted.append(m.dataset_id)
        return demoted

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
        result = [
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
        if len(result) > 1 and result[0].date > result[-1].date:
            result.reverse()
        return result

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
