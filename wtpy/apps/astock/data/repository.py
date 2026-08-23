"""MarketDataRepository – read-only access to fixed local datasets.

Backtest tasks must ONLY use this repository. Providers are never called
during a backtest run.

Storage modes:
  - blob_snapshot (legacy): each dataset is a manifest + full-history NPZ blob
    per symbol; ``load_bars`` reads the blobs directly.
  - overlay_v1: stable base blobs + DuckDB versioned delta. Formal L1/L2 are
    VIRTUAL manifests (``storage_mode="overlay_v1"``) whose bars are merged
    (base + delta) or runtime-derived (QFQ) by ``data/overlay.OverlayView``.
    ``load_bar_arrays`` / ``load_bars_batch`` are the unified entry points;
    ``load_bars`` stays as the compatible wrapper.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .dataset_store import DatasetManifest, DatasetStore, SymbolRecord
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


#: 每个 repository 实例缓存的 manifest view 上限。全市场导出的逐股票循环
#: 只涉及 2-3 个虚拟 manifest（L1/L2/raw），4 足够且能容纳 consolidation 前
#: 后的新旧代次并存；不引入环境变量，保持部署面不变。
_MANIFEST_VIEW_CACHE_MAX = 4


class MarketDataRepository:
    """Reads bars exclusively from local immutable datasets."""

    #: per-repository overlay view (lazy); virtual L1/L2 resolution.
    #: Class-level default only — the first access assigns an instance attr.
    _overlay = None
    _overlay_lock = threading.Lock()

    def __init__(self, store: DatasetStore):
        self._store = store
        # ---- manifest view LRU（修复 0f009e9 引入的回归）----
        # _load_virtual_bars 曾改为每次调用 OverlayView.for_manifest()，
        # 而 OverlayView 的 delta/pool/factor 缓存都是实例字段——每次新建
        # 实例等于缓存永不命中，全市场导出退化为每股一次 DuckDB 全量扫描。
        # 这里按 (dataset_id, manifest_sha256) 复用视图；虚拟 manifest 把
        # watermark/代次钉死在自身身份里（for_manifest 用 _state_override
        # 回放），因此按 manifest 身份缓存不会串代次。
        self._manifest_views: "OrderedDict[Tuple[str, str], Tuple[object, Dict[str, SymbolRecord]]]" = OrderedDict()
        self._manifest_views_lock = threading.RLock()

    @classmethod
    def from_root(cls, root: Path | str) -> "MarketDataRepository":
        return cls(DatasetStore(root))

    @property
    def manifests_dir(self) -> Path:
        """manifest 文件目录透传（供调用方做文件实算哈希校验）。"""
        return self._store.manifests_dir

    # ------------------------------------------------------------------
    # overlay support
    # ------------------------------------------------------------------
    def _overlay_enabled(self) -> bool:
        from .delta_store import load_overlay_state

        return load_overlay_state(self._store.root).enabled

    def _overlay_view(self):
        if self._overlay is not None:
            return self._overlay
        with self._overlay_lock:
            if self._overlay is not None:
                return self._overlay
            from .overlay import OverlayView

            view = OverlayView.from_root(self._store.root, required=False)
            if view is not None:
                self._overlay = view
            return view

    @staticmethod
    def _manifest_view_key(manifest: DatasetManifest) -> Tuple[str, str]:
        """LRU cache key: manifest identity (id + content hash).

        虚拟 manifest 把 watermark/代次钉死在自身身份里，consolidation
        换代产生新 id/sha，旧缓存条目自然失效、不会串代次。
        """
        return (manifest.dataset_id, getattr(manifest, "manifest_sha256", "") or "")

    def _overlay_entry_for_manifest(
        self, manifest: DatasetManifest
    ) -> Tuple[object, Dict[str, SymbolRecord]]:
        """Return (OverlayView, exact-symbol index) for a virtual manifest.

        查询、创建、LRU 更新与淘汰都在同一把锁内完成：并发首次访问只会
        创建一个实例（for_manifest 只做 manifest/registry 读取，不做 NPZ
        解压，锁内创建的开销可控）。
        """
        key = self._manifest_view_key(manifest)
        with self._manifest_views_lock:
            entry = self._manifest_views.get(key)
            if entry is not None:
                self._manifest_views.move_to_end(key)
                return entry
            from .overlay import OverlayView

            view = OverlayView.for_manifest(self._store, manifest)
            sym_index: Dict[str, SymbolRecord] = {}
            for r in manifest.symbols:
                sym_index[r.symbol] = r
            self._manifest_views[key] = (view, sym_index)
            while len(self._manifest_views) > _MANIFEST_VIEW_CACHE_MAX:
                self._manifest_views.popitem(last=False)
            return self._manifest_views[key]

    def _overlay_view_for_manifest(self, manifest: DatasetManifest):
        return self._overlay_entry_for_manifest(manifest)[0]

    def _virtual_symbol_record(
        self, manifest: DatasetManifest, symbol: str
    ) -> Optional[SymbolRecord]:
        """Exact-spelling O(1) lookup against the cached manifest index.

        未命中时回退到变体线性扫描，保持与 _find_symbol_record 相同的
        外部行为（调用方可能传 600000.SH 等非规范拼法）。
        """
        key = self._manifest_view_key(manifest)
        with self._manifest_views_lock:
            entry = self._manifest_views.get(key)
            idx = entry[1] if entry is not None else None
        if idx is not None:
            rec = idx.get(symbol)
            if rec is not None:
                return rec
        return self._find_symbol_record(manifest, symbol)

    def load_record_bars(
        self,
        *,
        manifest: DatasetManifest,
        record: SymbolRecord,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> List[MarketBar]:
        """Load bars for an already-resolved SymbolRecord.

        全市场循环（BaguaPlaneSession）已持有 manifest/record，直接传入可
        跳过 load_bars 内部重复的变体线性扫描；读取语义与
        ``load_bars(dataset_id=..., symbol=record.symbol, ...)`` 完全一致。
        """
        allowed = ("ready",)
        if manifest.status not in allowed:
            raise DatasetNotReadyError(
                f"Dataset {manifest.dataset_id} status={manifest.status}, cannot load"
            )
        if getattr(manifest, "storage_mode", "") == "overlay_v1":
            return self._load_virtual_bars(
                manifest, symbol=record.symbol,
                start_date=start_date, end_date=end_date, record=record,
            )
        bars: List[MarketBar] = []
        if not record.blob_sha256:
            return bars
        arrays = self._store.load_bars(record.blob_sha256)
        n = len(arrays["trade_date"])
        for i in range(n):
            td = int(arrays["trade_date"][i])
            if start_date is not None and td < start_date:
                continue
            if end_date is not None and td > end_date:
                continue
            bars.append(
                MarketBar(
                    symbol=record.symbol,
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
        if suffix == "SH" and code.startswith(("51", "52", "530", "551", "56", "58")):
            return "ETF"
        if suffix == "SZ" and code.startswith(("158", "159")):
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
            elif symbol.startswith(("51", "52", "530", "551", "56", "58")):
                # 沪市 ETF 段（52/530/551 新段）：必须生成 .ETF. 形态，否则
                # 正式面锁定会把裸代码查询当股票过滤（历史上 561830 因此
                # 无法解析甚至串号到深市品种）。
                variants.append(f"SSE.ETF.{symbol}")
                variants.append(f"{symbol}.SH")
                variants.append(f"sh{symbol}")
            elif symbol.startswith(("158", "159")):
                # 深市 ETF 段（159 传统段 + 158 新段；16xxxx 是 LOF、其余
                # 15/18 是其他场内基金，不在此映射）
                variants.append(f"SZSE.ETF.{symbol}")
                variants.append(f"{symbol}.SZ")
                variants.append(f"sz{symbol}")
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

        In overlay_v1 mode, the formal L1/L2 product roles resolve to their
        current virtual manifests (stable base blobs + DuckDB delta) instead
        of a materialized snapshot; explicit legacy dataset ids keep reading
        their original blobs.

        Raises DatasetNotFoundError if none exists.
        """
        if self._overlay_enabled():
            view = self._overlay_view()
            if view is not None:
                vm = _virtual_manifest_for(view, source, adjustment, period)
                if vm is not None:
                    return vm
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

        Overlay-aware: a virtual manifest (storage_mode=overlay_v1) is served
        from the merged base+delta surface (raw/composite views) or the
        runtime-derived QFQ view instead of per-symbol blobs.
        """
        manifest = self.get_dataset(dataset_id, deep_copy=False)
        allowed = ("ready",) if not allow_partial else ("ready", "partial")
        if manifest.status not in allowed:
            raise DatasetNotReadyError(
                f"Dataset {dataset_id} status={manifest.status}, cannot load"
            )

        # ---- overlay virtual view path ----
        if getattr(manifest, "storage_mode", "") == "overlay_v1":
            record = None
            if symbol is not None:
                record = self._virtual_symbol_record(manifest, symbol)
                if record is None:
                    raise DatasetNotFoundError(
                        f"Symbol {symbol} not in dataset {dataset_id}"
                    )
            return self._load_virtual_bars(
                manifest, symbol=symbol,
                start_date=start_date, end_date=end_date,
                record=record,
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

    # ------------------------------------------------------------------
    # overlay / unified array reads
    # ------------------------------------------------------------------
    def _load_virtual_bars(
        self,
        manifest: DatasetManifest,
        *,
        symbol: Optional[str],
        start_date: Optional[int],
        end_date: Optional[int],
        record: Optional[SymbolRecord] = None,
    ) -> List[MarketBar]:
        view = self._overlay_view_for_manifest(manifest)

        # per-symbol fast path: skip the batch dict wrapper + a second
        # manifest load (whole-market loops call this once per symbol).
        if symbol is not None:
            if record is None:
                record = self._virtual_symbol_record(manifest, symbol)
            if record is None:
                raise DatasetNotFoundError(
                    f"Symbol {symbol} not in dataset {manifest.dataset_id}"
                )
            resolved_symbol = record.symbol
            if manifest.view_type == "l1_virtual_qfq":
                arr = view.qfq_arrays(
                    resolved_symbol,
                    start_date=start_date,
                    end_date=end_date,
                    raw_watermark=manifest.delta_watermark,
                    factor_watermark=manifest.factor_watermark,
                )
            else:
                arr = view.merged_raw_arrays(
                    resolved_symbol,
                    start_date=start_date,
                    end_date=end_date,
                    watermark=manifest.delta_watermark,
                )
            return self._bars_from_arrays(manifest, resolved_symbol, arr)

        symbols = [r.symbol for r in manifest.symbols]
        arrays_map = self.load_bar_arrays(
            dataset_id=manifest.dataset_id,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )
        bars: List[MarketBar] = []
        for sym in symbols:
            bars.extend(self._bars_from_arrays(manifest, sym, arrays_map.get(sym)))
        return bars

    @staticmethod
    def _bars_from_arrays(
        manifest: DatasetManifest, sym: str, arr
    ) -> List[MarketBar]:
        if arr is None or len(arr["trade_date"]) == 0:
            return []
        n = len(arr["trade_date"])
        return [
            MarketBar(
                symbol=sym,
                trade_date=int(arr["trade_date"][i]),
                period=manifest.period,
                open=float(arr["open"][i]),
                high=float(arr["high"][i]),
                low=float(arr["low"][i]),
                close=float(arr["close"][i]),
                volume=float(arr["volume"][i]),
                amount=float(arr["amount"][i]),
                source=manifest.source,
                adjustment=manifest.adjustment,
                anchor_date=manifest.anchor_date,
                snapshot_date=manifest.snapshot_date,
                data_cutoff_date=manifest.data_cutoff_date,
                provider_version=manifest.provider_version,
            )
            for i in range(n)
        ]

    def load_bar_arrays(
        self,
        *,
        dataset_id: str,
        symbols: Optional[Sequence[str]] = None,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> Dict[str, Optional[Dict[str, np.ndarray]]]:
        """Unified bar-array read (blob datasets AND overlay virtual views).

        Returns {symbol: {trade_date/open/high/low/close/volume/amount arrays}}
        with the symbol's exact manifest spelling as the key. ``symbols=None``
        loads every symbol with a blob in the dataset.

        Virtual manifest behavior:
          - l2_virtual_composite / raw_virtual : base blob + DuckDB delta merge
          - l1_virtual_qfq                    : runtime QFQ derivation
        Blob manifests read their per-symbol blobs (no delta).
        """
        manifest = self.get_dataset(dataset_id, deep_copy=False)
        allowed = ("ready",)
        if manifest.status not in allowed:
            raise DatasetNotReadyError(
                f"Dataset {dataset_id} status={manifest.status}, cannot load"
            )
        view_type = getattr(manifest, "view_type", "")
        storage_mode = getattr(manifest, "storage_mode", "blob_snapshot")

        if storage_mode == "overlay_v1":
            view = self._overlay_view_for_manifest(manifest)
            if symbols is None:
                targets = [r.symbol for r in manifest.symbols]
            else:
                targets = []
                seen = set()
                for requested_symbol in symbols:
                    record = self._virtual_symbol_record(manifest, requested_symbol)
                    if record is None or record.symbol in seen:
                        continue
                    seen.add(record.symbol)
                    targets.append(record.symbol)
            if not targets:
                return {}
            if view_type == "l1_virtual_qfq":
                return view.qfq_arrays_batch(
                    targets,
                    start_date=start_date,
                    end_date=end_date,
                    raw_watermark=manifest.delta_watermark,
                    factor_watermark=manifest.factor_watermark,
                )
            return view.merged_raw_arrays_batch(
                targets,
                start_date=start_date,
                end_date=end_date,
                watermark=manifest.delta_watermark,
            )

        # ---- legacy blob snapshot path ----
        targets = (
            [r.symbol for r in manifest.symbols if r.blob_sha256]
            if symbols is None
            else list(symbols)
        )
        out: Dict[str, Optional[Dict[str, np.ndarray]]] = {}
        for sym in targets:
            rec = self._find_symbol_record(manifest, sym)
            if rec is None or not rec.blob_sha256:
                continue
            try:
                arr = self._store.load_bars(rec.blob_sha256)
            except FileNotFoundError:
                continue
            arr = _slice_arrays(arr, start_date, end_date)
            out[rec.symbol] = arr
        return out

    def load_factor_arrays(
        self,
        *,
        dataset_id: str,
        symbols: Sequence[str],
    ) -> Dict[str, Optional[Dict[str, np.ndarray]]]:
        """Unified factor-array read for blob and overlay factor datasets."""
        manifest = self.get_dataset(dataset_id, deep_copy=False)
        if manifest.status != "ready" or manifest.dataset_type != "factor":
            raise DatasetNotReadyError(
                f"Factor dataset {dataset_id} is not a ready factor surface"
            )
        if (
            getattr(manifest, "storage_mode", "") == "overlay_v1"
            and getattr(manifest, "view_type", "") == "factor_virtual"
        ):
            view = self._overlay_view_for_manifest(manifest)
            resolved = {}
            canonical_symbols = []
            seen = set()
            for symbol in symbols:
                record = self._find_symbol_record(manifest, symbol)
                canonical = record.symbol if record is not None else None
                resolved[symbol] = canonical
                if canonical is not None and canonical not in seen:
                    seen.add(canonical)
                    canonical_symbols.append(canonical)
            arrays = view.factor_arrays_batch(
                canonical_symbols, watermark=manifest.factor_watermark
            )
            return {
                symbol: arrays.get(canonical) if canonical is not None else None
                for symbol, canonical in resolved.items()
            }

        out: Dict[str, Optional[Dict[str, np.ndarray]]] = {}
        for symbol in symbols:
            record = self._find_symbol_record(manifest, symbol)
            if record is None or not record.blob_sha256:
                out[symbol] = None
                continue
            out[symbol] = self._store.load_bars(record.blob_sha256)
        return out

    def load_bars_batch(
        self,
        *,
        dataset_id: str,
        symbols: Sequence[str],
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
    ) -> Dict[str, Optional[List[MarketBar]]]:
        """Batch MarketBar loader for whole-market loops.

        One DuckDB query per overlay batch call; never creates a database
        connection per symbol. Returns {symbol: bars or None}.
        """
        arrays_map = self.load_bar_arrays(
            dataset_id=dataset_id,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )
        manifest = self.get_dataset(dataset_id, deep_copy=False)
        out: Dict[str, Optional[List[MarketBar]]] = {}
        for sym, arr in arrays_map.items():
            if arr is None or len(arr["trade_date"]) == 0:
                out[sym] = None
                continue
            n = len(arr["trade_date"])
            bars = [
                MarketBar(
                    symbol=sym,
                    trade_date=int(arr["trade_date"][i]),
                    period=manifest.period,
                    open=float(arr["open"][i]),
                    high=float(arr["high"][i]),
                    low=float(arr["low"][i]),
                    close=float(arr["close"][i]),
                    volume=float(arr["volume"][i]),
                    amount=float(arr["amount"][i]),
                    source=manifest.source,
                    adjustment=manifest.adjustment,
                    anchor_date=manifest.anchor_date,
                    snapshot_date=manifest.snapshot_date,
                    data_cutoff_date=manifest.data_cutoff_date,
                    provider_version=manifest.provider_version,
                )
                for i in range(n)
            ]
            out[sym] = bars
        return out

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
        """Validate dataset integrity: all blobs present, row counts match.

        Overlay virtual manifests validate their base lineage + delta health
        instead of per-symbol blobs (they carry none).
        """
        manifest = self.get_dataset(dataset_id)
        issues = []
        if getattr(manifest, "storage_mode", "") == "overlay_v1":
            return self._validate_virtual_dataset(manifest, issues)
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

    def _validate_virtual_dataset(
        self, manifest: DatasetManifest, issues: List
    ) -> Dict:
        try:
            view = self._overlay_view_for_manifest(manifest)
        except Exception as exc:  # noqa: BLE001
            issues.append(
                f"overlay view unavailable: {type(exc).__name__}: {exc}"
            )
            view = None

        base = None
        if view is not None:
            try:
                base = view.active_base()
                if base.status != "ready":
                    issues.append(f"base dataset not ready: {base.dataset_id}")
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    "base dataset unavailable: "
                    f"{type(exc).__name__}: {exc}"
                )
        elif not manifest.base_dataset_id:
            issues.append("base_dataset_id missing")

        if view is not None:
            delta = view.delta
            if delta is None:
                issues.append("delta store unavailable")
            else:
                view_type = str(getattr(manifest, "view_type", "") or "")
                raw_types = {
                    "raw_virtual",
                    "l2_virtual_composite",
                    "l1_virtual_qfq",
                }
                factor_types = {"factor_virtual", "l1_virtual_qfq"}
                if view_type in {"l2_virtual_composite", "l1_virtual_qfq"}:
                    try:
                        delisted_base = view.delisted_base()
                        if (
                            delisted_base is not None
                            and delisted_base.status != "ready"
                        ):
                            issues.append(
                                "delisted base dataset not ready: "
                                f"{delisted_base.dataset_id}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        issues.append(
                            "delisted base unavailable: "
                            f"{type(exc).__name__}: {exc}"
                        )

                raw_watermark = int(manifest.delta_watermark or 0)
                raw_commit_seq = int(manifest.delta_commit_seq or 0)
                base_cutoff = int(base.data_cutoff_date or 0) if base else 0
                expected_raw = (
                    raw_watermark
                    if view_type in raw_types
                    and (raw_commit_seq > 0 or raw_watermark > base_cutoff)
                    else 0
                )

                expected_factor = None
                factor_commit_seq = int(manifest.factor_commit_seq or 0)
                if view_type in factor_types:
                    try:
                        factor_base = view.factor_base()
                        if factor_base.status != "ready":
                            issues.append(
                                "factor base dataset not ready: "
                                f"{factor_base.dataset_id}"
                            )
                        supplement = view.supplement_factor_base()
                        if (
                            supplement is not None
                            and supplement.status != "ready"
                        ):
                            issues.append(
                                "supplement factor base dataset not ready: "
                                f"{supplement.dataset_id}"
                            )
                        factor_base_cutoff = int(
                            factor_base.data_cutoff_date or 0
                        )
                        factor_watermark = int(manifest.factor_watermark or 0)
                        if (
                            factor_commit_seq > 0
                            or factor_watermark > factor_base_cutoff
                        ):
                            expected_factor = factor_watermark
                    except Exception as exc:  # noqa: BLE001
                        issues.append(
                            "factor base unavailable: "
                            f"{type(exc).__name__}: {exc}"
                        )

                health = delta.health_check(
                    expected_raw,
                    factor_watermark=expected_factor,
                    commit_seq=(raw_commit_seq if expected_raw else None),
                    factor_commit_seq=(
                        factor_commit_seq if expected_factor else None
                    ),
                )
                if not health["ok"]:
                    issues.extend(health["problems"])
        return {
            "dataset_id": manifest.dataset_id,
            "status": manifest.status,
            "symbol_count": manifest.symbol_count,
            "issues": issues,
            "valid": len(issues) == 0,
        }


def _virtual_manifest_for(view, source: str, adjustment: str, period: str):
    """Map a formal product role to its current overlay virtual manifest.

    overlay_v1 substitution applies ONLY to the formal product roles
    (internal/composite_none -> L2, internal/composite_tushare_factor_qfq ->
    L1). Anything else (legacy families, explicit tushare/none requests,
    factor datasets) falls through to the normal manifest search so old
    backtests keep their explicit dataset ids untouched.
    """
    if (period or "1d") != "1d":
        return None
    if source == "internal" and adjustment == "composite_none":
        return view.l2_virtual_manifest()
    if source == "tushare" and adjustment == "none":
        return view.raw_virtual_manifest()
    if source == "internal" and adjustment == "composite_tushare_factor_qfq":
        return view.l1_virtual_manifest()
    if source == "tushare" and adjustment == "adj_factor":
        return view.factor_virtual_manifest()
    return None


def _slice_arrays(
    arr: Dict[str, np.ndarray],
    start_date: Optional[int],
    end_date: Optional[int],
) -> Dict[str, np.ndarray]:
    if start_date is None and end_date is None:
        return arr
    d = arr["trade_date"]
    mask = np.ones(len(d), dtype=bool)
    if start_date is not None:
        mask &= d >= int(start_date)
    if end_date is not None:
        mask &= d <= int(end_date)
    return {k: v[mask] for k, v in arr.items()}
