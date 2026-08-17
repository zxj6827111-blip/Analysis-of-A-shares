"""Blob garbage collection for the content-addressed market-data store.

Why a careful GC:
  Blobs are content-addressed and immutable. During a sync, a blob is written
  to blobs/ BEFORE it appears in a manifest or a checkpoint, so "no manifest
  references it" is NOT by itself proof a blob is garbage — it may be the
  in-flight output of a running sync, or a just-written blob awaiting publish.
  A naive sweep therefore risks deleting live data mid-sync.

Safety model (all must hold before any blob is deleted):
  1. Global exclusive GC lock — no other GC may run concurrently.
  2. No LIVE sync task holds any SyncTaskLock — a running sync means new
     blobs may exist that are not yet manifest-referenced. Stale lock files
     (dead holder pid) do NOT block GC.
  3. Retention = blobs referenced by ALL manifests (every status: ready,
     partial, superseded, failed) plus any sha256 mentioned in checkpoint
     files under sync_logs (in-flight references).
  4. Freshness guard: blobs written within ``protection_hours`` are never
     deleted, covering the gap between blob-write and manifest-publish even
     when no sync lock is visible (e.g. crash between the two steps).
  5. Pre-delete verification: every retained manifest's referenced blobs
     already exist, and every planned candidate is still an orphan. The same
     manifest check is repeated after deletion.

CLI (see scripts/gc_market_data.py):
  --dry-run   (default) only report what would be deleted.
  --apply     actually delete.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .dataset_store import (
    DatasetStore,
    manifest_runtime_dependencies,
    validate_manifest_path,
)
from .sync_lock import SyncTaskLock, _pid_alive

#: Minimum age of a blob before GC may consider it (covers write→publish gap).
DEFAULT_PROTECTION_HOURS = 72


@dataclass
class GcCandidate:
    sha256: str
    path: Path
    size_bytes: int
    mtime: float
    reason: str = "orphan"

    @property
    def age_hours(self) -> float:
        return (time.time() - self.mtime) / 3600.0


@dataclass
class GcPlan:
    candidates: List[GcCandidate] = field(default_factory=list)
    referenced_manifest_blobs: int = 0
    referenced_checkpoint_blobs: int = 0
    disk_blob_count: int = 0
    blocked_by_live_lock: Optional[str] = None
    blocked_by_overlay_manifest_missing: Optional[str] = None
    retained_manifest_count: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by_live_lock or self.blocked_by_overlay_manifest_missing)

    def summarize(self) -> dict:
        return {
            "disk_blob_count": self.disk_blob_count,
            "retained_manifest_count": self.retained_manifest_count,
            "referenced_manifest_blobs": self.referenced_manifest_blobs,
            "referenced_checkpoint_blobs": self.referenced_checkpoint_blobs,
            "candidate_count": len(self.candidates),
            "reclaimable_bytes": self.reclaimable_bytes,
            "reclaimable_gib": round(self.reclaimable_bytes / 1024**3, 4),
            "blocked_by_live_lock": self.blocked_by_live_lock,
            "blocked_by_overlay_manifest_missing": (
                self.blocked_by_overlay_manifest_missing
            ),
            "warnings": self.warnings,
        }


def _extract_manifest_blob_shas(manifest: dict) -> Set[str]:
    """All blob shas a single manifest references (symbols + top-level lineage)."""
    out: Set[str] = set()
    for sym in manifest.get("symbols", []):
        sha = sym.get("blob_sha256") or sym.get("blob") or ""
        if sha:
            out.add(sha)
    for k in (
        "blob_sha256",
        "raw_blob_sha256",
        "factor_blob_sha256",
        "content_hash",
    ):
        v = manifest.get(k)
        if isinstance(v, str) and v:
            out.add(v)
    return out


def _extract_shas_from_object(obj, out: Set[str]) -> None:
    """Recursively collect 64-hex sha256-looking strings from arbitrary JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (
                isinstance(v, str)
                and len(v) == 64
                and all(c in "0123456789abcdef" for c in v)
            ):
                out.add(v)
            elif isinstance(v, (dict, list)):
                _extract_shas_from_object(v, out)
    elif isinstance(obj, list):
        for it in obj:
            _extract_shas_from_object(it, out)


#: A sync task that acquired a lock longer ago than this is stale even if
#: the recorded pid happens to be alive again (pid reuse on Windows). No
#: legitimate sync chain runs longer than a few hours.
MAX_LOCK_AGE_HOURS = 24


def _parse_lock_age_hours(meta: dict) -> Optional[float]:
    """Hours since the lock's recorded start_time. None when unparseable."""
    st = str(meta.get("start_time") or "")
    if not st:
        return None
    try:
        import datetime

        dt = datetime.datetime.strptime(st, "%Y-%m-%dT%H:%M:%S")
        return (datetime.datetime.now() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _live_sync_lock_holder(store: DatasetStore) -> Optional[str]:
    """Return a description of a live holder of any sync lock, else None.

    A lock is treated as LIVE only when BOTH hold:
      - its recorded pid is alive right now, AND
      - it was acquired within MAX_LOCK_AGE_HOURS (or no start_time recorded).
    Stale lock files — dead pid, pid reuse, or acquired long ago with no
    released_at marker — are ignored; the OS-level byte lock died with the
    original process, so no sync is actually running.
    """
    lock_dir = store.root / ".locks"
    if not lock_dir.is_dir():
        return None
    for lock_path in sorted(lock_dir.glob("sync_*.lock")):
        if lock_path.name.startswith("sync_gc_blob_"):
            continue  # our own GC lock
        meta = SyncTaskLock.probe(lock_path)
        if not meta:
            continue
        if meta.get("released_at"):
            continue  # cleanly released
        age_h = _parse_lock_age_hours(meta)
        if age_h is not None and age_h > MAX_LOCK_AGE_HOURS:
            continue  # acquired too long ago to still be running
        pid = int(meta.get("pid") or -1)
        alive = _pid_alive(pid)
        if alive is True:
            return (
                f"{lock_path.name} held by pid={pid} "
                f"host={meta.get('hostname')} sync_run_id={meta.get('sync_run_id')} "
                f"age_hours={age_h}"
            )
    return None


def _checkpoint_blob_shas(store: DatasetStore) -> Set[str]:
    """sha256s mentioned in checkpoint/sync files under sync_logs.

    Checkpoints persist the per-symbol done map across resumes; a sha in a
    checkpoint is an in-flight reference even though no manifest may exist
    yet. Any *.json under sync_logs is scanned conservatively.
    """
    out: Set[str] = set()
    logs = store.sync_logs_dir
    if not logs.is_dir():
        return out
    for p in sorted(logs.glob("*.json")):
        if p.name.startswith(("checkpoint_", "tsfactor_", "tushare_", "localvendor_")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                _extract_shas_from_object(data, out)
            except Exception:
                continue
    return out


def _overlay_referenced_manifests(store: DatasetStore) -> Dict[str, str]:
    """Overlay base dataset ids mapped to their pinned manifest hashes.

    An existing but corrupt overlay registry raises; callers must fail closed.
    """
    from .delta_store import load_overlay_state

    state_path = store.root / "delta" / "overlay_state.json"
    if not state_path.exists():
        return {}
    state = load_overlay_state(store.root)
    pairs = (
        (state.base_dataset_id, state.base_manifest_sha256),
        (state.delisted_base_dataset_id, state.delisted_base_manifest_sha256),
        (state.factor_base_dataset_id, state.factor_base_manifest_sha256),
        (
            state.supplement_factor_base_dataset_id,
            state.supplement_factor_base_manifest_sha256,
        ),
    )
    return {dataset_id: sha for dataset_id, sha in pairs if dataset_id}


def build_gc_plan(
    store: DatasetStore,
    *,
    protection_hours: int = DEFAULT_PROTECTION_HOURS,
    respect_live_locks: bool = True,
) -> GcPlan:
    """Compute the set of blobs eligible for deletion (no mutation)."""
    plan = GcPlan()

    # Live-lock guard: refuse to plan while any sync task is actually running.
    if respect_live_locks:
        holder = _live_sync_lock_holder(store)
        if holder:
            plan.blocked_by_live_lock = holder
            return plan

    # 1. All manifests (every status) retain their own blobs. Runtime
    #    dependencies are checked separately; build-only parent lineage is
    #    intentionally excluded so legacy snapshots can be retired.
    retained_manifest_blobs: Set[str] = set()
    manifest_ids = store.list_manifests()
    plan.retained_manifest_count = len(manifest_ids)
    lineage_ids: Set[str] = set()
    for mid in manifest_ids:
        m = store.load_manifest(mid, deep_copy=False)
        if m is None:
            continue
        retained_manifest_blobs |= _extract_manifest_blob_shas(m.to_dict())
        lineage_ids.update(manifest_runtime_dependencies(m))
    plan.referenced_manifest_blobs = len(retained_manifest_blobs)

    # Defensive: runtime dependency ids with no manifest on disk are anomalies.
    # Overlay base references: keep every blob of the overlay-registered base
    # datasets even when their virtual views have been expired. A MISSING base
    # is a hard block: GC must never run against a broken overlay baseline
    # (the missing manifest means we cannot enumerate its blobs, and
    # collecting them as orphans would corrupt the whole overlay surface) —
    # mirroring the live-lock guard, the plan is returned empty.
    try:
        overlay_refs = _overlay_referenced_manifests(store)
    except Exception as exc:
        plan.blocked_by_overlay_manifest_missing = f"invalid_overlay_state:{exc}"
        plan.warnings.append(f"overlay state invalid; GC refused: {exc}")
        return plan
    for oid, expected_sha in sorted(overlay_refs.items()):
        om = store.load_manifest(oid, deep_copy=False)
        if om is None:
            plan.blocked_by_overlay_manifest_missing = oid
            plan.warnings.append(
                f"overlay state references missing base dataset: {oid} "
                f"(GC refused)"
            )
            return plan
        if (
            expected_sha
            and (
                om.manifest_sha256 != expected_sha
                or not validate_manifest_path(
                    store.manifests_dir / f"{oid}.json",
                    expected_sha256=expected_sha,
                )
            )
        ):
            plan.blocked_by_overlay_manifest_missing = f"sha_mismatch:{oid}"
            plan.warnings.append(
                f"overlay base manifest hash invalid: {oid} (GC refused)"
            )
            return plan
        retained_manifest_blobs |= _extract_manifest_blob_shas(om.to_dict())
        lineage_ids.add(oid)
    known_dependency_ids = set(manifest_ids)
    universe_dir = store.root / "universes"
    if universe_dir.is_dir():
        known_dependency_ids.update(
            path.stem for path in universe_dir.glob("*.json")
        )
    missing_lineage = sorted(lineage_ids - known_dependency_ids)
    if missing_lineage:
        plan.warnings.append(
            "manifests reference missing runtime dependencies: "
            f"{missing_lineage[:5]}"
        )

    # 2. Checkpoint / in-flight references.
    ckpt = _checkpoint_blob_shas(store)
    plan.referenced_checkpoint_blobs = len(ckpt)
    retained = retained_manifest_blobs | ckpt

    # 3. Disk blob set.
    disk_shas = store.blob_sha_set()
    plan.disk_blob_count = len(disk_shas)

    # 4. Candidates = disk - retained, filtered by freshness guard.
    now = time.time()
    cutoff_age = protection_hours * 3600.0
    for sha in sorted(disk_shas - retained):
        p = store.blobs_dir / f"{sha}.npz"
        try:
            st = p.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        if age < cutoff_age:
            continue  # too fresh — may be pre-publish in-flight blob
        plan.candidates.append(
            GcCandidate(
                sha256=sha,
                path=p,
                size_bytes=st.st_size,
                mtime=st.st_mtime,
                reason="orphan",
            )
        )
    return plan


def verify_retained_manifests(store: DatasetStore) -> List[str]:
    """Re-check every manifest's blobs still exist. Returns missing symbols."""
    missing: List[str] = []
    for mid in store.list_manifests():
        m = store.load_manifest(mid, deep_copy=False)
        if m is None:
            continue
        for sym in m.symbols or []:
            if sym.blob_sha256 and not store.blob_exists(sym.blob_sha256):
                missing.append(f"{mid}:{sym.symbol}")
    return missing


def apply_gc_plan(store: DatasetStore, plan: GcPlan) -> Dict:
    """Verify the plan is still safe, delete, then verify manifests again.

    Returns a summary dict. Raises RuntimeError before deletion when retained
    data is already incomplete or a candidate became referenced after planning.
    """
    if plan.blocked:
        raise RuntimeError(f"GC plan is blocked: {plan.summarize()}")
    missing_before = verify_retained_manifests(store)
    if missing_before:
        raise RuntimeError(
            f"PRE-DELETE VERIFICATION FAILED: {len(missing_before)} manifest "
            f"blobs already missing: {missing_before[:10]}"
        )
    fresh_plan = build_gc_plan(
        store,
        protection_hours=0,
        respect_live_locks=False,
    )
    if fresh_plan.blocked:
        raise RuntimeError(
            f"GC safety recheck is blocked: {fresh_plan.summarize()}"
        )
    current_orphans = {candidate.sha256 for candidate in fresh_plan.candidates}
    stale_candidates = sorted(
        candidate.sha256
        for candidate in plan.candidates
        if candidate.path.exists() and candidate.sha256 not in current_orphans
    )
    if stale_candidates:
        raise RuntimeError(
            "GC PLAN STALE: planned candidates are now retained or protected: "
            f"{stale_candidates[:10]}"
        )
    deleted = 0
    deleted_bytes = 0
    for c in plan.candidates:
        try:
            c.path.unlink(missing_ok=True)
            deleted += 1
            deleted_bytes += c.size_bytes
        except OSError as e:
            plan.warnings.append(f"unlink failed {c.sha256}: {e}")

    missing = verify_retained_manifests(store)
    if missing:
        raise RuntimeError(
            f"POST-DELETE VERIFICATION FAILED: {len(missing)} manifest blobs "
            f"missing after GC: {missing[:10]}"
        )
    return {
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "deleted_gib": round(deleted_bytes / 1024**3, 4),
        "warnings": plan.warnings,
        "verified_manifests_ok": True,
    }
