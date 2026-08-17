"""Shared import bootstrap for astock tests (avoid heavy wtpy DLL imports)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _restore_environ_after_test():
    """Roll back every os.environ mutation after each test.

    Script-entry tests call main(), which loads the machine-local .env
    (MARKET_DATA_ROOT / TUSHARE_* / ASTOCK_ENV). Without a rollback those
    values leak into later tests in the same process, redirecting
    get_default_config() and AStockConfig to the PRODUCTION data root and
    writing test manifests into the real external disk.
    """
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _ensure_pkg(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    m = ModuleType(name)
    m.__path__ = [str(path)]  # type: ignore[attr-defined]
    m.__package__ = name
    sys.modules[name] = m


def bootstrap() -> None:
    _ensure_pkg("wtpy", ROOT / "wtpy")
    _ensure_pkg("wtpy.apps", ROOT / "wtpy" / "apps")
    _ensure_pkg("wtpy.apps.astock", ROOT / "wtpy" / "apps" / "astock")
    _ensure_pkg("wtpy.apps.astock.data", ROOT / "wtpy" / "apps" / "astock" / "data")
    _ensure_pkg(
        "wtpy.apps.astock.indicators", ROOT / "wtpy" / "apps" / "astock" / "indicators"
    )
    _ensure_pkg("wtpy.apps.astock.bagua", ROOT / "wtpy" / "apps" / "astock" / "bagua")


bootstrap()


# ---------------------------------------------------------------------------
# overlay / delta storage fixtures (overlay_v1 改造)
# ---------------------------------------------------------------------------


def _mk_overlay_bar(symbol: str, d: int, base: float):
    from wtpy.apps.astock.data.providers.base import MarketBar

    return MarketBar(
        symbol=symbol, trade_date=d, period="1d",
        open=base, high=base + 0.5, low=base - 0.5, close=base + 0.2,
        volume=1000.0, amount=100000.0, source="tushare", adjustment="none",
    )


OVERLAY_BASE_DATES = [20240101, 20240102, 20240103, 20240104, 20240105, 20240108]


def build_overlay_warehouse(root, *, delisted: bool = False) -> "DatasetStore":
    """Create a store with base raw + factor datasets and overlay enabled."""
    from wtpy.apps.astock.data.dataset_store import (
        DatasetManifest,
        DatasetStore,
        SymbolRecord,
    )
    from wtpy.apps.astock.data.delta_store import (
        OverlayState,
        save_overlay_state,
    )

    store = DatasetStore(root)
    sym_specs = {
        "SSE.STK.600000": 10.0,
        "SZSE.STK.000001": 5.0,
        "SSE.STK.601088": 20.0,
    }
    base_recs = {}
    for sym, base in sym_specs.items():
        bars = [_mk_overlay_bar(sym, d, base) for d in OVERLAY_BASE_DATES]
        sha = store.store_bars(sym, bars)
        base_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=OVERLAY_BASE_DATES[0],
            last_date=OVERLAY_BASE_DATES[-1], row_count=len(bars), quality="ok",
        )
    base = DatasetManifest(
        dataset_id="tushare_none_1d_base", source="tushare", adjustment="none",
        period="1d", data_cutoff_date=OVERLAY_BASE_DATES[-1],
        snapshot_date=OVERLAY_BASE_DATES[-1], provider_version="test",
        status="ready", created_at="2024-01-08T18:00:00",
    )
    base.symbols = list(base_recs.values())
    base.symbol_count = len(base_recs)
    base.row_count = sum(r.row_count for r in base_recs.values())
    base.expected_symbol_count = len(base_recs)
    base.imported_symbol_count = len(base_recs)
    base.coverage_ratio = 1.0
    store.publish(base)

    fac_recs = {}
    for sym in sym_specs:
        sha = store.store_factors(sym, [20230101, 20240101], [1.0, 1.5])
        fac_recs[sym] = SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=20230101,
            last_date=20240101, row_count=2, quality="ok",
        )
    fac = DatasetManifest(
        dataset_id="tushare_adjfactor_1d_base", source="tushare",
        adjustment="adj_factor", period="1d", dataset_type="factor",
        data_cutoff_date=OVERLAY_BASE_DATES[-1],
        snapshot_date=OVERLAY_BASE_DATES[-1], provider_version="test",
        status="ready", created_at="2024-01-08T18:05:00",
    )
    fac.symbols = list(fac_recs.values())
    fac.symbol_count = len(fac_recs)
    fac.row_count = 2 * len(fac_recs)
    fac.expected_symbol_count = len(fac_recs)
    fac.imported_symbol_count = len(fac_recs)
    fac.coverage_ratio = 1.0
    store.publish(fac)

    delisted_id = ""
    if delisted:
        dl_sym = "SZSE.STK.300104"
        bars = [_mk_overlay_bar(dl_sym, d, 3.0) for d in OVERLAY_BASE_DATES[:4]]
        sha = store.store_bars(dl_sym, bars)
        dl_rec = SymbolRecord(
            symbol=dl_sym, blob_sha256=sha, first_date=OVERLAY_BASE_DATES[0],
            last_date=OVERLAY_BASE_DATES[3], row_count=len(bars), quality="ok",
        )
        dl = DatasetManifest(
            dataset_id="internal_delisted_complement_base", source="internal",
            adjustment="delisted_complement", period="1d",
            data_cutoff_date=OVERLAY_BASE_DATES[3],
            snapshot_date=OVERLAY_BASE_DATES[3], provider_version="test",
            status="ready", created_at="2024-01-08T18:10:00",
        )
        dl.symbols = [dl_rec]
        dl.symbol_count = 1
        dl.row_count = len(bars)
        dl.expected_symbol_count = 1
        dl.imported_symbol_count = 1
        dl.coverage_ratio = 1.0
        store.publish(dl)
        delisted_id = dl.dataset_id

    st = OverlayState(
        enabled=True,
        base_dataset_id=base.dataset_id,
        base_manifest_sha256=base.manifest_sha256,
        delisted_base_dataset_id=delisted_id,
        delisted_base_manifest_sha256="",
        factor_base_dataset_id=fac.dataset_id,
        factor_base_manifest_sha256=fac.manifest_sha256,
        delta_watermark=OVERLAY_BASE_DATES[-1],
        factor_watermark=OVERLAY_BASE_DATES[-1],
    )
    save_overlay_state(root, st)
    return store


@pytest.fixture
def warehouse(tmp_path):
    """A ready overlay warehouse (base + factor + registry)."""
    return build_overlay_warehouse(tmp_path)


@pytest.fixture
def delta(tmp_path, warehouse):
    from wtpy.apps.astock.data.delta_store import DeltaStore

    return DeltaStore(tmp_path)


def commit_eod_delta(
    store,
    *,
    cutoff: int,
    rows: dict,
    factor_rows: dict = None,
    batch_suffix: str = "eod",
) -> dict:
    """Commit raw (+ optional factor) delta and publish the watermark."""
    from wtpy.apps.astock.data.delta_store import load_overlay_state
    from wtpy.apps.astock.data.delta_writer import DeltaEodWriter

    writer = DeltaEodWriter(store)
    base = store.load_manifest(load_overlay_state(store.root).base_dataset_id)
    batch = writer.commit_bars(
        sync_run_id=f"test_{batch_suffix}", source="tushare",
        base_dataset_id=base.dataset_id, cutoff=cutoff, rows=rows,
        batch_suffix=batch_suffix,
    )
    if factor_rows:
        fac = store.load_manifest(
            load_overlay_state(store.root).factor_base_dataset_id
        )
        writer.commit_factors(
            sync_run_id=f"test_{batch_suffix}", source="tushare",
            factor_base_dataset_id=fac.dataset_id, cutoff=cutoff,
            rows=factor_rows, batch_suffix=f"{batch_suffix}_factor",
        )
    writer.publish(
        delta_watermark=cutoff,
        factor_watermark=cutoff if factor_rows else None,
    )
    return batch
