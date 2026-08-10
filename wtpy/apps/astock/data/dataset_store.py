"""Content-addressed dataset storage with immutable manifests.

Directory layout:
  storage/astock/market_data/
  ├── blobs/{sha256}.npz
  ├── manifests/{dataset_id}.json
  ├── catalog.sqlite3
  └── sync_logs/
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

import numpy as np

from .io_util import atomic_write_json, atomic_write_text
from .providers.base import (
    AdjustmentMode,
    BarPeriod,
    DataSource,
    MarketBar,
    WeeklyBarMode,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_sync_run_id(source: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%S")
    uid = uuid.uuid4().hex[:8]
    return f"{source}_{ts}_{uid}"


def make_dataset_id(
    source: str,
    adjustment: str,
    period: str,
    cutoff_or_anchor: str,
    manifest_sha: str,
) -> str:
    return f"{source}_{adjustment}_{period}_{cutoff_or_anchor}_{manifest_sha[:12]}"


@dataclass
class SymbolRecord:
    symbol: str
    blob_sha256: str
    first_date: Optional[int] = None
    last_date: Optional[int] = None
    row_count: int = 0
    quality: str = "ok"
    error: str = ""
    # Incremental retention marker: "no_new_rows_parent_retained" when the
    # parent blob was kept because the window had no new rows.
    window_status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetManifest:
    dataset_id: str
    source: str
    adjustment: str
    period: str
    weekly_bar_mode: str = "local_aggregate"
    anchor_date: Optional[int] = None
    snapshot_date: Optional[int] = None
    data_cutoff_date: Optional[int] = None
    provider_version: str = ""
    sync_run_id: str = ""
    parent_dataset_id: Optional[str] = None
    manifest_sha256: str = ""
    symbol_count: int = 0
    row_count: int = 0
    status: str = "building"
    created_at: str = ""
    # ---- universe / survivorship metadata (Gate A) ----
    universe_type: str = ""
    universe_definition_version: str = ""
    survivorship_bias: Optional[bool] = None
    historical_universe_complete: Optional[bool] = None
    delisted_coverage_complete: Optional[bool] = None
    coverage_start_year: Optional[int] = None
    coverage_end_year: Optional[int] = None
    known_missing_delisted_count: Optional[int] = None
    known_missing_delisted_symbols: List[str] = field(default_factory=list)
    warning_text: str = ""
    recommended_use: List[str] = field(default_factory=list)
    prohibited_or_discouraged_use: List[str] = field(default_factory=list)
    # ---- coverage accounting (Gate A no_data/failed policy) ----
    expected_symbol_count: int = 0
    imported_symbol_count: int = 0
    excluded_symbol_count: int = 0
    no_data_symbol_count: int = 0
    failed_symbol_count: int = 0
    warning_symbol_count: int = 0
    coverage_ratio: Optional[float] = None
    no_data_allowlist: List[Dict] = field(default_factory=list)
    # ---- Gate C: dataset typing / lineage / derivation (all optional) ----
    dataset_type: str = "bars"  # bars | factor
    universe_file: str = ""
    universe_sha256: str = ""
    content_hash: str = ""
    provider_versions: Dict = field(default_factory=dict)
    provenance: Dict = field(default_factory=dict)
    token_exposed: Optional[bool] = None
    incremental_policy_version: str = ""
    # derived-dataset lineage (internal/tushare_factor_qfq)
    raw_dataset_id: str = ""
    raw_dataset_sha256: str = ""
    raw_source: str = ""
    factor_dataset_id: str = ""
    factor_dataset_sha256: str = ""
    factor_source: str = ""
    anchor_policy: str = ""
    formula_version: str = ""
    price_precision_policy: str = ""
    volume_policy: str = ""
    amount_policy: str = ""
    symbols: List[SymbolRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["symbols"] = [s.to_dict() for s in self.symbols]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetManifest":
        symbols = [SymbolRecord(**s) for s in d.pop("symbols", [])]
        return cls(symbols=symbols, **d)


def evaluate_strict_publish(
    records: Sequence[SymbolRecord],
    *,
    expected_symbol_count: int,
    excluded_symbol_count: int = 0,
    no_data_allowlist: Optional[Dict[str, str]] = None,
    max_allow_count: int = 0,
    max_allow_ratio: float = 0.0,
) -> Dict:
    """Strict ready policy for full imports (Gate A).

    ready requires: failed==0 AND every no_data symbol is explicitly
    allowlisted AND allowlisted count within max_allow_count/max_allow_ratio.
    Otherwise -> partial. Returns dict with target_status + accounting
    fields to store on the manifest.
    """
    allow = dict(no_data_allowlist or {})
    imported = [r for r in records if r.quality == "ok"]
    failed = [r for r in records if r.quality == "error"]
    no_data = [r for r in records if r.quality == "no_data"]
    allowed = [r for r in no_data if r.symbol in allow]
    unallowed = [r for r in no_data if r.symbol not in allow]

    limit = max(int(max_allow_count), int(max_allow_ratio * max(expected_symbol_count, 1)))
    within_limit = len(allowed) <= limit

    ok = (not failed) and (not unallowed) and within_limit
    reasons = []
    if failed:
        reasons.append(f"failed_symbols={len(failed)}")
    if unallowed:
        reasons.append(f"no_data_not_allowlisted={len(unallowed)}")
    if not within_limit:
        reasons.append(f"allowlisted_no_data={len(allowed)}>limit={limit}")

    return {
        "target_status": "ready" if ok else "partial",
        "block_reasons": reasons,
        "expected_symbol_count": int(expected_symbol_count),
        "imported_symbol_count": len(imported),
        "excluded_symbol_count": int(excluded_symbol_count),
        "no_data_symbol_count": len(no_data),
        "failed_symbol_count": len(failed),
        "warning_symbol_count": len(allowed),
        "coverage_ratio": round(len(imported) / max(expected_symbol_count, 1), 6),
        "no_data_allowlist": [
            {"symbol": r.symbol, "reason": allow.get(r.symbol, "")} for r in allowed
        ],
    }


class DatasetStore:
    """Manages content-addressed blobs and immutable dataset manifests."""

    # Manifests are immutable (append-only) files; cache parsed objects keyed
    # by their stat signature so repeated access (available checks, searches)
    # doesn't re-read + re-parse the full JSON on every call.
    _MANIFEST_CACHE = {}
    _MANIFEST_CACHE_LOCK = threading.Lock()

    # Blob set (one scandir of the whole blobs dir) is expensive on large
    # warehouses (~100k entries) and only changes during sync; cache it keyed
    # by the dir stat signature with a short TTL (see blob_sha_set).
    _BLOB_SET_CACHE = {}
    _BLOB_SET_LOCK = threading.Lock()
    _BLOB_SET_TTL = 60.0

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.manifests_dir = self.root / "manifests"
        self.sync_logs_dir = self.root / "sync_logs"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.sync_logs_dir.mkdir(parents=True, exist_ok=True)

    def store_bars(self, symbol: str, bars: Sequence[MarketBar]) -> str:
        """Store bars as NPZ blob, return sha256 of the blob content."""
        if not bars:
            return ""
        arrays = {
            "trade_date": np.array([b.trade_date for b in bars], dtype=np.int64),
            "open": np.array([b.open for b in bars], dtype=np.float64),
            "high": np.array([b.high for b in bars], dtype=np.float64),
            "low": np.array([b.low for b in bars], dtype=np.float64),
            "close": np.array([b.close for b in bars], dtype=np.float64),
            "volume": np.array([b.volume for b in bars], dtype=np.float64),
            "amount": np.array([b.amount for b in bars], dtype=np.float64),
        }
        import io

        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        data = buf.getvalue()
        digest = sha256_bytes(data)
        blob_path = self.blobs_dir / f"{digest}.npz"
        if not blob_path.exists():
            tmp_path = blob_path.with_suffix(".npz.tmp")
            tmp_path.write_bytes(data)
            tmp_path.replace(blob_path)
        return digest

    def load_bars(self, blob_sha256: str) -> Dict[str, np.ndarray]:
        """Load arrays from a blob by its sha256."""
        blob_path = self.blobs_dir / f"{blob_sha256}.npz"
        if not blob_path.exists():
            raise FileNotFoundError(f"Blob not found: {blob_sha256}")
        data = np.load(blob_path)
        return {k: data[k] for k in data.files}

    def store_bar_arrays(self, symbol: str, arrays: Dict[str, "np.ndarray"]) -> str:
        """Store OHLCV arrays directly (same blob layout as store_bars).

        Used by derivation pipelines that operate on numpy arrays and would
        otherwise pay the cost of materializing millions of MarketBar objects.
        """
        required = ("trade_date", "open", "high", "low", "close", "volume", "amount")
        for k in required:
            if k not in arrays:
                raise ValueError(f"missing array: {k}")
        import io

        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            trade_date=np.asarray(arrays["trade_date"], dtype=np.int64),
            open=np.asarray(arrays["open"], dtype=np.float64),
            high=np.asarray(arrays["high"], dtype=np.float64),
            low=np.asarray(arrays["low"], dtype=np.float64),
            close=np.asarray(arrays["close"], dtype=np.float64),
            volume=np.asarray(arrays["volume"], dtype=np.float64),
            amount=np.asarray(arrays["amount"], dtype=np.float64),
        )
        data = buf.getvalue()
        digest = sha256_bytes(data)
        blob_path = self.blobs_dir / f"{digest}.npz"
        if not blob_path.exists():
            tmp_path = blob_path.with_suffix(".npz.tmp")
            tmp_path.write_bytes(data)
            tmp_path.replace(blob_path)
        return digest

    def store_factors(self, symbol: str, dates, factors) -> str:
        """Store an adjustment-factor series as an NPZ blob (content-addressed).

        Enforced invariants: strictly ascending unique trade_date, adj_factor > 0.
        Raises ValueError on violation — factor data must be cleaned by the
        sync layer before storage.
        """
        d = np.asarray(dates, dtype=np.int64)
        f = np.asarray(factors, dtype=np.float64)
        if len(d) == 0 or len(d) != len(f):
            raise ValueError("empty or mismatched factor series")
        if not np.all(np.diff(d) > 0):
            raise ValueError("factor trade_date must be strictly ascending/unique")
        if not np.all(f > 0):
            raise ValueError("adj_factor must be > 0")
        import io

        buf = io.BytesIO()
        np.savez_compressed(buf, trade_date=d, adj_factor=f)
        data = buf.getvalue()
        digest = sha256_bytes(data)
        blob_path = self.blobs_dir / f"{digest}.npz"
        if not blob_path.exists():
            tmp_path = blob_path.with_suffix(".npz.tmp")
            tmp_path.write_bytes(data)
            tmp_path.replace(blob_path)
        return digest

    def blob_exists(self, blob_sha256: str) -> bool:
        return (self.blobs_dir / f"{blob_sha256}.npz").exists()

    def blob_sha_set(self) -> Set[str]:
        """All blob shas present in blobs/ as a set (one scandir per scan).

        Read-only bulk integrity check: replaces per-symbol
        ``blob_exists`` stat() calls (one filesystem hit per symbol) with a
        single directory walk + in-memory set lookups. Cached 60s keyed by
        the blob-dir ``(mtime_ns, size)`` signature (entry changes update
        the dir mtime), thread-safe. Missing dir -> empty set; scandir
        failure -> empty set and the failure is never cached.
        """
        try:
            st = self.blobs_dir.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        cache_key = str(self.root)
        now = time.time()
        with self._BLOB_SET_LOCK:
            hit = self._BLOB_SET_CACHE.get(cache_key)
            if (
                hit is not None
                and hit[0] == sig
                and now - hit[2] < self._BLOB_SET_TTL
            ):
                return hit[1]
        shas: Set[str] = set()
        try:
            with os.scandir(self.blobs_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if name.endswith(".npz"):
                        shas.add(name[:-4])
        except OSError:
            return shas
        with self._BLOB_SET_LOCK:
            self._BLOB_SET_CACHE[cache_key] = (sig, shas, now)
        return shas

    def save_manifest(self, manifest: DatasetManifest) -> Path:
        """Atomically write manifest JSON. Returns the manifest path."""
        payload = manifest.to_dict()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        manifest.manifest_sha256 = sha256_text(canonical)
        payload["manifest_sha256"] = manifest.manifest_sha256
        path = self.manifests_dir / f"{manifest.dataset_id}.json"
        atomic_write_json(path, payload)
        return path

    def load_manifest(self, dataset_id: str, *, deep_copy: bool = True) -> Optional[DatasetManifest]:
        path = self.manifests_dir / f"{dataset_id}.json"
        if not path.exists():
            return None
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        cache_key = (str(self.root), dataset_id)
        with self._MANIFEST_CACHE_LOCK:
            cached = self._MANIFEST_CACHE.get(cache_key)
            if cached is not None and cached[0] == sig:
                if not deep_copy:
                    # Read-only fast path: hand out the shared cached object.
                    # Callers that mutate must use copy=True (the default).
                    return cached[1]
                # Manifests are mutable dataclasses; hand out a copy so a
                # caller that mutates a loaded manifest (e.g. demote_stale
                # setting status/provenance) can't leak into the shared cache.
                return copy.deepcopy(cached[1])
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = DatasetManifest.from_dict(data)
        if sig is not None:
            with self._MANIFEST_CACHE_LOCK:
                self._MANIFEST_CACHE[cache_key] = (sig, manifest)
        if not deep_copy:
            return manifest
        return copy.deepcopy(manifest)

    def list_manifests(self) -> List[str]:
        return sorted(p.stem for p in self.manifests_dir.glob("*.json"))

    def save_sync_log(self, sync_run_id: str, log: dict) -> Path:
        path = self.sync_logs_dir / f"{sync_run_id}.json"
        atomic_write_json(path, log)
        return path

    def publish(
        self,
        manifest: DatasetManifest,
        *,
        integrity_check: bool = True,
    ) -> DatasetManifest:
        """Atomically transition a dataset from building → ready.

        Validates all referenced blobs exist before publishing.
        Raises ValueError if integrity check fails.
        If manifest.status is already 'partial', it is preserved (interruption marker).
        """
        if integrity_check:
            missing = []
            for sym in manifest.symbols:
                if sym.blob_sha256 and not self.blob_exists(sym.blob_sha256):
                    missing.append(sym.symbol)
            if missing:
                manifest.status = "failed"
                self.save_manifest(manifest)
                raise ValueError(
                    f"Integrity check failed: {len(missing)} blobs missing"
                )

        if manifest.status == "partial":
            pass
        else:
            has_real_errors = any(
                s.quality == "error" and s.error and s.error != "empty"
                for s in manifest.symbols
            )
            if has_real_errors:
                manifest.status = "partial"
            else:
                manifest.status = "ready"

        self.save_manifest(manifest)
        return manifest
