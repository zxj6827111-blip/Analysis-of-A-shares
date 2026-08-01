"""Composite dataset builder — Gate B3.

Builds internal/composite_none by composing two parent datasets at the
manifest level:

  parent 1 (base):       local_vendor/none   — every vendor symbol used as-is
  parent 2 (supplement): tushare/none        — ONLY symbols absent from base

Rules (composite_merge_rule_version=composite_merge_v1):
  1. base symbols always win: a symbol present in both parents is a rule
     violation (supplement must be disjoint) and raises CompositeOverlapError
     — intra-symbol cross-source splicing is NOT supported and NOT silent;
  2. blobs are content-addressed and shared: the composite references parent
     blob hashes, no bar data is copied or rewritten;
  3. per-symbol provenance records the contributing parent dataset;
  4. both parents must be status=ready and are never modified.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Dict, List, Optional

from .dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
    make_dataset_id,
)

COMPOSITE_MERGE_RULE_VERSION = "composite_merge_v1"


class CompositeOverlapError(RuntimeError):
    """A supplement symbol collides with a base symbol (unapproved splice)."""


class CompositeParentError(RuntimeError):
    """A parent dataset is missing or not ready."""


def _manifest_file_sha256(store: DatasetStore, dataset_id: str) -> str:
    path = store.manifests_dir / f"{dataset_id}.json"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_composite_none(
    store: DatasetStore,
    *,
    base_dataset_id: str,
    supplement_dataset_id: str,
    cutoff: int,
    dry_run: bool = False,
) -> DatasetManifest:
    """Compose base + supplement into an immutable composite_none dataset.

    Returns the published manifest (or the unpublished manifest on dry_run).
    Raises CompositeParentError / CompositeOverlapError on rule violations.
    """
    base = store.load_manifest(base_dataset_id)
    supp = store.load_manifest(supplement_dataset_id)
    if base is None or base.status != "ready":
        raise CompositeParentError(
            f"base dataset not ready: {base_dataset_id} "
            f"({'missing' if base is None else base.status})"
        )
    if supp is None or supp.status != "ready":
        raise CompositeParentError(
            f"supplement dataset not ready: {supplement_dataset_id} "
            f"({'missing' if supp is None else supp.status})"
        )

    base_syms = {s.symbol for s in base.symbols if s.blob_sha256}
    overlap = [
        s.symbol for s in supp.symbols if s.blob_sha256 and s.symbol in base_syms
    ]
    if overlap:
        raise CompositeOverlapError(
            f"{len(overlap)} supplement symbols already exist in base "
            f"(cross-source splicing is not approved): {overlap[:5]}"
        )

    records: List[SymbolRecord] = []
    symbol_provenance: Dict[str, str] = {}
    missing_blobs: List[str] = []
    total_rows = 0
    for parent, parent_id in ((base, base_dataset_id), (supp, supplement_dataset_id)):
        for s in parent.symbols:
            if not s.blob_sha256:
                continue
            if not store.blob_exists(s.blob_sha256):
                missing_blobs.append(s.symbol)
                continue
            records.append(
                SymbolRecord(
                    symbol=s.symbol,
                    blob_sha256=s.blob_sha256,
                    first_date=s.first_date,
                    last_date=s.last_date,
                    row_count=s.row_count,
                    quality="ok",
                )
            )
            symbol_provenance[s.symbol] = parent_id
            total_rows += s.row_count
    if missing_blobs:
        raise CompositeParentError(
            f"{len(missing_blobs)} parent blobs missing: {missing_blobs[:5]}"
        )

    base_sha = _manifest_file_sha256(store, base_dataset_id)
    supp_sha = _manifest_file_sha256(store, supplement_dataset_id)

    canonical_pre = json.dumps(
        {
            "source": "internal",
            "adjustment": "composite_none",
            "period": "1d",
            "cutoff": cutoff,
            "merge_rule": COMPOSITE_MERGE_RULE_VERSION,
            "parents": [
                {"dataset_id": base_dataset_id, "manifest_file_sha256": base_sha},
                {"dataset_id": supplement_dataset_id, "manifest_file_sha256": supp_sha},
            ],
        },
        sort_keys=True,
    )
    pre_sha = hashlib.sha256(canonical_pre.encode()).hexdigest()
    dataset_id = make_dataset_id(
        "internal", "composite_none", "1d", str(cutoff), pre_sha
    )

    existing = store.load_manifest(dataset_id)
    if existing is not None:
        # deterministic id: same parents + rule => already published (idempotent)
        return existing

    manifest = DatasetManifest(
        dataset_id=dataset_id,
        source="internal",
        adjustment="composite_none",
        period="1d",
        data_cutoff_date=cutoff,
        snapshot_date=int(time.strftime("%Y%m%d")),
        provider_version="composite",
        sync_run_id=f"composite_none_{time.strftime('%Y%m%dT%H%M%S')}",
        status="building",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        dataset_type="bars",
        universe_type="b1_reference_universe_composite",
        universe_definition_version="gate_b1_reference_v1",
        survivorship_bias=False,
        historical_universe_complete=True,
        delisted_coverage_complete=True,
        warning_text=(
            "Execution (L2) dataset composed of local_vendor bars plus "
            "Tushare bars for delisted stocks missing from the vendor. "
            "Raw unadjusted prices; use the matching composite QFQ dataset "
            "for signals (L1)."
        ),
        recommended_use=[
            "survivorship-safe backtest execution (L2)",
            "B6 composite QFQ parent",
        ],
        prohibited_or_discouraged_use=[
            "signal generation without adjustment handling",
        ],
        provenance={
            "gate": "B3",
            "composite_merge_rule_version": COMPOSITE_MERGE_RULE_VERSION,
            "parents": [
                {
                    "dataset_id": base_dataset_id,
                    "manifest_file_sha256": base_sha,
                    "manifest_sha256_field": base.manifest_sha256,
                    "role": "base",
                    "symbol_count": len(base_syms),
                },
                {
                    "dataset_id": supplement_dataset_id,
                    "manifest_file_sha256": supp_sha,
                    "manifest_sha256_field": supp.manifest_sha256,
                    "role": "supplement",
                    "symbol_count": sum(1 for s in supp.symbols if s.blob_sha256),
                },
            ],
            "symbol_provenance": symbol_provenance,
            "intra_symbol_splicing": "disabled",
        },
        token_exposed=False,
    )
    manifest.symbols = records
    manifest.symbol_count = len(records)
    manifest.row_count = total_rows
    manifest.expected_symbol_count = len(records)
    manifest.imported_symbol_count = len(records)
    manifest.coverage_ratio = 1.0

    if dry_run:
        return manifest

    store.publish(manifest)
    return manifest
