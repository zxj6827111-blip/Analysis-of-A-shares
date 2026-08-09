"""Tushare-only product dataset coordinator (migration plan ``tushare_only_v1``).

Formal product roles (single source of truth for the default selection chain):

  L2 (execution plane) : internal/composite_none
  L1 (signal plane)    : internal/composite_tushare_factor_qfq

Chain:

  latest complete tushare/none
      + Tushare delisted missing complement
          -> internal/composite_none              (formal L2)
      x latest ready tushare/adj_factor
          -> internal/composite_tushare_factor_qfq (formal L1)

This module owns the coordination so CLI scripts, API routes and baseline
resolution do not each reimplement product selection. Everything is local and
idempotent: parents are content-addressed and never modified; the composite /
derived products are deterministic from their parents, so a reconcile with
unchanged parents is a no-op (``up_to_date``).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .composite_dataset import build_composite_none
from .dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
    make_sync_run_id,
)

# ---------------------------------------------------------------------------
# Policy identifiers (shared by composite + derived manifests + resolvers)
# ---------------------------------------------------------------------------

DATA_POLICY_TUSHARE_ONLY = "tushare_only_v1"
SURVIVORSHIP_POLICY = "listed_plus_delisted"
SUPPLEMENT_RULE = "missing_symbols_only"
QUALITY_STATUS_PASSED = "passed"

# Formal roles
L2_SOURCE = "internal"
L2_ADJUSTMENT = "composite_none"
L1_SOURCE = "internal"
L1_ADJUSTMENT = "composite_tushare_factor_qfq"

# Parent families
BASE_SOURCE = "tushare"
BASE_ADJUSTMENT = "none"
FACTOR_SOURCE = "tushare"
FACTOR_ADJUSTMENT = "adj_factor"

# Delisted-only tushare/none datasets are tagged with this universe_type
# (published by scripts/sync_tushare_delisted.py, Gate B2).
DELISTED_UNIVERSE_TYPE = "b1_delisted_supplement"

# Complement dataset identity
COMPLEMENT_SOURCE = "internal"
COMPLEMENT_ADJUSTMENT = "delisted_complement"

# Full-history base gates (orphan-window guard). A "complete" tushare/none
# base must look like a full-market history, not a short incremental window
# or a truncated 6000-row pull: median per-symbol rows >= MIN_MEDIAN_ROWS and
# a symbol pool that spans a real temporal range.
MIN_BASE_SYMBOL_COUNT = 100
MIN_BASE_MEDIAN_ROWS = 250
MIN_ORPHAN_MEDIAN_ROWS = 120
MIN_ORPHAN_TEMPORAL_SPAN_DAYS = 60

# Factor per-symbol freshness gate (reconcile side). The formal L1/L2 publish
# recomputes freshness against the CURRENT raw base instead of trusting the
# factor manifest's provenance (which may have been computed against an older
# raw surface). Active = base quality ok AND last_date within 5 calendar days
# of the base cutoff; fresh = factor last_date within 3 calendar days of the
# raw symbol's last date. Ratio below the minimum blocks the publish.
FACTOR_FRESH_RATIO_MIN = 0.95
FRESHNESS_RAW_TOLERANCE_DAYS = 5
FRESHNESS_FACTOR_TOLERANCE_DAYS = 3

# Regression gates (quality publish policy)
MAX_REGRESSION_RATIO = 0.5  # symbol_count / total_rows may not fall below this

# QFQ derivation (same math + ids as the legacy script constants)
QFQ_ANCHOR_POLICY = "last_factor_on_or_before_cutoff"
QFQ_PRICE_PRECISION_POLICY = "round_half_even_4dp_store; compare at 2dp"
COMPOSITE_QFQ_FORMULA_VERSION = "ctsfqfq_v1"
FACTOR_RESOLUTION_RULE_VERSION = (
    "factor_resolution_v1:exact_main>exact_supplement>bse_alias_main"
)
COMPLEMENT_RULE_VERSION = "delisted_missing_complement_v1"


@dataclass
class ProductPair:
    """A validated formal L1/L2 product pair (atomic selection unit)."""

    l2_dataset_id: str
    l1_dataset_id: str
    l2_manifest: DatasetManifest
    l1_manifest: DatasetManifest
    base_dataset_id: str = ""
    supplement_dataset_id: str = ""
    factor_dataset_id: str = ""
    data_policy: str = DATA_POLICY_TUSHARE_ONLY
    cutoff: int = 0

    @property
    def l2_max_date(self) -> int:
        return max(
            (int(r.last_date or 0) for r in self.l2_manifest.symbols if r.last_date),
            default=0,
        )

    @property
    def l1_max_date(self) -> int:
        return max(
            (int(r.last_date or 0) for r in self.l1_manifest.symbols if r.last_date),
            default=0,
        )


@dataclass
class ProductReconcileResult:
    """Structured outcome of ``reconcile_tushare_product_datasets``.

    status: up_to_date | published | waiting_for_parent | failed
    published: True only when a new L2 or L1 manifest was actually written.
    """

    status: str = "up_to_date"
    published: bool = False
    cutoff: int = 0
    base_dataset_id: str = ""
    supplement_dataset_id: str = ""
    factor_dataset_id: str = ""
    l2_dataset_id: str = ""
    l1_dataset_id: str = ""
    missing: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    checked_at: str = ""
    # Factor freshness metrics carried from the factor manifest's provenance
    # (per-symbol active coverage); informational only, does not change the
    # waiting_for_parent / failed decision logic.
    factor_freshness: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "published": self.published,
            "up_to_date": self.status == "up_to_date",
            "cutoff": self.cutoff,
            "base_dataset_id": self.base_dataset_id,
            "supplement_dataset_id": self.supplement_dataset_id,
            "factor_dataset_id": self.factor_dataset_id,
            "l2_dataset_id": self.l2_dataset_id,
            "l1_dataset_id": self.l1_dataset_id,
            "missing": self.missing,
            "issues": self.issues,
            "errors": self.errors,
            "checked_at": self.checked_at,
            "factor_freshness": self.factor_freshness,
        }


@dataclass
class HistorySignals:
    """Cheap manifest-level history statistics (no blob reads)."""

    symbol_count: int = 0
    total_rows: int = 0
    median_rows: float = 0.0
    p10_first_date: Optional[int] = None
    max_last_date: Optional[int] = None
    min_first_date: Optional[int] = None

    @property
    def is_short_window(self) -> bool:
        if self.symbol_count <= 0:
            return True
        if self.median_rows < MIN_ORPHAN_MEDIAN_ROWS:
            return True
        if (
            self.p10_first_date is not None
            and self.max_last_date is not None
            and int(self.max_last_date) - int(self.p10_first_date)
            < MIN_ORPHAN_TEMPORAL_SPAN_DAYS
        ):
            return True
        return False


def manifest_history_signals(m: DatasetManifest) -> HistorySignals:
    rows = [int(r.row_count or 0) for r in m.symbols if r.blob_sha256]
    firsts: List[int] = []
    lasts: List[int] = []
    for r in m.symbols:
        if not r.blob_sha256:
            continue
        d0, d1 = r.first_date, r.last_date
        if d0 is None or d1 is None:
            if d0 is not None:
                firsts.append(int(d0))
            if d1 is not None:
                lasts.append(int(d1))
            continue
        d0, d1 = int(d0), int(d1)
        # tolerate reversed record dates (defensive, mirrors session _scan)
        if d0 > d1:
            d0, d1 = d1, d0
        firsts.append(d0)
        lasts.append(d1)
    firsts.sort()
    med = float(np.median(rows)) if rows else 0.0
    p10 = firsts[max(0, int(len(firsts) * 0.1) - 1)] if firsts else None
    return HistorySignals(
        symbol_count=len(rows),
        total_rows=int(sum(rows)),
        median_rows=med,
        p10_first_date=p10,
        max_last_date=max(lasts) if lasts else None,
        min_first_date=firsts[0] if firsts else None,
    )


def is_orphan_window(m: DatasetManifest) -> bool:
    """True when the manifest looks like a short-window orphan dataset."""
    return manifest_history_signals(m).is_short_window


# ---------------------------------------------------------------------------
# Parent selection
# ---------------------------------------------------------------------------


def _is_delisted_supplement_dataset(m: DatasetManifest) -> bool:
    return (
        m.source == BASE_SOURCE
        and (m.adjustment or "") == BASE_ADJUSTMENT
        and (
            (m.universe_type or "") == DELISTED_UNIVERSE_TYPE
            or (m.provenance or {}).get("gate") == "B2"
            or (m.universe_type or "").startswith("b1_delisted")
        )
    )


def _is_tushare_raw_base_candidate(m: DatasetManifest) -> bool:
    if m.source != BASE_SOURCE or (m.adjustment or "") != BASE_ADJUSTMENT:
        return False
    if (m.period or "1d") != "1d":
        return False
    if m.status != "ready":
        return False
    if _is_delisted_supplement_dataset(m):
        return False
    sig = manifest_history_signals(m)
    if sig.symbol_count < MIN_BASE_SYMBOL_COUNT:
        return False
    if sig.median_rows < MIN_BASE_MEDIAN_ROWS:
        return False
    return True


def select_tushare_base(store: DatasetStore) -> Optional[DatasetManifest]:
    """Latest ready complete ``tushare/none`` full-market base.

    Orphan windows (16-row truncations, incremental windows without a parent
    history merge) are rejected outright; selection prefers the freshest
    cutoff, then the fullest symbol pool and the most rows.
    """
    candidates: List[DatasetManifest] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if not _is_tushare_raw_base_candidate(m):
            continue
        if any(r.blob_sha256 and not store.blob_exists(r.blob_sha256)
               for r in m.symbols):
            continue
        candidates.append(m)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
    )


def select_tushare_factor(store: DatasetStore) -> Optional[DatasetManifest]:
    """Latest ready tushare/adj_factor dataset (dataset_type=factor)."""
    candidates = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if m.source != FACTOR_SOURCE or (m.adjustment or "") != FACTOR_ADJUSTMENT:
            continue
        if (m.dataset_type or "") != "factor":
            continue
        if m.status != "ready":
            continue
        if any(r.blob_sha256 and not store.blob_exists(r.blob_sha256)
               for r in m.symbols):
            continue
        candidates.append(m)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
    )


def _select_latest_tushare_factor_candidate(
    store: DatasetStore,
) -> Optional[DatasetManifest]:
    """Latest tushare/adj_factor factor manifest regardless of status.

    Same ordering as :func:`select_tushare_factor` but WITHOUT the ready
    filter, so a freshly synced partial (e.g. demoted by the freshness gate)
    is seen as the authoritative "latest" candidate instead of being shadowed
    by an older ready factor. Blob integrity is still enforced.
    """
    candidates = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if m.source != FACTOR_SOURCE or (m.adjustment or "") != FACTOR_ADJUSTMENT:
            continue
        if (m.dataset_type or "") != "factor":
            continue
        if any(r.blob_sha256 and not store.blob_exists(r.blob_sha256)
               for r in m.symbols):
            continue
        candidates.append(m)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
    )


def _recompute_factor_freshness(
    store: DatasetStore,
    base: DatasetManifest,
    factor: DatasetManifest,
) -> Optional[dict]:
    """Recompute per-symbol factor freshness against the CURRENT raw base.

    Never trusts the factor manifest's provenance (it may have been computed
    against an older raw surface). Active = base symbol quality ok AND
    last_date >= base.data_cutoff_date - FRESHNESS_RAW_TOLERANCE_DAYS calendar
    days (suspended/delisted symbols are auto-exempt); fresh = the factor
    symbol's last_date >= the raw symbol's last_date -
    FRESHNESS_FACTOR_TOLERANCE_DAYS. Returns None when there are no active
    symbols. Output mirrors the sync script's _factor_freshness_metrics shape.
    """
    import datetime as _dt

    raw_cutoff = int(base.data_cutoff_date or 0)
    max_last_date = max(
        (int(r.last_date or 0) for r in base.symbols if r.last_date),
        default=0,
    )
    if not raw_cutoff or (max_last_date and raw_cutoff > max_last_date):
        # Manifest without an explicit cutoff (ad-hoc composite/test
        # surfaces), or a cutoff AHEAD of every symbol date (raw base
        # synced with --end-date=<today> on the first run after a long
        # holiday > FRESHNESS_RAW_TOLERANCE_DAYS): fall back to the real
        # max symbol date so the active set still computes instead of
        # silently passing. Equal/earlier cutoffs stay as-is.
        raw_cutoff = max_last_date
    if not raw_cutoff:
        return None
    try:
        cutoff_d = _dt.datetime.strptime(str(raw_cutoff), "%Y%m%d").date()
    except ValueError:
        return None
    tol_d = cutoff_d - _dt.timedelta(days=FRESHNESS_RAW_TOLERANCE_DAYS)

    factor_by_sym = {r.symbol: r for r in factor.symbols}
    active: List[tuple] = []  # (raw_last_date_obj, raw_last, fac_last, symbol)
    for r in base.symbols:
        if r.quality != "ok" or not r.blob_sha256:
            continue
        raw_last = int(r.last_date or 0)
        if not raw_last:
            continue
        try:
            rl_d = _dt.datetime.strptime(str(raw_last), "%Y%m%d").date()
        except ValueError:
            continue
        if rl_d < tol_d:
            continue  # suspended/delisted: auto-exempt
        fac = factor_by_sym.get(r.symbol)
        fac_last = int(fac.last_date or 0) if fac else 0
        active.append((rl_d, raw_last, fac_last, r.symbol))
    if not active:
        return None

    def _is_fresh(fac_last: int, raw_last_d) -> bool:
        if not fac_last:
            return False
        try:
            fl_d = _dt.datetime.strptime(str(fac_last), "%Y%m%d").date()
        except ValueError:
            return False
        return fl_d >= raw_last_d - _dt.timedelta(
            days=FRESHNESS_FACTOR_TOLERANCE_DAYS)

    fresh = sum(1 for rl_d, _, fac_last, _ in active if _is_fresh(fac_last, rl_d))
    stale = sorted(
        ({"symbol": sym, "factor_last_date": fac_last,
          "raw_last_date": raw_last}
         for rl_d, raw_last, fac_last, sym in active
         if not _is_fresh(fac_last, rl_d)),
        key=lambda s: int(s["raw_last_date"] or 0) - int(s["factor_last_date"] or 0),
        reverse=True,
    )
    factor_lasts = sorted(fac_last for _, _, fac_last, _ in active)
    n = len(factor_lasts)
    return {
        "fresh_symbol_ratio": round(fresh / len(active), 4),
        "fresh_count": fresh,
        "active_count": len(active),
        "stale_active_symbols": stale[:20],
        "p50_last_date": factor_lasts[n // 2] if n else 0,
        "p10_last_date": factor_lasts[max(0, int(n * 0.1))] if n else 0,
        "raw_dataset_id": base.dataset_id,
        "factor_dataset_id": factor.dataset_id,
        "fresh_tolerance_days": FRESHNESS_FACTOR_TOLERANCE_DAYS,
    }


def select_delisted_pool(store: DatasetStore) -> List[DatasetManifest]:
    """Ready tushare/none delisted-supplement datasets (newest first)."""
    out = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if not _is_delisted_supplement_dataset(m):
            continue
        if m.status != "ready":
            continue
        out.append(m)
    out.sort(
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            m.created_at or "",
        ),
        reverse=True,
    )
    return out


# ---------------------------------------------------------------------------
# Delisted missing complement
# ---------------------------------------------------------------------------


def _manifest_file_sha256(store: DatasetStore, dataset_id: str) -> str:
    return hashlib.sha256(
        (store.manifests_dir / f"{dataset_id}.json").read_bytes()
    ).hexdigest()


def build_delisted_missing_complement(
    store: DatasetStore,
    *,
    base: DatasetManifest,
    delisted_pool: Sequence[DatasetManifest],
    cutoff: int,
    dry_run: bool = False,
) -> DatasetManifest:
    """Publish internal/delisted_complement = delisted symbols - base symbols.

    Whole-symbol selection only: no intra-symbol date-range splicing. The
    overlap with base is recorded as ``excluded_overlap`` and never included —
    the strict overlap check in :func:`build_composite_none` stays in force.

    Blobs are referenced (content-addressed), never copied. The dataset id is
    deterministic from (parents, cutoff), so repeat calls are idempotent.
    """
    base_syms = {s.symbol for s in base.symbols if s.blob_sha256}
    pool_ids: List[str] = []
    included: Dict[str, SymbolRecord] = {}
    for m in delisted_pool:
        pool_ids.append(m.dataset_id)
        for s in m.symbols:
            if not s.blob_sha256:
                continue
            if not store.blob_exists(s.blob_sha256):
                continue
            included.setdefault(s.symbol, s)
    excluded_overlap = sorted(sym for sym in included if sym in base_syms)
    for sym in excluded_overlap:
        included.pop(sym, None)

    symbols = sorted(included)
    records = [
        SymbolRecord(
            symbol=sym,
            blob_sha256=included[sym].blob_sha256,
            first_date=included[sym].first_date,
            last_date=included[sym].last_date,
            row_count=included[sym].row_count,
            quality="ok",
        )
        for sym in symbols
    ]
    symbol_provenance = {sym: _record_pool_id(included[sym], delisted_pool) for sym in symbols}

    canonical_pre = json.dumps(
        {
            "source": COMPLEMENT_SOURCE,
            "adjustment": COMPLEMENT_ADJUSTMENT,
            "period": "1d",
            "cutoff": cutoff,
            "rule": COMPLEMENT_RULE_VERSION,
            "base": base.dataset_id,
            "parents": pool_ids,
        },
        sort_keys=True,
    )
    pre_sha = hashlib.sha256(canonical_pre.encode()).hexdigest()
    dataset_id = make_dataset_id(
        COMPLEMENT_SOURCE, COMPLEMENT_ADJUSTMENT, "1d", str(cutoff), pre_sha
    )

    existing = store.load_manifest(dataset_id)
    if existing is not None:
        return existing

    total_rows = int(sum(r.row_count for r in records))
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source=COMPLEMENT_SOURCE,
        adjustment=COMPLEMENT_ADJUSTMENT,
        period="1d",
        data_cutoff_date=cutoff,
        snapshot_date=int(time.strftime("%Y%m%d")),
        provider_version="delisted_complement",
        sync_run_id=f"delisted_complement_{time.strftime('%Y%m%dT%H%M%S')}",
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        universe_type="b1_delisted_complement",
        universe_definition_version="tushare_only_v1",
        survivorship_bias=False,
        historical_universe_complete=False,
        delisted_coverage_complete=True,
        warning_text=(
            "Delisted-symbol complement for the Tushare-only composite "
            "execution dataset (L2). Contains ONLY delisted symbols missing "
            "from the base tushare/none dataset."
        ),
        recommended_use=["internal/composite_none supplement (formal L2)"],
        prohibited_or_discouraged_use=[
            "standalone whole-market backtest",
            "signal plane without adjustment",
        ],
        parent_dataset_id=pool_ids[0] if pool_ids else "",
        provenance={
            "data_policy": DATA_POLICY_TUSHARE_ONLY,
            "survivorship_policy": SURVIVORSHIP_POLICY,
            "base_source": "tushare",
            "supplement_source": "tushare",
            "supplement_rule": SUPPLEMENT_RULE,
            "quality_status": QUALITY_STATUS_PASSED,
            "rule_version": COMPLEMENT_RULE_VERSION,
            "base_dataset_id": base.dataset_id,
            "base_manifest_sha256": _manifest_file_sha256(store, base.dataset_id),
            "parent_dataset_ids": pool_ids,
            "parent_manifest_sha256": {
                p: _manifest_file_sha256(store, p) for p in pool_ids
            },
            "excluded_overlap": excluded_overlap,
            "symbol_provenance": symbol_provenance,
        },
        token_exposed=False,
    )
    manifest.symbols = records
    manifest.symbol_count = len(records)
    manifest.row_count = total_rows
    manifest.expected_symbol_count = len(records)
    manifest.imported_symbol_count = len(records)
    manifest.coverage_ratio = 1.0

    if not dry_run:
        store.publish(manifest)
    return manifest


def _record_pool_id(rec: SymbolRecord, pool: Sequence[DatasetManifest]) -> str:
    for m in pool:
        for s in m.symbols:
            if s.symbol == rec.symbol:
                return m.dataset_id
    return ""


# ---------------------------------------------------------------------------
# QFQ derivation (moved here so CLI + reconcile share one implementation)
# ---------------------------------------------------------------------------


class _FactorCoverageView:
    """Lightweight factor-coverage view for freshness checks.

    :func:`_recompute_factor_freshness` only reads ``.symbols`` and
    ``.dataset_id``; the derive passes the merged main + supplement + alias
    resolution so the gate judges exactly the factor coverage the derive
    actually uses (a symbol covered only via supplement/alias must not look
    stale).
    """

    def __init__(self, dataset_id: str, symbols: List[SymbolRecord]):
        self.dataset_id = dataset_id
        self.symbols = symbols


def derive_composite_tushare_factor_qfq(
    store: DatasetStore,
    *,
    raw_dataset_id: str,
    factor_dataset_id: str,
    supplement_factor_dataset_id: Optional[str] = None,
    universe_dataset_id: Optional[str] = None,
    cutoff: Optional[int] = None,
    dry_run: bool = False,
    missing_factor_tolerance: float = 0.005,
) -> dict:
    """Derive internal/composite_tushare_factor_qfq from composite_none x factors.

    Per-symbol math is identical to the legacy tsqfq_v1 pipeline:
    anchor = last factor on or before cutoff; ratio(t) = factor_asof(t)/anchor;
    qfq = raw * ratio rounded half-even to 4dp; volume/amount copied.

    Returns a dict with keys: status ("success"|"failed"), error, dataset_id,
    dataset_status, result, issues — mirroring the legacy script return shape.
    A freshness-gate block (factor per-symbol coverage below
    FACTOR_FRESH_RATIO_MIN vs the raw surface this derive consumes) demotes
    the manifest to partial and is reported under ``freshness_gate``.
    """
    raw_m = store.load_manifest(raw_dataset_id)
    fac_m = store.load_manifest(factor_dataset_id)
    sup_m = (
        store.load_manifest(supplement_factor_dataset_id)
        if supplement_factor_dataset_id
        else None
    )
    if raw_m is None or fac_m is None or (supplement_factor_dataset_id and sup_m is None):
        return {"status": "failed", "error": "parent_manifest_not_found"}
    if raw_m.source != L2_SOURCE or raw_m.adjustment != L2_ADJUSTMENT:
        return {"status": "failed", "error": "raw_parent_wrong_source"}
    if raw_m.status != "ready":
        return {"status": "failed", "error": "parent_not_ready"}
    for label, m in (("factor", fac_m), ("supplement_factor", sup_m)):
        if m is None:
            continue
        if m.status != "ready":
            return {"status": "failed", "error": f"{label}_not_ready"}
        if (m.dataset_type or "") != "factor":
            return {"status": "failed", "error": f"{label}_not_a_factor_dataset"}

    alias_to_canon: Dict[str, str] = {}
    if universe_dataset_id:
        try:
            from .pit_universe import PointInTimeUniverse

            pit = PointInTimeUniverse.from_root(store.root, universe_dataset_id)
            for canon, w in pit.entries.items():
                for alias in w.aliases:
                    alias_to_canon[alias] = canon
        except Exception:
            alias_to_canon = {}

    # Data policy is derived from the REAL raw parent (the composite_none
    # L2), never hardcoded: a legacy mixed-vendor composite (build_composite_none
    # still allows base=local_vendor) must not be labelled tushare_only_v1.
    # The strict L1/L2 pair validation rejects any non-tushare_only_v1 L1
    # (fail-closed), so a legacy-derived L1 never becomes the formal product.
    raw_prov = raw_m.provenance or {}
    l1_data_policy = (
        DATA_POLICY_TUSHARE_ONLY
        if (
            raw_prov.get("data_policy") == DATA_POLICY_TUSHARE_ONLY
            or raw_prov.get("base_source") == "tushare"
        )
        else "legacy_mixed_vendor"
    )

    raw_ok = {r.symbol: r for r in raw_m.symbols if r.quality == "ok"}
    fac_ok = {r.symbol: r for r in fac_m.symbols if r.quality == "ok"}
    sup_ok = (
        {r.symbol: r for r in sup_m.symbols if r.quality == "ok"} if sup_m else {}
    )

    resolution: Dict[str, Tuple[str, SymbolRecord]] = {}
    missing: List[str] = []
    for sym in raw_ok:
        if sym in fac_ok:
            resolution[sym] = ("main", fac_ok[sym])
        elif sym in sup_ok:
            resolution[sym] = ("supplement", sup_ok[sym])
        elif sym in alias_to_canon and alias_to_canon[sym] in fac_ok:
            resolution[sym] = ("alias_main", fac_ok[alias_to_canon[sym]])
        elif sym in alias_to_canon and alias_to_canon[sym] in sup_ok:
            resolution[sym] = ("alias_supplement", sup_ok[alias_to_canon[sym]])
        else:
            missing.append(sym)

    lasts = [r.last_date for r in raw_ok.values() if r.last_date]
    eff_cutoff = int(cutoff or (max(lasts) if lasts else 0))
    if not eff_cutoff:
        return {"status": "failed", "error": "no_cutoff"}

    # Deterministic per parent set so a reconcile with unchanged parents is a
    # true no-op (idempotent product publication).
    sync_run_id = (
        f"ctsfqfq_{raw_dataset_id[:16]}_{factor_dataset_id[:16]}_"
        f"{(supplement_factor_dataset_id or 'none')[:16]}"
    )
    eligible = sorted(resolution)
    records: List[SymbolRecord] = []
    issues: List[dict] = []
    source_counts = {"main": 0, "supplement": 0, "alias_main": 0, "alias_supplement": 0}
    nonpos_qfq_rows = 0
    nan_inf_rows = 0
    total_rows = 0
    imported = 0
    for sym in sorted(missing):
        records.append(
            SymbolRecord(symbol=sym, blob_sha256="", quality="no_data", error="missing_factor")
        )
    for sym in eligible:
        rr = raw_ok[sym]
        fsrc, fr = resolution[sym]
        raw_arr = store.load_bars(rr.blob_sha256)
        fac_arr = store.load_bars(fr.blob_sha256)
        rd = raw_arr["trade_date"]
        fd = fac_arr["trade_date"]
        fv = fac_arr["adj_factor"]
        aidx = int(np.searchsorted(fd, eff_cutoff, side="right")) - 1
        if aidx < 0:
            records.append(
                SymbolRecord(symbol=sym, blob_sha256="", quality="error", error="no_anchor_factor")
            )
            issues.append({"symbol": sym, "issue": "no_anchor_factor"})
            continue
        anchor = float(fv[aidx])
        pos = np.searchsorted(fd, rd, side="right") - 1
        valid = pos >= 0
        leading_gap = int(np.sum(~valid))
        if leading_gap:
            issues.append(
                {"symbol": sym, "issue": "leading_gap_rows_dropped", "detail": leading_gap}
            )
        rdv = rd[valid]
        if len(rdv) == 0:
            records.append(
                SymbolRecord(symbol=sym, blob_sha256="", quality="no_data", error="all_rows_leading_gap")
            )
            continue
        ratio = fv[pos[valid]] / anchor
        arrays = {
            "trade_date": rdv,
            "open": np.round(raw_arr["open"][valid] * ratio, 4),
            "high": np.round(raw_arr["high"][valid] * ratio, 4),
            "low": np.round(raw_arr["low"][valid] * ratio, 4),
            "close": np.round(raw_arr["close"][valid] * ratio, 4),
            "volume": raw_arr["volume"][valid],
            "amount": raw_arr["amount"][valid],
        }
        o, h, l, c = arrays["open"], arrays["high"], arrays["low"], arrays["close"]
        _finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
        _nbad_fin = int(np.sum(~_finite))
        if _nbad_fin:
            nan_inf_rows += _nbad_fin
            issues.append({"symbol": sym, "issue": "nan_inf_rows", "detail": _nbad_fin})
        _npos = int(np.sum(c <= 0))
        if _npos:
            nonpos_qfq_rows += _npos
            issues.append({"symbol": sym, "issue": "non_positive_qfq_close", "detail": _npos})
        bad = int(np.sum((h < l) | (o > h) | (o < l) | (c > h) | (c < l)))
        if bad:
            issues.append({"symbol": sym, "issue": "ohlc_bounds_inherited_from_raw", "detail": bad})
        sha = store.store_bar_arrays(sym, arrays)
        total_rows += len(rdv)
        imported += 1
        source_counts[fsrc] += 1
        records.append(
            SymbolRecord(
                symbol=sym, blob_sha256=sha, first_date=int(rdv[0]),
                last_date=int(rdv[-1]), row_count=len(rdv), quality="ok",
            )
        )

    failed = [r for r in records if r.quality == "error"]
    no_data_cnt = sum(1 for r in records if r.quality == "no_data")
    content_hash = hashlib.sha256(
        json.dumps(
            sorted((r.symbol, r.blob_sha256) for r in records if r.blob_sha256)
        ).encode()
    ).hexdigest()
    canonical_pre = json.dumps(
        {
            "source": "internal",
            "adjustment": L1_ADJUSTMENT,
            "period": "1d",
            "raw": raw_dataset_id,
            "factor": factor_dataset_id,
            "supplement_factor": supplement_factor_dataset_id or "",
            "universe": universe_dataset_id or "",
            "cutoff": eff_cutoff,
            "formula": COMPOSITE_QFQ_FORMULA_VERSION,
            "factor_resolution": FACTOR_RESOLUTION_RULE_VERSION,
            "sync_run_id": sync_run_id,
        },
        sort_keys=True,
    )
    dataset_id = make_dataset_id(
        "internal", L1_ADJUSTMENT, "1d", str(eff_cutoff),
        hashlib.sha256(canonical_pre.encode()).hexdigest(),
    )

    existing = store.load_manifest(dataset_id)
    if existing is not None:
        # Deterministic id: same parents + rule => already published
        # (idempotent). The on-disk status is authoritative; the recorded
        # freshness gate (when present) tells callers why a partial L1 is
        # partial without re-running the gate. Stats are reconstructed from
        # the manifest's symbol records so the CLI / sync log report the
        # real data volume instead of a zeroed summary.
        _existing_gate = (existing.provenance or {}).get("freshness") or {}
        _existing_prov = existing.provenance or {}
        _imported = sum(1 for r in existing.symbols if r.quality == "ok")
        _failed = sum(1 for r in existing.symbols if r.quality == "error")
        _no_data_recs = [r for r in existing.symbols if r.quality == "no_data"]
        _missing_factor = sum(
            1 for r in _no_data_recs if r.error == "missing_factor")
        _eligible = _imported + _failed + (len(_no_data_recs) - _missing_factor)
        _rows = int(sum(
            int(r.row_count or 0)
            for r in existing.symbols if r.quality == "ok"
        ))
        _source_counts = _existing_prov.get("factor_source_counts") or {
            "main": 0, "supplement": 0, "alias_main": 0, "alias_supplement": 0,
        }
        _missing_syms = _existing_prov.get("missing_factor_symbols") or sorted(
            r.symbol for r in _no_data_recs if r.error == "missing_factor"
        )
        return {
            "status": "success",
            "dataset_id": dataset_id,
            "dataset_status": existing.status,
            "result": {
                "eligible": _eligible, "imported": _imported,
                "missing_factor": _missing_factor, "failed": _failed,
                "rows": _rows, "status": existing.status,
                "factor_source_counts": _source_counts,
                "missing_factor_symbols": sorted(_missing_syms),
            },
            "issues": issues,
            "freshness_gate": _existing_gate,
            "idempotent": True,
        }

    alias_syms = sorted(s for s, (src, _) in resolution.items() if src in ("alias_main", "alias_supplement"))
    supplement_syms = sorted(s for s, (src, _) in resolution.items() if src == "supplement")
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source=L1_SOURCE,
        adjustment=L1_ADJUSTMENT,
        period="1d",
        dataset_type="bars",
        snapshot_date=int(time.strftime("%Y%m%d")),
        data_cutoff_date=eff_cutoff,
        provider_version=f"derive_{COMPOSITE_QFQ_FORMULA_VERSION}",
        sync_run_id=sync_run_id,
        parent_dataset_id=raw_dataset_id,
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        raw_dataset_id=raw_dataset_id,
        raw_dataset_sha256=_manifest_file_sha256(store, raw_dataset_id),
        raw_source="internal_composite",
        factor_dataset_id=factor_dataset_id,
        factor_dataset_sha256=_manifest_file_sha256(store, factor_dataset_id),
        factor_source="tushare",
        anchor_policy=QFQ_ANCHOR_POLICY,
        formula_version=COMPOSITE_QFQ_FORMULA_VERSION,
        price_precision_policy=QFQ_PRICE_PRECISION_POLICY,
        volume_policy="copied_from_raw_shares_no_adjustment",
        amount_policy="copied_from_raw_cny_no_adjustment",
        universe_file=raw_m.universe_file,
        universe_sha256=raw_m.universe_sha256,
        content_hash=content_hash,
        survivorship_bias=False,
        historical_universe_complete=True,
        delisted_coverage_complete=True,
        coverage_start_year=raw_m.coverage_start_year,
        coverage_end_year=raw_m.coverage_end_year,
        warning_text=(
            "Derived composite QFQ (NOT Tushare-native qfq). Signal use only. "
            "Tushare-only lineage (composite_none x tushare adj_factor)."
        ),
        recommended_use=["L1 signal computation (front-adjusted, survivorship-safe)"],
        prohibited_or_discouraged_use=[
            "L2 execution prices",
            "limit up/down checks",
            "claiming Tushare-native qfq data",
        ],
        provenance={
            "data_policy": l1_data_policy,
            "survivorship_policy": SURVIVORSHIP_POLICY,
            "base_source": raw_prov.get("base_source") or "tushare",
            "supplement_source": raw_prov.get("supplement_source") or "tushare",
            "supplement_rule": SUPPLEMENT_RULE,
            "quality_status": QUALITY_STATUS_PASSED,
            "derivation": "composite_none raw OHLC x (adj_factor_asof/anchor)",
            "anchor_policy": QFQ_ANCHOR_POLICY,
            "formula_version": COMPOSITE_QFQ_FORMULA_VERSION,
            "factor_resolution_rule": FACTOR_RESOLUTION_RULE_VERSION,
            "supplement_factor_dataset_id": supplement_factor_dataset_id or "",
            "supplement_factor_dataset_sha256": (
                _manifest_file_sha256(store, supplement_factor_dataset_id)
                if supplement_factor_dataset_id
                else ""
            ),
            "universe_dataset_id": universe_dataset_id or "",
            "factor_source_counts": source_counts,
            "alias_factor_symbols": alias_syms,
            "supplement_factor_symbols": supplement_syms,
            "missing_factor_symbols": sorted(missing),
            "leading_gap_policy": "rows before first factor date are dropped and recorded",
        },
        expected_symbol_count=len(raw_ok),
    )
    manifest.symbols = records
    manifest.symbol_count = len(records)
    manifest.row_count = total_rows
    manifest.imported_symbol_count = imported
    manifest.excluded_symbol_count = len(missing)
    manifest.no_data_symbol_count = no_data_cnt
    manifest.failed_symbol_count = len(failed)
    manifest.coverage_ratio = round(imported / max(len(raw_ok), 1), 6)
    # A tiny missing-factor ratio (e.g. brand-new IPO symbols) is recorded
    # per-symbol as no_data but does not block the formal product. Large gaps
    # (e.g. a missing delisted factor supplement) keep the strict partial
    # status so the reconcile reports waiting_for_parent instead.
    missing_ratio = len(missing) / max(len(raw_ok), 1)
    if failed or (no_data_cnt and missing_ratio > missing_factor_tolerance):
        manifest.status = "partial"
    elif manifest.status == "building":
        manifest.status = "ready"

    # ---- freshness gate (derive path, fail-closed) ----
    # The L1 must never publish ready when the factor coverage this derive
    # actually uses (main + supplement + alias resolution) lags the raw
    # surface this derive consumes — otherwise an OLD ready factor selected
    # by the derive parent auto-resolve would push a残缺 L1 into the formal
    # product plane. Reuses the reconcile-side per-symbol recompute (never
    # trusts factor manifest provenance). A blocked gate demotes the
    # manifest to partial (still recorded on disk with the deterministic id,
    # but never selectable as the formal L1) and is reported in the result.
    merged_factor: Dict[str, SymbolRecord] = {}
    for sym, (_src, fr) in resolution.items():
        if fr is not None:
            merged_factor[sym] = SymbolRecord(
                symbol=sym, blob_sha256=fr.blob_sha256,
                first_date=fr.first_date, last_date=fr.last_date,
                row_count=fr.row_count, quality=fr.quality,
            )
    _fresh = _recompute_factor_freshness(
        store, raw_m, _FactorCoverageView(fac_m.dataset_id, list(merged_factor.values()))
    )
    if _fresh is None or _fresh["fresh_symbol_ratio"] < FACTOR_FRESH_RATIO_MIN:
        manifest.status = "partial"
        _gate: Dict[str, Any] = {
            "status": "blocked",
            "reason": "freshness_below_threshold",
            "fresh_symbol_ratio": _fresh["fresh_symbol_ratio"] if _fresh else None,
            "fresh_count": _fresh["fresh_count"] if _fresh else None,
            "active_count": _fresh["active_count"] if _fresh else None,
            "stale_active_symbols": (
                _fresh["stale_active_symbols"] if _fresh else []
            ),
            "factor_dataset_id": fac_m.dataset_id,
            "raw_dataset_id": raw_m.dataset_id,
            "min_ratio": FACTOR_FRESH_RATIO_MIN,
        }
        print(
            f"  DERIVE FRESHNESS GATE: blocked (fresh="
            f"{_gate['fresh_count'] if _fresh else 0}/"
            f"{_gate['active_count'] if _fresh else 0}, "
            f"ratio={_gate['fresh_symbol_ratio']} < "
            f"{FACTOR_FRESH_RATIO_MIN}) -> L1 partial, not ready"
        )
        for _s in (_gate["stale_active_symbols"] or [])[:8]:
            print(f"    stale {_s['symbol']}: factor="
                  f"{_s['factor_last_date']} raw={_s['raw_last_date']}")
    else:
        _gate = {
            "status": "passed",
            "fresh_symbol_ratio": _fresh["fresh_symbol_ratio"],
            "fresh_count": _fresh["fresh_count"],
            "active_count": _fresh["active_count"],
        }
        print(
            f"  DERIVE FRESHNESS GATE: fresh="
            f"{_fresh['fresh_count']}/{_fresh['active_count']} "
            f"(ratio={_fresh['fresh_symbol_ratio']})"
        )

    # Record the gate on the manifest so the reconcile can distinguish a
    # freshness-blocked partial L1 (waiting for fresher factors) from a plain
    # provider-failure partial without re-running the per-symbol math.
    manifest.provenance = dict(manifest.provenance or {})
    manifest.provenance["freshness"] = _gate

    def _result() -> dict:
        return {
            "eligible": len(eligible), "imported": imported,
            "missing_factor": len(missing), "failed": len(failed),
            "rows": total_rows, "status": manifest.status,
            "factor_source_counts": source_counts,
            "missing_factor_symbols": sorted(missing),
        }

    if dry_run:
        return {
            "status": "success",
            "dataset_id": dataset_id,
            "dataset_status": manifest.status,
            "result": _result(),
            "issues": issues,
            "freshness_gate": _gate,
            "dry_run": True,
        }

    store.publish(manifest)
    return {
        "status": "success",
        "dataset_id": dataset_id,
        "dataset_status": manifest.status,
        "result": _result(),
        "issues": issues,
        "freshness_gate": _gate,
    }


def select_supplement_factor(
    store: DatasetStore, main_factor: DatasetManifest
) -> Optional[DatasetManifest]:
    """Ready tushare factor dataset other than the main one (delisted factors).

    Any ready factor set whose symbols extend the main factor coverage is a
    valid supplement (delisted stocks only carry factor rows in the B2
    supplement factor pipeline).
    """
    main_syms = {s.symbol for s in main_factor.symbols if s.quality == "ok"}
    best: Optional[DatasetManifest] = None
    best_score = (-1, -1, "")
    for mid in store.list_manifests():
        m = store.load_manifest(mid)
        if m is None:
            continue
        if m.source != FACTOR_SOURCE or (m.adjustment or "") != FACTOR_ADJUSTMENT:
            continue
        if (m.dataset_type or "") != "factor":
            continue
        if m.status != "ready":
            continue
        if m.dataset_id == main_factor.dataset_id:
            continue
        extra = sum(1 for s in m.symbols if s.quality == "ok" and s.symbol not in main_syms)
        if extra <= 0:
            continue
        score = (extra, int(m.data_cutoff_date or 0), m.created_at or "")
        if score > best_score:
            best_score, best = score, m
    return best


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------


def _product_pair_from_manifests(
    l1: DatasetManifest, l2: DatasetManifest
) -> ProductPair:
    l1_prov = l1.provenance or {}
    l2_prov = l2.provenance or {}
    parents = l2_prov.get("parents") or []
    base_id = ""
    supp_id = ""
    for p in parents:
        if p.get("role") == "base":
            base_id = str(p.get("dataset_id") or "")
        elif p.get("role") == "supplement":
            supp_id = str(p.get("dataset_id") or "")
    return ProductPair(
        l2_dataset_id=l2.dataset_id,
        l1_dataset_id=l1.dataset_id,
        l2_manifest=l2,
        l1_manifest=l1,
        base_dataset_id=str(l1_prov.get("base_dataset_id") or base_id),
        supplement_dataset_id=str(l1_prov.get("supplement_dataset_id") or supp_id),
        factor_dataset_id=str(l1_prov.get("factor_dataset_id") or l1.factor_dataset_id or ""),
        data_policy=str(l1_prov.get("data_policy") or DATA_POLICY_TUSHARE_ONLY),
        cutoff=int(l1.data_cutoff_date or 0),
    )


def _l2_parent_source(m: DatasetManifest) -> str:
    """Effective vendor source an L2 parent contributes.

    A delisted complement manifest is internal/delisted_complement but its
    bars are Tushare data; the composite derive records the real source in
    the complement provenance.
    """
    if m.source == "internal" and m.adjustment == COMPLEMENT_ADJUSTMENT:
        prov = m.provenance or {}
        return str(
            prov.get("supplement_source") or prov.get("base_source") or m.source
        )
    return str(m.source or "")


def _validate_pair_core(
    store: DatasetStore,
    l1: Optional[DatasetManifest],
    l2: Optional[DatasetManifest],
    *,
    deep_copy: bool = True,
) -> Tuple[bool, List[str]]:
    """Non-recursive L1/L2 pair validation shared by validate + resolver.

    Checks L1/L2 roles (source/adjustment), tushare_only_v1 data policy,
    the L1 raw/factor parents (must exist, be ready and carry the formal
    roles), L1.raw_dataset_id == L2.dataset_id, the L1 factor source and
    the L2 provenance parents (exactly base + supplement with positional
    role labels and unique ids, tushare lineage, disjoint). Never calls resolve_active_tushare_product_pair or
    validate_tushare_product_pair (the resolver path must stay recursion-
    free). The issues text matches validate_tushare_product_pair's output.
    """
    issues: List[str] = []
    if l1 is not None:
        if l1.status != "ready":
            issues.append(f"L1 not ready: {l1.status}")
        if l1.source != L1_SOURCE or l1.adjustment != L1_ADJUSTMENT:
            issues.append(f"L1 role mismatch: {l1.source}/{l1.adjustment}")
        prov = l1.provenance or {}
        if prov.get("data_policy") != DATA_POLICY_TUSHARE_ONLY:
            issues.append("L1 is not a tushare_only_v1 product dataset")
        if (l1.factor_source or "") not in ("", "tushare"):
            issues.append(f"L1 factor_source is not tushare: {l1.factor_source}")
        # factor parent must exist and be a ready tushare/adj_factor dataset
        factor_parent_id = str(
            prov.get("factor_dataset_id") or l1.factor_dataset_id or ""
        )
        if not factor_parent_id:
            issues.append("L1 factor parent missing")
        else:
            fac_m = store.load_manifest(factor_parent_id, deep_copy=deep_copy)
            if fac_m is None:
                issues.append(f"L1 factor manifest missing: {factor_parent_id}")
            else:
                if fac_m.status != "ready":
                    issues.append(f"L1 factor not ready: {fac_m.status}")
                if (fac_m.source or "") != FACTOR_SOURCE or (
                    fac_m.adjustment or ""
                ) != FACTOR_ADJUSTMENT:
                    issues.append(
                        f"L1 factor source mismatch: {fac_m.source}/{fac_m.adjustment}"
                    )
        # raw parent must exist, be ready and match the formal L2
        raw_parent = l1.raw_dataset_id or ""
        if not raw_parent:
            issues.append("L1 raw parent missing")
        else:
            raw_m = store.load_manifest(raw_parent, deep_copy=deep_copy)
            if raw_m is None:
                issues.append(f"L1 raw parent manifest missing: {raw_parent}")
            else:
                if raw_m.status != "ready":
                    issues.append(f"L1 raw parent not ready: {raw_m.status}")
                if raw_m.source != L2_SOURCE or raw_m.adjustment != L2_ADJUSTMENT:
                    issues.append(
                        f"L1 raw parent role mismatch: {raw_m.source}/{raw_m.adjustment}"
                    )
            if l2 is not None and raw_parent != l2.dataset_id:
                issues.append(
                    f"L1 raw parent {raw_parent} != formal L2 {l2.dataset_id}"
                )

    if l2 is not None:
        if l2.status != "ready":
            issues.append(f"L2 not ready: {l2.status}")
        if l2.source != L2_SOURCE or l2.adjustment != L2_ADJUSTMENT:
            issues.append(f"L2 role mismatch: {l2.source}/{l2.adjustment}")
        l2_prov = l2.provenance or {}
        if l2_prov.get("data_policy") != DATA_POLICY_TUSHARE_ONLY:
            issues.append("L2 is not a tushare_only_v1 product dataset")
        # base / supplement must both come from tushare
        for role in ("base", "supplement"):
            src = l2_prov.get(f"{role}_source")
            if not src:
                issues.append(f"L2 {role}_source missing")
            elif str(src) != "tushare":
                issues.append(f"L2 {role}_source is not tushare: {src}")
        # lineage parents: exactly two (base + supplement) whose role labels
        # must match their positions (parents[0] base, parents[1]
        # supplement — the order the composite derive writes) and whose
        # dataset ids must be non-empty and distinct; both manifests must
        # exist with the roles the composite derive writes
        parents = l2_prov.get("parents")
        if not isinstance(parents, list) or len(parents) != 2:
            issues.append("L2 parents missing or malformed")
        else:
            parent_ids = ["", ""]
            for i, expected in ((0, "base"), (1, "supplement")):
                p = parents[i]
                if not isinstance(p, dict):
                    issues.append("L2 parents role missing")
                    continue
                role = p.get("role")
                if role is None or str(role) == "":
                    issues.append("L2 parents role missing")
                elif str(role) != expected:
                    issues.append(f"L2 parents[{i}] role mismatch: {role}")
                parent_ids[i] = str(p.get("dataset_id") or "")
            if parent_ids[0] and parent_ids[1] and parent_ids[0] == parent_ids[1]:
                issues.append(
                    f"L2 base and supplement parents are identical: "
                    f"{parent_ids[0]}"
                )
            base_m = store.load_manifest(parent_ids[0]) if parent_ids[0] else None
            supp_m = store.load_manifest(parent_ids[1]) if parent_ids[1] else None
            if base_m is None:
                issues.append(f"L2 base manifest missing: {parent_ids[0]}")
            if supp_m is None:
                issues.append(f"L2 supplement manifest missing: {parent_ids[1]}")
            if base_m is not None and supp_m is not None:
                if base_m.status != "ready":
                    issues.append(f"L2 base not ready: {base_m.status}")
                if _l2_parent_source(base_m) != BASE_SOURCE or (
                    base_m.adjustment or ""
                ) != BASE_ADJUSTMENT:
                    issues.append(
                        f"L2 base role mismatch: {base_m.source}/{base_m.adjustment}"
                    )
                if supp_m.status != "ready":
                    issues.append(f"L2 supplement not ready: {supp_m.status}")
                if _l2_parent_source(supp_m) != BASE_SOURCE or (
                    (supp_m.adjustment or "")
                    not in (BASE_ADJUSTMENT, COMPLEMENT_ADJUSTMENT)
                ):
                    issues.append(
                        f"L2 supplement role mismatch: "
                        f"{supp_m.source}/{supp_m.adjustment}"
                    )
                # supplement must be disjoint from base (strict rule)
                base_syms = {s.symbol for s in base_m.symbols if s.blob_sha256}
                supp_syms = {s.symbol for s in supp_m.symbols if s.blob_sha256}
                overlap = sorted(base_syms & supp_syms)
                if overlap:
                    issues.append(f"L2 base/supplement overlap: {overlap[:5]}")
    return (not issues, issues)


def validate_tushare_product_pair(
    store: DatasetStore,
    *,
    l1_dataset_id: Optional[str] = None,
    l2_dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate one formal L1/L2 pair; returns {ok, issues, pair or None}."""
    issues: List[str] = []
    l1: Optional[DatasetManifest] = None
    l2: Optional[DatasetManifest] = None

    if l1_dataset_id:
        l1 = store.load_manifest(l1_dataset_id)
        if l1 is None:
            issues.append(f"l1 manifest missing: {l1_dataset_id}")
    if l2_dataset_id:
        l2 = store.load_manifest(l2_dataset_id)
        if l2 is None:
            issues.append(f"l2 manifest missing: {l2_dataset_id}")

    if l1 is None:
        pair = resolve_active_tushare_product_pair(store)
        if pair is None:
            issues.append("no ready formal L1 product dataset")
            return {"ok": False, "issues": issues, "pair": None}
        l1 = pair.l1_manifest
        if l2 is None:
            l2 = pair.l2_manifest

    if l2 is None:
        issues.append("formal L2 dataset could not be resolved")

    _ok, core_issues = _validate_pair_core(store, l1, l2)
    issues.extend(core_issues)

    pair = None
    if l1 is not None and l2 is not None:
        pair = _product_pair_from_manifests(l1, l2)
    return {"ok": not issues, "issues": issues, "pair": pair}


# ---------------------------------------------------------------------------
# Active product pair resolution
# ---------------------------------------------------------------------------


def resolve_active_tushare_product_pair(
    store: DatasetStore, *, deep_copy: bool = True
) -> Optional[ProductPair]:
    """Resolve the current formal L1/L2 product pair (atomic).

    Rule: the latest ready ``tushare_only_v1`` L1 wins; its raw parent must
    be the matching formal L2 (ready, composite_none, tushare-only lineage).
    Only a fully consistent pair is returned.

    ``copy=False`` skips manifest deepcopy for read-only callers (large
    warehouses: each manifest deepcopy walks ~100k symbol records).
    """
    l1_candidates: List[DatasetManifest] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=deep_copy)
        if m is None:
            continue
        if m.source != L1_SOURCE or m.adjustment != L1_ADJUSTMENT:
            continue
        if (m.period or "1d") != "1d" or m.status != "ready":
            continue
        if (m.provenance or {}).get("data_policy") != DATA_POLICY_TUSHARE_ONLY:
            continue
        l1_candidates.append(m)
    if not l1_candidates:
        return None
    l1_candidates.sort(
        key=lambda m: (
            int(m.data_cutoff_date or 0),
            int(m.symbol_count or 0),
            int(m.row_count or 0),
            m.created_at or "",
        ),
        reverse=True,
    )
    for l1 in l1_candidates:
        l2_id = (l1.raw_dataset_id or "").strip()
        if not l2_id:
            continue
        l2 = store.load_manifest(l2_id, deep_copy=deep_copy)
        if l2 is None or l2.status != "ready":
            continue
        if l2.source != L2_SOURCE or l2.adjustment != L2_ADJUSTMENT:
            continue
        if (l2.provenance or {}).get("data_policy") != DATA_POLICY_TUSHARE_ONLY:
            continue
        # Full lineage gate (P1-2): the candidate pair must pass the same
        # core validation as validate_tushare_product_pair (roles, data
        # policy, raw parent lineage, factor source, supplement overlap).
        # A pair the validator would reject (e.g. factor_source=tdx) is
        # never resolved as the active product.
        ok, _issues = _validate_pair_core(store, l1, l2, deep_copy=deep_copy)
        if not ok:
            continue
        return _product_pair_from_manifests(l1, l2)
    return None


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def _regression_issues(
    current: Optional[ProductPair],
    new_l2: Optional[DatasetManifest],
    base: Optional[DatasetManifest],
) -> List[str]:
    issues: List[str] = []
    if current is None or new_l2 is None:
        return issues
    cur_sig = manifest_history_signals(current.l2_manifest)
    new_sig = manifest_history_signals(new_l2)
    if new_sig.max_last_date is not None and cur_sig.max_last_date is not None:
        if new_sig.max_last_date < cur_sig.max_last_date:
            issues.append(
                f"data_date_regression: new L2 max date {new_sig.max_last_date} "
                f"< current {cur_sig.max_last_date}"
            )
    if (
        cur_sig.symbol_count > 0
        and new_sig.symbol_count < cur_sig.symbol_count * MAX_REGRESSION_RATIO
    ):
        issues.append(
            f"symbol_count_regression: {new_sig.symbol_count} < "
            f"{cur_sig.symbol_count} * {MAX_REGRESSION_RATIO}"
        )
    if (
        cur_sig.total_rows > 0
        and new_sig.total_rows < cur_sig.total_rows * MAX_REGRESSION_RATIO
    ):
        issues.append(
            f"total_rows_regression: {new_sig.total_rows} < "
            f"{cur_sig.total_rows} * {MAX_REGRESSION_RATIO}"
        )
    if base is not None and is_orphan_window(base):
        issues.append("base dataset is a short-window orphan surface")
    return issues


def _dry_run_composite_placeholder(
    store: DatasetStore,
    base: DatasetManifest,
    complement: DatasetManifest,
    cutoff: int,
) -> DatasetManifest:
    """In-memory candidate L2 for a dry-run reconcile (nothing is published).

    ``build_composite_none`` requires both parent manifest files on disk, but
    a dry-run reconcile must not publish the delisted complement manifest.
    This placeholder mirrors the composite record surface (base + complement
    blobs) so the regression gate still sees the same symbol/date shape. The
    dataset id is a best-effort deterministic hash of the parents (the real
    id needs the complement manifest file, which a dry-run never writes).
    """
    import hashlib as _hl

    records = [r for r in base.symbols if r.blob_sha256]
    records = records + [r for r in complement.symbols if r.blob_sha256]
    placeholder_sha = _hl.sha256(
        json.dumps(
            {
                "base": base.dataset_id,
                "supplement": complement.dataset_id,
                "cutoff": cutoff,
                "dry_run_placeholder": True,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    m = DatasetManifest(
        dataset_id=make_dataset_id(
            "internal", "composite_none", "1d", str(cutoff), placeholder_sha
        ),
        source="internal",
        adjustment="composite_none",
        period="1d",
        data_cutoff_date=cutoff,
        snapshot_date=int(time.strftime("%Y%m%d")),
        provider_version="composite_dry_run_placeholder",
        sync_run_id=f"composite_none_dry_run_{time.strftime('%Y%m%dT%H%M%S')}",
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        symbols=records,
        symbol_count=len(records),
        row_count=sum(int(r.row_count or 0) for r in records),
        provenance={
            "data_policy": DATA_POLICY_TUSHARE_ONLY,
            "dry_run_placeholder": True,
        },
    )
    return m


def _l1_partial_freshness_block_reason(
    store: DatasetStore,
    l1_dataset_id: str,
    candidate_l2: DatasetManifest,
    derive: dict,
) -> Optional[str]:
    """Return why a partial L1 is freshness-blocked, or None when it is not.

    The derive records the freshness gate in the published manifest's
    provenance; the idempotent derive path also echoes it in the result. For
    manifests published before that recording existed, recompute per-symbol
    freshness against the same raw surface the derive consumed (candidate L2
    x the L1's factor parent). Returns one of ``freshness_gate_blocked`` /
    ``freshness_below_threshold``, or None when the partial has another cause.
    """
    gate = derive.get("freshness_gate") if isinstance(derive, dict) else None
    if isinstance(gate, dict) and gate.get("status") == "blocked":
        return str(gate.get("reason") or "freshness_gate_blocked")
    l1_m = store.load_manifest(l1_dataset_id) if l1_dataset_id else None
    if l1_m is None:
        return None
    prov = l1_m.provenance or {}
    recorded = prov.get("freshness")
    if isinstance(recorded, dict) and recorded.get("status") == "blocked":
        return str(recorded.get("reason") or "freshness_gate_blocked")
    fac_id = str(prov.get("factor_dataset_id") or l1_m.factor_dataset_id or "")
    fac_m = store.load_manifest(fac_id) if fac_id else None
    if fac_m is None:
        return None
    fresh = _recompute_factor_freshness(store, candidate_l2, fac_m)
    if fresh is None or fresh["fresh_symbol_ratio"] < FACTOR_FRESH_RATIO_MIN:
        return "freshness_below_threshold"
    return None


def _reconcile_tushare_product_datasets_unlocked(
    store: DatasetStore,
    *,
    requested_cutoff: Optional[int] = None,
    dry_run: bool = False,
) -> ProductReconcileResult:
    """Build/refresh the formal L1/L2 product pair from local Tushare data.

    Order:
      1. latest complete tushare/none (base)
      2. Tushare delisted missing complement
      3. internal/composite_none (formal L2)
      4. latest ready tushare/adj_factor
      5. internal/composite_tushare_factor_qfq (formal L1)
      6. lineage / quality / freshness validation

    Never publishes a half-baked formal surface: when a parent is missing the
    result is ``waiting_for_parent``; when the new candidate regresses the
    current product (date rollback, coverage collapse, orphan window) the
    result is ``failed`` and nothing supersedes the current pair.
    """
    result = ProductReconcileResult(checked_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    base = select_tushare_base(store)
    if base is None:
        result.status = "waiting_for_parent"
        result.missing.append(f"{BASE_SOURCE}/{BASE_ADJUSTMENT}")
        return result
    result.base_dataset_id = base.dataset_id

    # Latest-candidate factor semantics (P0-2): the NEWEST factor manifest
    # wins regardless of status — an older ready factor must never shadow a
    # freshly synced partial (e.g. demoted by the freshness gate). Only when
    # no factor manifest exists at all does the ready-only selector remain
    # as the backward-compatible fallback.
    factor = _select_latest_tushare_factor_candidate(store)
    if factor is None:
        factor = select_tushare_factor(store)
    if factor is None:
        result.status = "waiting_for_parent"
        result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
        return result
    result.factor_dataset_id = factor.dataset_id
    # A freshness-gate-blocked partial is the latest surface: it must block
    # the formal publish (never silently fall back to an older ready factor
    # whose global max date happens to cover the cutoff).
    if (factor.status or "") == "partial" and (
        ((factor.provenance or {}).get("freshness") or {}).get("gate")
        == "blocked"
    ):
        result.status = "waiting_for_parent"
        result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
        result.issues.append(
            "freshness_gate_blocked: latest factor is partial (freshness "
            "gate blocked); refusing to publish over an older ready factor"
        )
        return result

    base_sig = manifest_history_signals(base)
    factor_sig = manifest_history_signals(factor)
    cutoff = int(requested_cutoff or base_sig.max_last_date or 0)
    if not cutoff:
        result.status = "failed"
        result.errors.append("cannot determine cutoff (no base dates)")
        return result
    result.cutoff = cutoff
    if not factor_sig.max_last_date or int(factor_sig.max_last_date) < cutoff:
        result.status = "waiting_for_parent"
        result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
        result.issues.append(
            "factor_date_lag: "
            f"factor={factor_sig.max_last_date or 0} raw={cutoff}"
        )
        return result

    # Recompute per-symbol freshness against the CURRENT raw base (never
    # trust the manifest provenance — it may have been computed against an
    # older raw surface). Missing freshness or a ratio below the minimum
    # blocks the formal publish.
    _fresh = _recompute_factor_freshness(store, base, factor)
    result.factor_freshness = _fresh
    if _fresh is None or _fresh["fresh_symbol_ratio"] < FACTOR_FRESH_RATIO_MIN:
        result.status = "waiting_for_parent"
        result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
        if _fresh is None:
            result.issues.append(
                "freshness_below_threshold: no active raw symbols to compare"
            )
        else:
            result.issues.append(
                f"freshness_below_threshold: "
                f"fresh={_fresh['fresh_count']}/{_fresh['active_count']} "
                f"(ratio={_fresh['fresh_symbol_ratio']} < "
                f"{FACTOR_FRESH_RATIO_MIN})"
            )
        return result
    # A non-ready factor that survived the gates (plain partial from provider
    # failures) can never feed the L1 derive — refuse before publishing L2.
    if (factor.status or "") != "ready":
        result.status = "waiting_for_parent"
        result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
        result.issues.append(
            f"factor_not_ready: latest factor status={factor.status}; "
            "refusing to publish the formal product from a non-ready factor"
        )
        return result

    delisted_pool = select_delisted_pool(store)
    if not delisted_pool:
        result.status = "waiting_for_parent"
        result.missing.append("tushare_delisted")
        return result

    complement = build_delisted_missing_complement(
        store,
        base=base,
        delisted_pool=delisted_pool,
        cutoff=cutoff,
        dry_run=dry_run,
    )
    result.supplement_dataset_id = complement.dataset_id
    if (complement.provenance or {}).get("excluded_overlap"):
        result.issues.append(
            f"delisted/base overlap excluded: "
            f"{len((complement.provenance or {}).get('excluded_overlap', []))} symbols"
        )

    # Gate BEFORE publishing the candidate L2: no date/coverage regression.
    current = resolve_active_tushare_product_pair(store)

    # build_composite_none requires both parent manifest files on disk; a
    # dry-run must never publish the delisted complement, so on a store that
    # never published the product chain use a read-only placeholder for the
    # regression gate instead of letting CompositeParentError escape.
    if dry_run and store.load_manifest(complement.dataset_id) is None:
        candidate_l2 = _dry_run_composite_placeholder(
            store, base, complement, cutoff
        )
    else:
        candidate_l2 = build_composite_none(
            store,
            base_dataset_id=base.dataset_id,
            supplement_dataset_id=complement.dataset_id,
            cutoff=cutoff,
            dry_run=True,
        )
    regression = _regression_issues(current, candidate_l2, base)
    if regression:
        result.status = "failed"
        result.issues.extend(regression)
        result.l2_dataset_id = current.l2_dataset_id if current else ""
        result.l1_dataset_id = current.l1_dataset_id if current else ""
        return result

    result.l2_dataset_id = candidate_l2.dataset_id

    sup_factor = select_supplement_factor(store, factor)
    # BSE alias universe: reuse the latest point-in-time universe when present.
    # PointInTimeUniverse.list_universes returns the universe dataset ids
    # (file stems under <root>/universes), same ids the CLI passes via
    # --universe-dataset-id and from_root() resolves.
    universe_id = ""
    try:
        from .pit_universe import PointInTimeUniverse

        unis = sorted(
            PointInTimeUniverse.list_universes(store.root),
            reverse=True,
        )
        if unis:
            universe_id = unis[0]
    except Exception:
        universe_id = ""

    # ---- factor missing-symbol pre-check (BEFORE any product publish) ----
    # Mirrors the resolution rule of derive_composite_tushare_factor_qfq
    # (FACTOR_RESOLUTION_RULE_VERSION) against the candidate L2 symbol set,
    # so a large factor gap (e.g. a missing delisted factor supplement)
    # returns waiting_for_parent WITHOUT leaving a ready L2 manifest (or a
    # published partial L1) on the disk. The real derive below re-runs the
    # same rule and stays authoritative; its dry_run path requires the L2
    # manifest to be published first, hence the explicit pre-check here.
    l2_syms = {s.symbol for s in candidate_l2.symbols if s.quality == "ok"}
    fac_ok = {r.symbol for r in factor.symbols if r.quality == "ok"}
    sup_ok = (
        {r.symbol for r in sup_factor.symbols if r.quality == "ok"}
        if sup_factor
        else set()
    )
    alias_to_canon: Dict[str, str] = {}
    if universe_id:
        try:
            from .pit_universe import PointInTimeUniverse

            pit = PointInTimeUniverse.from_root(store.root, universe_id)
            for canon, w in pit.entries.items():
                for alias in w.aliases:
                    alias_to_canon[alias] = canon
        except Exception:
            alias_to_canon = {}
    missing_fac = sorted(
        s for s in l2_syms
        if s not in fac_ok
        and s not in sup_ok
        and not (
            s in alias_to_canon
            and (alias_to_canon[s] in fac_ok or alias_to_canon[s] in sup_ok)
        )
    )
    l1_eligible = len(l2_syms) - len(missing_fac)
    if missing_fac and len(missing_fac) / max(l1_eligible, 1) > 0.005:
        comp_syms = {s.symbol for s in complement.symbols}
        base_syms = {s.symbol for s in base.symbols if s.blob_sha256}
        missing_set = set(missing_fac)
        if missing_set <= comp_syms:
            result.missing.append("tushare_delisted_factor")
        elif missing_set & base_syms:
            result.missing.append("tushare_factor")
        result.issues.append(
            f"l1_missing_factor_symbols={len(missing_fac)} sample="
            f"{missing_fac[:5]}"
        )
        result.status = "waiting_for_parent"
        return result

    # Checks passed: publish the formal L2 first (the L1 derive loads it as
    # its raw parent), then the real L1 derivation. A dry-run on a store
    # without the published L2 manifest stops here: the derive cannot load a
    # manifest that a dry-run must not write.
    if dry_run and store.load_manifest(candidate_l2.dataset_id) is None:
        result.status = "dry_run"
        result.published = False
        result.issues.append(
            "dry_run_placeholder: candidate L2 manifest not on disk; the L1 "
            "derivation requires the published composite_none manifest"
        )
        return result

    if not dry_run:
        build_composite_none(
            store,
            base_dataset_id=base.dataset_id,
            supplement_dataset_id=complement.dataset_id,
            cutoff=cutoff,
        )

    derive = derive_composite_tushare_factor_qfq(
        store,
        raw_dataset_id=candidate_l2.dataset_id,
        factor_dataset_id=factor.dataset_id,
        supplement_factor_dataset_id=sup_factor.dataset_id if sup_factor else None,
        universe_dataset_id=universe_id or None,
        cutoff=cutoff,
        dry_run=dry_run,
    )
    if derive.get("status") != "success":
        result.status = "failed"
        result.errors.append(f"l1_derivation_failed: {derive.get('error', 'unknown')}")
        return result
    result.l1_dataset_id = derive["dataset_id"]

    if not dry_run:
        validation = validate_tushare_product_pair(
            store,
            l1_dataset_id=result.l1_dataset_id,
            l2_dataset_id=result.l2_dataset_id,
        )
        if not validation["ok"]:
            # "L1 not ready: partial" on an EXISTING deterministic L1 is a
            # waiting-for-fresher-factors situation when the partial was
            # caused by the freshness gate (the derive's idempotent path
            # returns the on-disk status without re-running the gate). Any
            # other partial cause stays a hard failure.
            if any(
                i.startswith("L1 not ready: partial") for i in validation["issues"]
            ):
                _why = _l1_partial_freshness_block_reason(
                    store, result.l1_dataset_id, candidate_l2, derive
                )
            else:
                _why = None
            if _why is not None:
                result.status = "waiting_for_parent"
                result.missing.append(f"{FACTOR_SOURCE}/{FACTOR_ADJUSTMENT}")
                result.issues.extend(validation["issues"])
                result.issues.append(
                    f"l1_not_ready_freshness: {_why}; the formal L1 waits for "
                    "a re-derivation over fresher factors"
                )
                return result
            result.status = "failed"
            result.issues.extend(validation["issues"])
            return result

    if current is not None and current.l2_dataset_id == result.l2_dataset_id:
        result.status = "up_to_date"
        result.published = False
    else:
        result.status = "published"
        result.published = True
    return result


def reconcile_tushare_product_datasets(
    store: DatasetStore,
    *,
    requested_cutoff: Optional[int] = None,
    dry_run: bool = False,
) -> ProductReconcileResult:
    """Serialize reconciliation across service and sync processes."""
    from .sync_lock import SyncLockHeldError, SyncTaskLock

    lock = SyncTaskLock(
        store.root,
        source="internal",
        adjustment="tushare_product_reconcile",
        period="1d",
        sync_run_id=make_sync_run_id("tushare_product"),
    )
    try:
        lock.acquire()
    except SyncLockHeldError:
        return ProductReconcileResult(
            status="busy",
            issues=["another Tushare product reconciliation is running"],
            checked_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    try:
        return _reconcile_tushare_product_datasets_unlocked(
            store,
            requested_cutoff=requested_cutoff,
            dry_run=dry_run,
        )
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _last_weekday_on_or_before(ymd: int) -> int:
    import datetime as _dt

    d = _dt.date(ymd // 10000, (ymd // 100) % 100, ymd % 100)
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return int(d.strftime("%Y%m%d"))


def tushare_product_data_health(
    store: DatasetStore,
    *,
    expected_trading_day: Optional[int] = None,
    calendar_dates: Optional[Sequence[int]] = None,
    recent_sync_errors: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Structured data-health snapshot over the Tushare-only product chain.

    Never uses the universe file maximum date as the formal product date:
    raw / factor / L1 / L2 each report their own real dataset date.
    """
    base = select_tushare_base(store)
    factor = select_tushare_factor(store)
    delisted_pool = select_delisted_pool(store)
    pair = resolve_active_tushare_product_pair(store)

    def _ds_info(m: Optional[DatasetManifest], key: str, label: str) -> Dict[str, Any]:
        if m is None:
            return {
                "key": key, "label": label, "status": "missing",
                "dataset_id": None, "data_cutoff_date": None, "max_date": None,
                "fresh_symbol_ratio": None, "stale_active_symbols": None,
            }
        sig = manifest_history_signals(m)
        freshness = (m.provenance or {}).get("freshness")
        return {
            "key": key, "label": label, "status": m.status,
            "dataset_id": m.dataset_id, "data_cutoff_date": m.data_cutoff_date,
            "max_date": sig.max_last_date,
            "symbol_count": sig.symbol_count, "row_count": sig.total_rows,
            "data_policy": (m.provenance or {}).get("data_policy"),
            "fresh_symbol_ratio": (freshness or {}).get("fresh_symbol_ratio"),
            "stale_active_symbols": (freshness or {}).get("stale_active_symbols"),
        }

    raw_info = _ds_info(base, "tushare_raw", "Tushare raw")
    factor_info = _ds_info(factor, "tushare_factor", "Tushare adj_factor")
    delisted_info: Dict[str, Any] = {
        "status": "missing" if not delisted_pool else "ready",
        "dataset_ids": [m.dataset_id for m in delisted_pool],
        "symbol_count": sum(
            int(m.symbol_count or 0) for m in delisted_pool
        ),
    }
    l2_info: Dict[str, Any] = {}
    if pair is not None:
        l2_info = {
            "status": "ready",
            "dataset_id": pair.l2_dataset_id,
            "data_cutoff_date": pair.l2_manifest.data_cutoff_date,
            "max_date": pair.l2_max_date,
            "data_policy": pair.data_policy,
        }
    else:
        l2_info = {"status": "missing", "dataset_id": None, "max_date": None}
    l1_info: Dict[str, Any] = {}
    if pair is not None:
        l1_info = {
            "status": "ready",
            "dataset_id": pair.l1_dataset_id,
            "data_cutoff_date": pair.l1_manifest.data_cutoff_date,
            "max_date": pair.l1_max_date,
            "data_policy": pair.data_policy,
        }
    else:
        l1_info = {"status": "missing", "dataset_id": None, "max_date": None}

    lineage = validate_tushare_product_pair(store)
    if pair is None:
        lineage = {"ok": False, "issues": ["no active formal product pair"], "pair": None}

    # ---- trading-day lag (calendar-aware when provided) ----
    expected = int(
        expected_trading_day
        or _last_weekday_on_or_before(int(time.strftime("%Y%m%d")))
    )
    raw_max = raw_info.get("max_date")
    factor_max = factor_info.get("max_date")

    def _lag(actual: Optional[int]) -> Optional[int]:
        if actual is None:
            return None
        if calendar_dates:
            days = [d for d in calendar_dates if int(d) <= expected]
            idx = days.index(actual) if actual in days else None
            if idx is not None:
                return max(0, len(days) - 1 - idx)
            return None
        return max(0, expected - int(actual))

    lag_raw = _lag(raw_max)
    lag_factor = _lag(factor_max)

    issues: List[str] = []
    if raw_max is None:
        issues.append("tushare_raw_missing")
    elif lag_raw is not None and lag_raw > 0:
        issues.append(f"tushare_raw_lag={lag_raw}")
    if factor_max is None:
        issues.append("tushare_factor_missing")
    elif lag_factor is not None and lag_factor > 0:
        issues.append(f"tushare_factor_lag={lag_factor}")
    if not delisted_pool:
        issues.append("delisted_supplement_missing")
    if pair is None:
        issues.append("formal_product_pair_missing")
    else:
        if not lineage["ok"]:
            issues.extend(lineage["issues"])
    for err in recent_sync_errors or []:
        issues.append(f"recent_sync_error: {err.get('error') or err.get('status')}")

    # ---- freshness classification (trading-day aware) ----
    # The product chain is only as fresh as its stalest required parent.
    lag_for_status = max(
        (l for l in (lag_raw, lag_factor) if l is not None), default=0
    )
    if pair is None or raw_max is None:
        status = "stale"
    elif lag_for_status <= 1 and not any(
        i.startswith(("tushare_raw_missing", "tushare_factor_missing"))
        or i.startswith("formal_product")
        or i.startswith("recent_sync_error")
        for i in issues
    ):
        status = "healthy"
    elif lag_for_status <= 3:
        status = "warning"
    else:
        status = "stale"

    # ---- historical completeness (pre-2001 backfill is a separate channel) ----
    hist_complete = False
    hist_note = "no base dataset"
    if base is not None:
        sig = manifest_history_signals(base)
        p10 = sig.p10_first_date or 0
        hist_complete = p10 <= 20000101
        hist_note = (
            "complete (10th-percentile history start {p10})".format(p10=p10)
            if hist_complete
            else (
                "pre-2001 history incomplete (10th-percentile start {p10}); "
                "forward incremental backfill is a separate channel".format(p10=p10)
            )
        )

    return {
        "status": status,
        "expected_latest_trading_day": expected,
        "current_freshness": {
            "tushare_raw": raw_info,
            "tushare_factor": factor_info,
            "delisted_supplement": delisted_info,
        },
        "formal_l2": l2_info,
        "formal_l1": l1_info,
        "lineage": {
            "consistent": bool(lineage["ok"]),
            "l1_raw_parent": pair.l1_manifest.raw_dataset_id if pair else None,
            "l2_dataset_id": pair.l2_dataset_id if pair else None,
            "issues": lineage["issues"],
        },
        "trading_day_lag": {"raw": lag_raw, "factor": lag_factor},
        "historical_completeness": {
            "complete": hist_complete,
            "note": hist_note,
            "current_freshness": raw_info.get("max_date"),
        },
        "recent_sync_errors": recent_sync_errors or [],
        "bootstrap_fallback_active": pair is None,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
