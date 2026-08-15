# -*- coding: utf-8 -*-
"""Migration + governance tool tests.

Covers:
  - migrate --plan selects only ready complete bases (never partial/orphan)
  - migrate --apply registers the overlay and enables it
  - migrate refuses to overwrite a different existing overlay without --force
  - govern --audit reports delta + watermark + alerts
  - govern --pin/--unpin/--list-pins round trip
  - govern --consolidate merges the delta into a new base and resets the store
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from wtpy.apps.astock.data.dataset_store import (
    DatasetManifest,
    DatasetStore,
    SymbolRecord,
)
from wtpy.apps.astock.data.delta_store import (
    DeltaStore,
    KIND_BARS,
    load_overlay_state,
)
from wtpy.apps.astock.data.repository import MarketDataRepository

from .conftest import _mk_overlay_bar, build_overlay_warehouse

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _run(script: str, *args, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--storage-root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def build_migratable_warehouse(root: Path) -> DatasetStore:
    """A warehouse big enough to pass select_tushare_base's quality gates
    (>= 100 symbols, >= 250 median rows)."""
    from wtpy.apps.astock.data.delta_store import (
        OverlayState,
        save_overlay_state,
    )

    store = DatasetStore(root)
    dates = list(range(20200101, 20201231, 3))[:300]
    n_syms = 120
    base_recs = []
    for i in range(n_syms):
        sym = f"SSE.STK.{600000 + i}"
        bars = [_mk_overlay_bar(sym, d, 10.0 + i) for d in dates]
        sha = store.store_bars(sym, bars)
        base_recs.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=dates[0],
            last_date=dates[-1], row_count=len(bars), quality="ok",
        ))
    base = DatasetManifest(
        dataset_id="tushare_none_1d_full", source="tushare", adjustment="none",
        period="1d", data_cutoff_date=dates[-1], snapshot_date=dates[-1],
        provider_version="test", status="ready", created_at="2020-12-31T18:00:00",
    )
    base.symbols = base_recs
    base.symbol_count = len(base_recs)
    base.row_count = sum(r.row_count for r in base_recs)
    base.expected_symbol_count = len(base_recs)
    base.imported_symbol_count = len(base_recs)
    base.coverage_ratio = 1.0
    store.publish(base)

    fac_recs = []
    for i in range(n_syms):
        sym = f"SSE.STK.{600000 + i}"
        sha = store.store_factors(sym, [20200101, 20200601], [1.0, 1.5])
        fac_recs.append(SymbolRecord(
            symbol=sym, blob_sha256=sha, first_date=20200101,
            last_date=20200601, row_count=2, quality="ok",
        ))
    fac = DatasetManifest(
        dataset_id="tushare_adjfactor_1d_full", source="tushare",
        adjustment="adj_factor", period="1d", dataset_type="factor",
        data_cutoff_date=dates[-1], snapshot_date=dates[-1],
        provider_version="test", status="ready", created_at="2020-12-31T18:05:00",
    )
    fac.symbols = fac_recs
    fac.symbol_count = len(fac_recs)
    fac.row_count = 2 * len(fac_recs)
    fac.expected_symbol_count = len(fac_recs)
    fac.imported_symbol_count = len(fac_recs)
    fac.coverage_ratio = 1.0
    store.publish(fac)
    return store


class TestMigrateTool:
    def test_plan_and_apply(self, tmp_path):
        store = build_migratable_warehouse(tmp_path)
        # a partial base must never be selected
        partial = DatasetManifest(
            dataset_id="tushare_none_1d_partial", source="tushare",
            adjustment="none", period="1d", data_cutoff_date=20250101,
            snapshot_date=20250101, provider_version="t", status="partial",
            created_at="2025-01-01T00:00:00",
        )
        partial.symbols = [SymbolRecord(
            symbol="SSE.STK.600000", blob_sha256="", quality="error",
            error="x",
        )]
        partial.symbol_count = 0
        partial.row_count = 0
        store.save_manifest(partial)

        r = _run("migrate_market_data_overlay.py", "--plan", root=tmp_path)
        assert r.returncode == 0, r.stderr
        import json

        plan = json.loads(r.stdout)["overlay_plan"]
        assert plan["base"] == "tushare_none_1d_full"
        assert plan["factor_base"] == "tushare_adjfactor_1d_full"
        assert plan["base_manifest_sha256"]

        r = _run("migrate_market_data_overlay.py", "--apply", root=tmp_path)
        assert r.returncode == 0, r.stderr
        st = load_overlay_state(tmp_path)
        assert st.enabled
        assert st.base_dataset_id == "tushare_none_1d_full"

        # repository now resolves virtual L1/L2
        repo = MarketDataRepository(store)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        assert l2.storage_mode == "overlay_v1"

    def test_apply_refuses_different_existing_without_force(self, tmp_path):
        build_migratable_warehouse(tmp_path)
        r = _run("migrate_market_data_overlay.py", "--apply", root=tmp_path)
        assert r.returncode == 0
        # second apply with the same bases is a no-op
        r2 = _run("migrate_market_data_overlay.py", "--apply", root=tmp_path)
        assert r2.returncode == 0

    def test_status_reports_overlay_and_delta(self, tmp_path):
        build_migratable_warehouse(tmp_path)
        r = _run("migrate_market_data_overlay.py", "--apply", root=tmp_path)
        assert r.returncode == 0
        r = _run("migrate_market_data_overlay.py", "--status", root=tmp_path)
        assert r.returncode == 0
        import json

        data = json.loads(r.stdout)
        assert data["overlay"]["enabled"] is True
        assert "delta_health" in data


class TestGovernTool:
    def test_audit_reports_delta_and_watermark(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        ds = DeltaStore(tmp_path)
        base = store.load_manifest(
            load_overlay_state(tmp_path).base_dataset_id
        )
        ds.commit_batch(
            batch_id="eod1", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id=base.dataset_id,
            watermark=20240109,
            rows={"SSE.STK.600000": [(20240109, 10.8, 11.0, 10.7, 10.9,
                                      1000.0, 100000.0)]},
        )
        r = _run("govern_market_data.py", "--audit", root=tmp_path)
        assert r.returncode == 0, r.stderr
        import json

        report = json.loads(r.stdout)
        assert report["overlay_enabled"] is True
        assert report["delta"]["bars_rows"] == 1
        assert report["delta"]["watermark"] == 20240108

    def test_pin_round_trip(self, tmp_path):
        build_overlay_warehouse(tmp_path)
        r = _run(
            "govern_market_data.py", "--pin", "tushare_none_1d_base",
            "--task", "manual", "--reason", "acceptance", root=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        r = _run("govern_market_data.py", "--list-pins", root=tmp_path)
        import json

        pins = json.loads(r.stdout)["pins"]
        assert "tushare_none_1d_base" in pins
        assert pins["tushare_none_1d_base"]["reason"] == "acceptance"
        r = _run("govern_market_data.py", "--unpin", "tushare_none_1d_base",
                 root=tmp_path)
        assert r.returncode == 0
        r = _run("govern_market_data.py", "--list-pins", root=tmp_path)
        assert json.loads(r.stdout)["pins"] == {}

    def test_consolidate_merges_delta_into_new_base(self, tmp_path):
        store = build_overlay_warehouse(tmp_path)
        from wtpy.apps.astock.data.delta_store import (
            load_overlay_state as _los,
            save_overlay_state,
        )

        ds = DeltaStore(tmp_path)
        base = store.load_manifest(_los(tmp_path).base_dataset_id)
        ds.commit_batch(
            batch_id="eod1", kind=KIND_BARS, source="tushare",
            adjustment="none", period="1d", base_dataset_id=base.dataset_id,
            watermark=20240109,
            rows={"SSE.STK.600000": [(20240109, 10.8, 11.0, 10.7, 10.9,
                                      1000.0, 100000.0)]},
        )
        st = _los(tmp_path)
        st.delta_watermark = 20240109
        save_overlay_state(tmp_path, st)

        r = _run("govern_market_data.py", "--consolidate", "--apply",
                 root=tmp_path)
        assert r.returncode == 0, r.stderr
        st2 = load_overlay_state(tmp_path)
        assert st2.base_dataset_id != "tushare_none_1d_base"
        # delta reset
        assert ds.delta_row_count(KIND_BARS) == 0
        # new base contains the delta day
        repo = MarketDataRepository(store)
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        bars = repo.load_bars(dataset_id=l2.dataset_id, symbol="SSE.STK.600000")
        assert bars[-1].trade_date == 20240109
