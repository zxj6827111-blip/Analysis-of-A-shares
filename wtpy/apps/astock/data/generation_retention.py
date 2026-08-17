# -*- coding: utf-8 -*-
"""Reference-aware local retention for overlay generations."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

from .dataset_store import (
    DatasetManifest,
    DatasetStore,
    manifest_runtime_dependencies,
)
from .delta_store import DeltaStore, load_overlay_state
from .generation_catalog import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_RETIRED,
    generation_manifest_ids,
    mark_generations_expired,
    reconcile_generation_catalog,
)

DEFAULT_KEEP_GENERATIONS = 2
DEFAULT_GRACE_DAYS = 7
DEFAULT_LEGACY_KEEP_PER_FAMILY = 1
DEFAULT_LEGACY_MIGRATION_GRACE_DAYS = 14
DEFAULT_LEGACY_MANIFEST_MIN_AGE_DAYS = 7
_LIVE_STATUSES = {"queued", "pending", "running", "stopping"}

# Only the daily materialized layers that were repeatedly rewritten before
# overlay_v1 are governed here. Vendor archives, minute data, TDX datasets and
# point-in-time universes stay outside this automatic policy.
_LEGACY_DAILY_FAMILIES = {
    ("tushare", "none", "1d"),
    ("tushare", "qfq", "1d"),
    ("tushare", "adj_factor", "1d"),
    ("internal", "delisted_complement", "1d"),
    ("internal", "composite_none", "1d"),
    ("internal", "composite_tushare_factor_qfq", "1d"),
    ("internal", "tushare_factor_qfq", "1d"),
}


def _parse_time(value: object) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _json_dataset_ids(value: object) -> Set[str]:
    found: Set[str] = set()

    def _walk(item, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                _walk(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                _walk(child, key)
        elif (
            isinstance(item, str)
            and item
            and (
                key.endswith("dataset_id")
                or key.endswith("dataset_ids")
                or key in {"dataset", "signal_dataset", "execution_dataset"}
            )
        ):
            found.add(item)

    _walk(value)
    return found


def collect_live_dataset_references() -> dict:
    """Read only queued/running task references from the local run database."""
    configured = os.environ.get("ASTOCK_RUN_DB_PATH", "").strip()
    if configured:
        db_path = Path(configured)
    else:
        try:
            from wtpy.apps.astock.config import get_default_config
            from wtpy.apps.astock.service.db import db_path as resolve_db_path

            db_path = resolve_db_path(get_default_config())
        except Exception:
            db_path = Path()

    result = {
        "db_path": str(db_path) if db_path else "",
        "dataset_ids": set(),
        "warnings": [],
    }
    if not db_path or not db_path.is_file():
        return result

    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        result["warnings"].append(
            f"run database unavailable: {type(exc).__name__}: {exc}"
        )
        return result

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "runs" in tables:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info('runs')").fetchall()
            }
            dataset_columns = [
                name
                for name in (
                    "dataset_id",
                    "execution_dataset_id",
                    "signal_raw_dataset_id",
                    "signal_factor_dataset_id",
                    "universe_dataset_id",
                    "signal_supplement_factor_dataset_id",
                )
                if name in columns
            ]
            if dataset_columns and "status" in columns:
                placeholders = ",".join("?" for _ in _LIVE_STATUSES)
                sql = (
                    f"SELECT {','.join(dataset_columns)} FROM runs "
                    f"WHERE lower(COALESCE(status,'')) IN ({placeholders})"
                )
                for row in conn.execute(sql, sorted(_LIVE_STATUSES)):
                    for name in dataset_columns:
                        if row[name]:
                            result["dataset_ids"].add(str(row[name]))

        if "experiment_variants" in tables:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info('experiment_variants')"
                ).fetchall()
            }
            if {"status", "params_json"}.issubset(columns):
                placeholders = ",".join("?" for _ in _LIVE_STATUSES)
                sql = (
                    "SELECT params_json FROM experiment_variants "
                    f"WHERE lower(COALESCE(status,'')) IN ({placeholders})"
                )
                for row in conn.execute(sql, sorted(_LIVE_STATUSES)):
                    try:
                        payload = json.loads(row[0] or "{}")
                    except Exception:
                        continue
                    result["dataset_ids"].update(_json_dataset_ids(payload))
    except Exception as exc:
        result["warnings"].append(
            f"run reference scan failed: {type(exc).__name__}: {exc}"
        )
    finally:
        conn.close()
    return result


def _manifest_blob_shas(manifest: DatasetManifest) -> Set[str]:
    return {
        str(record.blob_sha256)
        for record in manifest.symbols
        if record.blob_sha256
    }


def _legacy_family(manifest: DatasetManifest) -> Optional[str]:
    key = (
        str(manifest.source or ""),
        str(manifest.adjustment or ""),
        str(manifest.period or ""),
    )
    if (
        manifest.storage_mode == "overlay_v1"
        or key not in _LEGACY_DAILY_FAMILIES
    ):
        return None
    return "/".join(key)


def _manifest_timestamp(
    store: DatasetStore,
    manifest_id: str,
    manifest: DatasetManifest,
) -> Optional[dt.datetime]:
    parsed = _parse_time(manifest.created_at)
    if parsed is not None:
        return parsed
    try:
        return dt.datetime.fromtimestamp(
            (store.manifests_dir / f"{manifest_id}.json").stat().st_mtime
        )
    except OSError:
        return None


def _latest_complete_family_members(
    family_members: Dict[str, list[tuple[str, DatasetManifest]]],
    keep_per_family: int,
) -> Dict[str, list[str]]:
    """Choose full-market rollback snapshots without preferring tiny test sets."""
    selected: Dict[str, list[str]] = {}
    for family, members in family_members.items():
        ready = [
            item
            for item in members
            if item[1].status == "ready" and int(item[1].row_count or 0) > 0
        ]
        pool = ready or members
        max_symbols = max(
            (int(manifest.symbol_count or 0) for _, manifest in pool),
            default=0,
        )
        full_threshold = max(1, int(max_symbols * 0.8))
        complete = [
            item
            for item in pool
            if int(item[1].symbol_count or 0) >= full_threshold
        ]
        ranked = complete or pool
        ranked.sort(
            key=lambda item: (
                int(item[1].data_cutoff_date or 0),
                int(item[1].snapshot_date or 0),
                str(item[1].created_at or ""),
                int(item[1].row_count or 0),
                item[0],
            ),
            reverse=True,
        )
        selected[family] = [
            manifest_id
            for manifest_id, _ in ranked[: max(0, keep_per_family)]
        ]
    return selected


def _dependency_closure(
    manifests: Dict[str, DatasetManifest],
    roots: Iterable[str],
) -> tuple[Set[str], Dict[str, Set[str]]]:
    protected = {
        str(manifest_id)
        for manifest_id in roots
        if manifest_id and str(manifest_id) in manifests
    }
    dependency_sources: Dict[str, Set[str]] = {}
    stack = list(protected)
    while stack:
        manifest_id = stack.pop()
        manifest = manifests.get(manifest_id)
        if manifest is None:
            continue
        for dependency in manifest_runtime_dependencies(manifest):
            if dependency not in manifests:
                continue
            dependency_sources.setdefault(dependency, set()).add(manifest_id)
            if dependency not in protected:
                protected.add(dependency)
                stack.append(dependency)
    return protected, dependency_sources


def _generation_manifests(
    store: DatasetStore,
    catalog: dict,
) -> tuple[Dict[str, Set[str]], Dict[str, DatasetManifest]]:
    manifests: Dict[str, DatasetManifest] = {}
    for manifest_id in store.list_manifests():
        manifest = store.load_manifest(manifest_id, deep_copy=False)
        if manifest is not None:
            manifests[manifest_id] = manifest

    by_generation: Dict[str, Set[str]] = {}
    for generation_id, record in catalog.get("generations", {}).items():
        owned = generation_manifest_ids(record)
        store_id = str(record.get("delta_store_id") or generation_id)
        for manifest_id, manifest in manifests.items():
            if (
                manifest.storage_mode == "overlay_v1"
                and manifest.delta_store_id == store_id
            ):
                owned.add(manifest_id)
        by_generation[generation_id] = owned
    return by_generation, manifests


def build_retention_plan(
    store: DatasetStore,
    *,
    pins: Optional[Dict[str, dict]] = None,
    live_dataset_ids: Optional[Iterable[str]] = None,
    keep_generations: int = DEFAULT_KEEP_GENERATIONS,
    grace_days: int = DEFAULT_GRACE_DAYS,
    now: Optional[dt.datetime] = None,
    persist_catalog: bool = True,
) -> dict:
    """Plan expiry while preserving active, rollback, pinned and live data."""
    now = now or dt.datetime.now()
    keep_generations = max(2, int(keep_generations))
    grace_days = max(0, int(grace_days))
    pins = pins or {}
    live_ids = {str(value) for value in (live_dataset_ids or []) if value}

    state = load_overlay_state(store.root)
    catalog = reconcile_generation_catalog(
        store.root,
        state,
        persist=persist_catalog,
    )
    by_generation, manifests = _generation_manifests(store, catalog)
    records = catalog.get("generations", {})

    active_ids = {
        generation_id
        for generation_id, record in records.items()
        if record.get("status") == STATUS_ACTIVE
    }
    retired = [
        (generation_id, record)
        for generation_id, record in records.items()
        if record.get("status") == STATUS_RETIRED
    ]
    retired.sort(
        key=lambda item: (
            int(item[1].get("sequence") or 0),
            str(item[1].get("retired_at") or ""),
            str(item[1].get("activated_at") or ""),
            item[0],
        ),
        reverse=True,
    )
    rollback_slots = max(0, keep_generations - len(active_ids))
    rollback_ids = {generation_id for generation_id, _ in retired[:rollback_slots]}

    keep_reasons: Dict[str, list[str]] = {}
    candidates: Set[str] = set()
    for generation_id, record in records.items():
        status = record.get("status")
        reasons: list[str] = []
        manifest_ids = by_generation.get(generation_id, set())
        if status == STATUS_EXPIRED:
            continue
        if generation_id in active_ids:
            reasons.append("active")
        if generation_id in rollback_ids:
            reasons.append("rollback_generation")
        retired_at = _parse_time(record.get("retired_at"))
        if (
            status == STATUS_RETIRED
            and retired_at is not None
            and now < retired_at + dt.timedelta(days=grace_days)
        ):
            reasons.append("grace_period")
        if (
            f"generation:{generation_id}" in pins
            or manifest_ids.intersection(pins)
        ):
            reasons.append("pinned")
        if manifest_ids.intersection(live_ids):
            reasons.append("live_task")
        if reasons:
            keep_reasons[generation_id] = reasons
        elif status == STATUS_RETIRED:
            candidates.add(generation_id)

    # If a retained manifest depends on a candidate generation, keep that
    # generation too. Iterate because keeping one old generation may in turn
    # retain an even older dependency.
    while candidates:
        candidate_manifests = set().union(
            *(by_generation.get(generation_id, set()) for generation_id in candidates)
        )
        blocked: Dict[str, Set[str]] = {}
        for manifest_id, manifest in manifests.items():
            if manifest_id in candidate_manifests:
                continue
            dependencies = manifest_runtime_dependencies(manifest)
            for generation_id in candidates:
                hit = dependencies.intersection(
                    by_generation.get(generation_id, set())
                )
                if hit:
                    blocked.setdefault(generation_id, set()).update(hit)
        if not blocked:
            break
        for generation_id, dependencies in blocked.items():
            candidates.discard(generation_id)
            keep_reasons.setdefault(generation_id, []).append(
                "referenced_by_retained_manifest"
            )
            keep_reasons[generation_id].extend(
                f"dependency:{value}" for value in sorted(dependencies)[:5]
            )

    expire_manifests = sorted(
        set().union(
            *(by_generation.get(generation_id, set()) for generation_id in candidates)
        )
        if candidates
        else set()
    )
    retained_manifest_ids = set(manifests) - set(expire_manifests)
    candidate_shas = set().union(
        *(
            _manifest_blob_shas(manifests[manifest_id])
            for manifest_id in expire_manifests
            if manifest_id in manifests
        )
    ) if expire_manifests else set()
    retained_shas = set().union(
        *(
            _manifest_blob_shas(manifests[manifest_id])
            for manifest_id in retained_manifest_ids
        )
    ) if retained_manifest_ids else set()
    reclaimable_shas = candidate_shas - retained_shas
    reclaimable_blob_bytes = 0
    for sha in reclaimable_shas:
        try:
            reclaimable_blob_bytes += (store.blobs_dir / f"{sha}.npz").stat().st_size
        except OSError:
            pass

    delta_files = []
    delta_bytes = 0
    for generation_id in sorted(candidates):
        record = records[generation_id]
        delta = DeltaStore(
            store.root,
            str(record.get("delta_store_id") or generation_id),
        )
        delta_files.append(str(delta.db_path))
        delta_bytes += delta.db_file_size()

    return {
        "keep_generations": keep_generations,
        "grace_days": grace_days,
        "active_generation_ids": sorted(active_ids),
        "rollback_generation_ids": sorted(rollback_ids),
        "keep_reasons": keep_reasons,
        "expire_generation_ids": sorted(candidates),
        "expire_manifest_ids": expire_manifests,
        "delete_delta_files": delta_files,
        "estimated_reclaimable_blob_bytes": reclaimable_blob_bytes,
        "estimated_reclaimable_delta_bytes": delta_bytes,
        "live_dataset_ids": sorted(live_ids),
        "catalog": catalog,
    }


def build_legacy_manifest_retention_plan(
    store: DatasetStore,
    *,
    pins: Optional[Dict[str, dict]] = None,
    live_dataset_ids: Optional[Iterable[str]] = None,
    keep_per_family: int = DEFAULT_LEGACY_KEEP_PER_FAMILY,
    migration_grace_days: int = DEFAULT_LEGACY_MIGRATION_GRACE_DAYS,
    manifest_min_age_days: int = DEFAULT_LEGACY_MANIFEST_MIN_AGE_DAYS,
    enabled: bool = True,
    now: Optional[dt.datetime] = None,
    persist_catalog: bool = True,
) -> dict:
    """Plan one-time cleanup of pre-overlay full-history snapshots.

    The active overlay, current/rollback generations, one complete fallback per
    materialized family, pins, live tasks and the dependency closure of every
    retained manifest are fail-closed roots. The migration-wide grace period
    prevents the first weekly maintenance run from deleting legacy data.
    """
    now = now or dt.datetime.now()
    keep_per_family = max(1, int(keep_per_family))
    migration_grace_days = max(0, int(migration_grace_days))
    manifest_min_age_days = max(0, int(manifest_min_age_days))
    pins = pins or {}
    live_ids = {str(value) for value in (live_dataset_ids or []) if value}

    state = load_overlay_state(store.root)
    catalog = reconcile_generation_catalog(
        store.root,
        state,
        persist=persist_catalog,
    )
    manifests: Dict[str, DatasetManifest] = {}
    family_members: Dict[str, list[tuple[str, DatasetManifest]]] = {}
    for manifest_id in store.list_manifests():
        manifest = store.load_manifest(manifest_id, deep_copy=False)
        if manifest is None:
            continue
        manifests[manifest_id] = manifest
        family = _legacy_family(manifest)
        if family is not None:
            family_members.setdefault(family, []).append((manifest_id, manifest))

    keep_reasons: Dict[str, Set[str]] = {}
    protected_roots: Set[str] = set()

    def _protect(manifest_id: str, reason: str) -> None:
        if manifest_id not in manifests:
            return
        protected_roots.add(manifest_id)
        if _legacy_family(manifests[manifest_id]) is not None:
            keep_reasons.setdefault(manifest_id, set()).add(reason)

    # Non-governed manifests are retained roots. Their lineage may point into a
    # legacy family, so dependency closure below keeps the required parents.
    for manifest_id, manifest in manifests.items():
        if _legacy_family(manifest) is None:
            _protect(manifest_id, "outside_legacy_policy")

    for manifest_id in (
        state.base_dataset_id,
        state.delisted_base_dataset_id,
        state.factor_base_dataset_id,
        state.supplement_factor_base_dataset_id,
    ):
        if manifest_id:
            _protect(str(manifest_id), "active_overlay_base")

    for generation_id, record in catalog.get("generations", {}).items():
        if record.get("status") == STATUS_EXPIRED:
            continue
        for manifest_id in generation_manifest_ids(record):
            _protect(manifest_id, f"retained_generation:{generation_id}")

    fallback_ids = _latest_complete_family_members(
        family_members,
        keep_per_family,
    )
    for family, manifest_ids in fallback_ids.items():
        for manifest_id in manifest_ids:
            _protect(manifest_id, f"family_fallback:{family}")

    for manifest_id in pins:
        _protect(str(manifest_id), "pinned")
    for manifest_id in live_ids:
        _protect(manifest_id, "live_task")

    recent_cutoff = now - dt.timedelta(days=manifest_min_age_days)
    for family, members in family_members.items():
        for manifest_id, manifest in members:
            created_at = _manifest_timestamp(store, manifest_id, manifest)
            if created_at is None:
                _protect(manifest_id, "unknown_age")
            elif created_at > recent_cutoff:
                _protect(manifest_id, "manifest_min_age")

    protected, dependency_sources = _dependency_closure(
        manifests,
        protected_roots,
    )
    for manifest_id, sources in dependency_sources.items():
        if _legacy_family(manifests[manifest_id]) is None:
            continue
        keep_reasons.setdefault(manifest_id, set()).update(
            f"dependency_of:{source}" for source in sorted(sources)[:5]
        )

    target_ids = {
        manifest_id
        for members in family_members.values()
        for manifest_id, _ in members
    }
    eligible_candidates = sorted(target_ids - protected)

    blocked_reason = ""
    cleanup_eligible_at = ""
    overlay_started_at = _parse_time(state.created_at)
    if not enabled:
        blocked_reason = "legacy_cleanup_disabled"
    elif not state.enabled:
        blocked_reason = "overlay_disabled"
    elif overlay_started_at is None:
        blocked_reason = "overlay_created_at_missing"
    else:
        eligible_at = overlay_started_at + dt.timedelta(
            days=migration_grace_days
        )
        cleanup_eligible_at = eligible_at.isoformat(timespec="seconds")
        if now < eligible_at:
            blocked_reason = "migration_grace_period"

    expire_manifest_ids = [] if blocked_reason else eligible_candidates
    retained_after_cleanup = set(manifests) - set(eligible_candidates)
    candidate_shas = set().union(
        *(
            _manifest_blob_shas(manifests[manifest_id])
            for manifest_id in eligible_candidates
        )
    ) if eligible_candidates else set()
    retained_shas = set().union(
        *(
            _manifest_blob_shas(manifests[manifest_id])
            for manifest_id in retained_after_cleanup
        )
    ) if retained_after_cleanup else set()
    reclaimable_shas = candidate_shas - retained_shas
    reclaimable_blob_bytes = 0
    for sha in reclaimable_shas:
        try:
            reclaimable_blob_bytes += (
                store.blobs_dir / f"{sha}.npz"
            ).stat().st_size
        except OSError:
            pass
    reclaimable_manifest_bytes = 0
    for manifest_id in eligible_candidates:
        try:
            reclaimable_manifest_bytes += (
                store.manifests_dir / f"{manifest_id}.json"
            ).stat().st_size
        except OSError:
            pass

    return {
        "enabled": bool(enabled),
        "eligible": not blocked_reason,
        "blocked_reason": blocked_reason or None,
        "cleanup_eligible_at": cleanup_eligible_at or None,
        "keep_per_family": keep_per_family,
        "migration_grace_days": migration_grace_days,
        "manifest_min_age_days": manifest_min_age_days,
        "family_counts": {
            family: len(members)
            for family, members in sorted(family_members.items())
        },
        "family_fallback_manifest_ids": fallback_ids,
        "keep_reasons": {
            manifest_id: sorted(reasons)
            for manifest_id, reasons in sorted(keep_reasons.items())
        },
        "deferred_candidate_manifest_ids": (
            eligible_candidates if blocked_reason else []
        ),
        "expire_manifest_ids": expire_manifest_ids,
        "estimated_reclaimable_manifest_bytes": reclaimable_manifest_bytes,
        "estimated_reclaimable_blob_bytes": reclaimable_blob_bytes,
        "estimated_reclaimable_total_bytes": (
            reclaimable_manifest_bytes + reclaimable_blob_bytes
        ),
        "estimate_is_deferred": bool(blocked_reason),
        "live_dataset_ids": sorted(live_ids),
        "catalog": catalog,
    }


def apply_legacy_manifest_retention_plan(
    store: DatasetStore,
    plan: dict,
) -> dict:
    if not plan.get("eligible") and plan.get("expire_manifest_ids"):
        raise RuntimeError("blocked legacy retention plan cannot be applied")
    expired = []
    for manifest_id in plan.get("expire_manifest_ids", []):
        path = store.manifests_dir / f"{manifest_id}.json"
        try:
            path.unlink(missing_ok=True)
            expired.append(manifest_id)
        except OSError as exc:
            raise RuntimeError(
                f"failed to expire legacy manifest {manifest_id}: {exc}"
            ) from exc
    return {
        "eligible": bool(plan.get("eligible")),
        "blocked_reason": plan.get("blocked_reason"),
        "expired_manifest_ids": expired,
    }


def apply_retention_plan(store: DatasetStore, plan: dict) -> dict:
    expired_manifests = []
    for manifest_id in plan.get("expire_manifest_ids", []):
        path = store.manifests_dir / f"{manifest_id}.json"
        try:
            path.unlink(missing_ok=True)
            expired_manifests.append(manifest_id)
        except OSError as exc:
            raise RuntimeError(
                f"failed to expire manifest {manifest_id}: {exc}"
            ) from exc

    deleted_delta_files = []
    for raw_path in plan.get("delete_delta_files", []):
        path = Path(raw_path)
        for candidate in (path, path.with_suffix(path.suffix + ".wal")):
            if candidate.exists():
                candidate.unlink()
                deleted_delta_files.append(str(candidate))

    mark_generations_expired(
        store.root,
        plan["catalog"],
        plan.get("expire_generation_ids", []),
    )
    return {
        "expired_generation_ids": plan.get("expire_generation_ids", []),
        "expired_manifest_ids": expired_manifests,
        "deleted_delta_files": deleted_delta_files,
    }
