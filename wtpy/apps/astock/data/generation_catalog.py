# -*- coding: utf-8 -*-
"""Local lifecycle registry for overlay base + delta generations.

The catalog is operational metadata, not a second source of market-data
truth. ``overlay_state.json`` remains authoritative for the active
generation; every load reconciles the catalog with that state so a crash
between the atomic overlay switch and catalog persistence self-heals.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Optional, Set

from .delta_store import DELTA_DIR_NAME, OverlayState, load_overlay_state
from .io_util import atomic_write_json

GENERATION_CATALOG_NAME = "generation_catalog.json"
GENERATION_CATALOG_VERSION = 1

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_EXPIRED = "expired"


class GenerationCatalogError(RuntimeError):
    """The generation catalog exists but is invalid."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def catalog_path(root: Path | str) -> Path:
    return Path(root) / DELTA_DIR_NAME / GENERATION_CATALOG_NAME


def _base_manifest_ids(state: OverlayState) -> list[str]:
    return [
        dataset_id
        for dataset_id in (
            state.base_dataset_id,
            state.delisted_base_dataset_id,
            state.factor_base_dataset_id,
            state.supplement_factor_base_dataset_id,
        )
        if dataset_id
    ]


def record_from_state(
    state: OverlayState,
    *,
    status: str = STATUS_ACTIVE,
    now: Optional[str] = None,
) -> dict:
    now = now or _now_iso()
    return {
        "generation_id": state.delta_store_id or "main",
        "sequence": 0,
        "status": status,
        "delta_store_id": state.delta_store_id or "main",
        "base_dataset_id": state.base_dataset_id,
        "delisted_base_dataset_id": state.delisted_base_dataset_id,
        "factor_base_dataset_id": state.factor_base_dataset_id,
        "supplement_factor_base_dataset_id": (
            state.supplement_factor_base_dataset_id
        ),
        "manifest_ids": _base_manifest_ids(state),
        "data_cutoff_date": int(state.delta_watermark or 0),
        "factor_cutoff_date": int(state.factor_watermark or 0),
        "created_at": state.created_at or now,
        "activated_at": now,
        "retired_at": None,
        "expired_at": None,
    }


def empty_catalog() -> dict:
    return {
        "schema_version": GENERATION_CATALOG_VERSION,
        "updated_at": "",
        "generations": {},
    }


def load_generation_catalog(root: Path | str) -> dict:
    path = catalog_path(root)
    if not path.exists():
        return empty_catalog()
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GenerationCatalogError(
            f"generation catalog unreadable: {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GenerationCatalogError("generation catalog root must be an object")
    generations = payload.get("generations")
    if not isinstance(generations, dict):
        raise GenerationCatalogError(
            "generation catalog generations must be an object"
        )
    payload["schema_version"] = int(
        payload.get("schema_version") or GENERATION_CATALOG_VERSION
    )
    return payload


def save_generation_catalog(root: Path | str, catalog: dict) -> Path:
    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    catalog["schema_version"] = GENERATION_CATALOG_VERSION
    catalog["updated_at"] = _now_iso()
    atomic_write_json(path, catalog)
    return path


def reconcile_generation_catalog(
    root: Path | str,
    state: Optional[OverlayState] = None,
    *,
    persist: bool = True,
) -> dict:
    """Make the catalog's active record match ``overlay_state.json``."""

    state = state or load_overlay_state(root)
    catalog = load_generation_catalog(root)
    if not state.enabled:
        return catalog

    now = _now_iso()
    generations = catalog.setdefault("generations", {})
    current_id = state.delta_store_id or "main"
    changed = False

    for generation_id, record in generations.items():
        if (
            generation_id != current_id
            and record.get("status") == STATUS_ACTIVE
        ):
            record["status"] = STATUS_RETIRED
            record["retired_at"] = record.get("retired_at") or now
            changed = True

    current = generations.get(current_id)
    if current is None:
        record = record_from_state(state, now=now)
        record["sequence"] = 1 + max(
            (
                int(item.get("sequence") or 0)
                for item in generations.values()
            ),
            default=0,
        )
        generations[current_id] = record
        changed = True
    else:
        desired = record_from_state(state, now=now)
        for key in (
            "delta_store_id",
            "base_dataset_id",
            "delisted_base_dataset_id",
            "factor_base_dataset_id",
            "supplement_factor_base_dataset_id",
            "manifest_ids",
            "data_cutoff_date",
            "factor_cutoff_date",
        ):
            if current.get(key) != desired[key]:
                current[key] = desired[key]
                changed = True
        if not current.get("sequence"):
            current["sequence"] = 1 + max(
                (
                    int(item.get("sequence") or 0)
                    for item in generations.values()
                    if item is not current
                ),
                default=0,
            )
            changed = True
        if current.get("status") != STATUS_ACTIVE:
            current["status"] = STATUS_ACTIVE
            current["activated_at"] = now
            current["retired_at"] = None
            current["expired_at"] = None
            changed = True

    if persist and (changed or not catalog_path(root).exists()):
        save_generation_catalog(root, catalog)
    return catalog


def retained_delta_store_ids(catalog: dict) -> Set[str]:
    return {
        str(record.get("delta_store_id") or generation_id)
        for generation_id, record in catalog.get("generations", {}).items()
        if record.get("status") != STATUS_EXPIRED
    }


def generation_manifest_ids(record: dict) -> Set[str]:
    ids = {
        str(value)
        for value in record.get("manifest_ids", [])
        if value
    }
    for key in (
        "base_dataset_id",
        "delisted_base_dataset_id",
        "factor_base_dataset_id",
        "supplement_factor_base_dataset_id",
    ):
        value = record.get(key)
        if value:
            ids.add(str(value))
    return ids


def mark_generations_expired(
    root: Path | str,
    catalog: dict,
    generation_ids: Iterable[str],
) -> None:
    now = _now_iso()
    for generation_id in generation_ids:
        record = catalog.get("generations", {}).get(generation_id)
        if record is None:
            continue
        record["status"] = STATUS_EXPIRED
        record["expired_at"] = now
    save_generation_catalog(root, catalog)
