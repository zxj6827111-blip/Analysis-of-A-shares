# -*- coding: utf-8 -*-
"""Dataset binding validation (Gate C remediation, D1/D4).

Every explicitly requested dataset_id must be validated against its manifest
BEFORE a run/experiment is created: existence, ready status, dataset_type,
source, adjustment, period, signal/execution role, parent lineage and basic
blob integrity. Mismatches raise :class:`DatasetBindingError` with a
structured payload (code / requested vs manifest summary / remediation) that
the API layer maps to 4xx — never a generic 500, never a silent fallback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .dataset_store import DatasetManifest
from .repository import DatasetNotFoundError, MarketDataRepository

# Adjustments that mark a dataset as signal-plane (adjusted) vs execution (raw)
EXECUTION_ADJUSTMENT = "none"
# Gate B7: internal/composite_none is unadjusted raw too (survivorship-safe
# execution plane). Any adjustment in this set is execution-role.
EXECUTION_ADJUSTMENTS = ("none", "composite_none")

# Product data-source keys. The Tushare-only policy (tushare_only_v1) makes
# the formal composite the default product surface; `local_vendor` /
# `tdx_local` / `tdxquant` remain available as EXPLICIT legacy selections only
# (historical reproducibility), never as product defaults.
PRODUCT_SOURCES = ("local_vendor", "tdx_local", "tdxquant", "tushare", "internal", "raw")


def signal_resolve_candidates(
    src: Optional[str], adj: Optional[str]
) -> List[Tuple[str, str]]:
    """Ordered (source, adjustment) pairs for product signal sources.

    Tushare-only policy defaults:
      raw       -> formal L2 (internal/composite_none), bootstrap fallback to
                   the latest complete tushare/none (quick/read-only only)
      tushare   -> formal L1 (internal/composite_tushare_factor_qfq) only;
                   older L1-class datasets are NOT a default fallback for
                   formal backtests (fail-closed product surface)
    Legacy families (tdxquant / local_vendor / tdx_local) bind strictly to
    their own source.
    """
    if src == "raw":
        return [
            ("internal", "composite_none"),
            ("tushare", "none"),
        ]
    if src == "tushare":
        return [
            ("internal", "composite_tushare_factor_qfq"),
        ]
    if src == "tdxquant":
        return [("tdxquant", adj or "front")]
    if src == "internal":
        return [
            ("internal", adj or "tushare_factor_qfq"),
            ("internal", "composite_tushare_factor_qfq"),
        ]
    return [(src, adj or "")]


def execution_resolve_candidates(src: Optional[str]) -> List[Tuple[str, str]]:
    """Ordered (source, adjustment) pairs for the L2 execution dataset.

    Tushare-only policy: the product default (None / "" / "raw" / "internal")
    resolves to the formal L2 (internal/composite_none) ONLY — a formal
    backtest never falls back to a survivorship-unsafe raw family when the
    product surface is missing.

    Legacy families stay available as EXPLICIT selections: local_vendor /
    tdx_local / tdxquant / tushare each bind to their own raw `none` set
    (historical reproducibility), and a bare `local_vendor` keeps the old
    cross-family fallback chain.
    """
    s = (src or "").strip()
    if s in ("internal", "raw", ""):
        return [("internal", "composite_none")]
    if s in PRODUCT_SOURCES:
        if s == "local_vendor":
            return [
                ("local_vendor", "none"),
                ("tdx_local", "none"),
                ("tdxquant", "none"),
                ("tushare", "none"),
            ]
        return [(s, "none")]
    return [(s, "none")]


class DatasetBindingError(ValueError):
    """Structured dataset binding failure (maps to HTTP 4xx)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        dataset_id: Optional[str] = None,
        requested_source: Optional[str] = None,
        requested_adjustment: Optional[str] = None,
        manifest_source: Optional[str] = None,
        manifest_adjustment: Optional[str] = None,
        remediation: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = int(http_status)
        self.dataset_id = dataset_id
        self.requested_source = requested_source
        self.requested_adjustment = requested_adjustment
        self.manifest_source = manifest_source
        self.manifest_adjustment = manifest_adjustment
        self.remediation = remediation
        self.extra = dict(extra or {})

    def to_payload(self) -> Dict[str, Any]:
        payload = {
            "code": self.code,
            "message": str(self),
            "dataset_id": self.dataset_id,
            "requested_source": self.requested_source,
            "requested_adjustment": self.requested_adjustment,
            "manifest_source": self.manifest_source,
            "manifest_adjustment": self.manifest_adjustment,
            "remediation": self.remediation,
        }
        payload.update(self.extra)
        return payload


def _load_manifest(
    repo: MarketDataRepository,
    dataset_id: str,
    *,
    requested_source: Optional[str],
    requested_adjustment: Optional[str],
) -> DatasetManifest:
    try:
        return repo.get_dataset(dataset_id)
    except DatasetNotFoundError:
        raise DatasetBindingError(
            "DATASET_NOT_FOUND",
            f"dataset not found: {dataset_id}",
            http_status=404,
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            remediation="检查 dataset_id 拼写，或在数据仓库页面查看可用的 ready 数据集",
        ) from None
    except DatasetBindingError:
        raise
    except Exception as e:
        raise DatasetBindingError(
            "DATASET_MANIFEST_INVALID",
            f"dataset manifest unreadable: {dataset_id}: {type(e).__name__}",
            http_status=422,
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            remediation="manifest 损坏或格式不兼容；请检查数据根完整性（不要修改不可变 manifest）",
        ) from e


def _check_common(
    manifest: DatasetManifest,
    *,
    dataset_id: str,
    role: str,
    requested_source: Optional[str],
    requested_adjustment: Optional[str],
    requested_period: str,
) -> None:
    if manifest.status != "ready":
        raise DatasetBindingError(
            "DATASET_NOT_READY",
            f"dataset {dataset_id} status={manifest.status}; "
            f"only ready datasets can be used for backtest",
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation="仅可选择 status=ready 的数据集；partial/building 数据集不可用于回测",
        )
    if (manifest.dataset_type or "bars") != "bars":
        raise DatasetBindingError(
            "DATASET_ROLE_MISMATCH",
            f"dataset {dataset_id} dataset_type={manifest.dataset_type} "
            f"cannot be used as a {role} bars dataset",
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation="factor 数据集不能作为信号/执行行情集；请选择 bars 类型数据集",
        )
    if requested_period and manifest.period != requested_period:
        raise DatasetBindingError(
            "DATASET_BINDING_MISMATCH",
            f"dataset {dataset_id} period={manifest.period} does not match "
            f"requested period={requested_period}",
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation="请求周期与数据集周期不一致",
            extra={"manifest_period": manifest.period, "requested_period": requested_period},
        )
    if requested_source and manifest.source != requested_source:
        raise DatasetBindingError(
            "DATASET_BINDING_MISMATCH",
            f"dataset {dataset_id} source={manifest.source} does not match "
            f"requested source={requested_source}",
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation=(
                f"该 dataset 属于 source={manifest.source}/adjustment={manifest.adjustment}；"
                f"请修正请求的 source，或改用与请求匹配的 dataset_id"
            ),
        )
    if requested_adjustment and manifest.adjustment != requested_adjustment:
        raise DatasetBindingError(
            "DATASET_BINDING_MISMATCH",
            f"dataset {dataset_id} adjustment={manifest.adjustment} does not "
            f"match requested adjustment={requested_adjustment}",
            dataset_id=dataset_id,
            requested_source=requested_source,
            requested_adjustment=requested_adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation=(
                f"该 dataset 的复权口径为 {manifest.adjustment}；"
                f"请修正请求的 adjustment，或改用匹配的 dataset_id"
            ),
        )


def _spot_check_blobs(
    repo: MarketDataRepository,
    manifest: DatasetManifest,
    dataset_id: str,
) -> None:
    """Cheap integrity check: first symbol record with a blob must exist on
    disk. Full validation stays in repo.validate_dataset (audit tooling)."""
    store = getattr(repo, "_store", None)
    if store is None:
        return
    for rec in manifest.symbols:
        if rec.blob_sha256:
            if not store.blob_exists(rec.blob_sha256):
                raise DatasetBindingError(
                    "DATASET_CORRUPT",
                    f"dataset {dataset_id}: blob missing for {rec.symbol} "
                    f"({rec.blob_sha256[:12]}…)",
                    http_status=422,
                    dataset_id=dataset_id,
                    manifest_source=manifest.source,
                    manifest_adjustment=manifest.adjustment,
                    remediation="数据集 blob 缺失（dataset 损坏）；请检查数据根，不要静默继续",
                )
            return


def validate_signal_dataset_binding(
    repo: MarketDataRepository,
    dataset_id: str,
    *,
    source: Optional[str],
    adjustment: Optional[str],
    period: str = "1d",
    check_lineage: bool = True,
    allow_raw_signal: bool = False,
) -> DatasetManifest:
    """Validate an explicitly requested SIGNAL dataset_id (D1).

    allow_raw_signal: product UI「未复权」explicitly uses a raw/none dataset as
    the L1 signal plane (research_unadjusted path). Default False keeps the
    historical role guard (raw sets are execution-only).
    """
    manifest = _load_manifest(
        repo, dataset_id, requested_source=source, requested_adjustment=adjustment
    )
    _check_common(
        manifest,
        dataset_id=dataset_id,
        role="signal",
        requested_source=source,
        requested_adjustment=adjustment,
        requested_period=period,
    )
    # signal role: execution raw datasets (adjustment none/composite_none)
    # are not signal sets — unless product explicitly chose 未复权 signal.
    if manifest.adjustment in EXECUTION_ADJUSTMENTS and not allow_raw_signal:
        raise DatasetBindingError(
            "DATASET_ROLE_MISMATCH",
            f"dataset {dataset_id} adjustment={manifest.adjustment} is an "
            f"execution (raw) dataset and cannot be bound as the signal dataset",
            dataset_id=dataset_id,
            requested_source=source,
            requested_adjustment=adjustment,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation="信号数据集必须是复权口径数据集（front / tushare_factor_qfq 等），"
            "或显式选择「未复权」信号源",
        )
    if check_lineage and (manifest.raw_dataset_id or manifest.factor_dataset_id):
        for parent_id, kind in (
            (manifest.raw_dataset_id, "raw"),
            (manifest.factor_dataset_id, "factor"),
        ):
            if not parent_id:
                continue
            try:
                repo.get_dataset(parent_id)
            except DatasetNotFoundError:
                raise DatasetBindingError(
                    "DATASET_LINEAGE_BROKEN",
                    f"dataset {dataset_id} {kind} parent {parent_id} "
                    f"is missing from the data root",
                    http_status=422,
                    dataset_id=dataset_id,
                    requested_source=source,
                    requested_adjustment=adjustment,
                    manifest_source=manifest.source,
                    manifest_adjustment=manifest.adjustment,
                    remediation="派生数据集的父集缺失；数据根不完整，禁止继续",
                ) from None
    _spot_check_blobs(repo, manifest, dataset_id)
    return manifest


def validate_execution_dataset_binding(
    repo: MarketDataRepository,
    dataset_id: str,
    *,
    source: Optional[str],
    period: str = "1d",
) -> DatasetManifest:
    """Validate an explicitly requested EXECUTION dataset_id (D1).

    Execution datasets must be raw (adjustment=none) — adjusted datasets are
    a role violation regardless of the requested source.
    """
    manifest = _load_manifest(
        repo, dataset_id, requested_source=source,
        requested_adjustment=EXECUTION_ADJUSTMENT,
    )
    _check_common(
        manifest,
        dataset_id=dataset_id,
        role="execution",
        requested_source=source,
        requested_adjustment=None,
        requested_period=period,
    )
    if manifest.adjustment not in EXECUTION_ADJUSTMENTS:
        raise DatasetBindingError(
            "DATASET_ROLE_MISMATCH",
            f"dataset {dataset_id} adjustment={manifest.adjustment} cannot be "
            f"bound as the execution dataset (execution requires raw "
            f"none/composite_none)",
            dataset_id=dataset_id,
            requested_source=source,
            requested_adjustment=EXECUTION_ADJUSTMENT,
            manifest_source=manifest.source,
            manifest_adjustment=manifest.adjustment,
            remediation="执行数据集必须是未复权 raw（adjustment=none 或 composite_none）",
        )
    _spot_check_blobs(repo, manifest, dataset_id)
    return manifest


def manifest_symbol_index(manifest: DatasetManifest) -> Dict[str, Any]:
    """Build {canonical_and_variant_symbol -> SymbolRecord} for O(1) coverage
    checks (records without blobs are still indexed; caller checks quality)."""
    idx: Dict[str, Any] = {}
    for rec in manifest.symbols:
        for variant in MarketDataRepository._symbol_variants(rec.symbol):
            idx.setdefault(variant, rec)
    return idx


def classify_symbol_coverage(
    symbol_index: Dict[str, Any],
    code: str,
) -> str:
    """Coverage class for one requested code against a dataset index (D3).

    Returns one of: "ok" | "not_in_dataset" | "no_data_allowlisted" | "error_record".
    """
    rec = None
    for variant in MarketDataRepository._symbol_variants(code):
        rec = symbol_index.get(variant)
        if rec is not None:
            break
    if rec is None:
        return "not_in_dataset"
    if not getattr(rec, "blob_sha256", ""):
        q = getattr(rec, "quality", "")
        return "no_data_allowlisted" if q == "no_data" else "error_record"
    return "ok"
