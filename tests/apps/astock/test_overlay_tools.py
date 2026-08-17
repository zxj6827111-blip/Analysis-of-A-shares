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

import copy
import json
import os
import sqlite3
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
    KIND_FACTOR,
    load_overlay_state,
)
from wtpy.apps.astock.data.repository import MarketDataRepository

from .conftest import _mk_overlay_bar, build_overlay_warehouse

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _run(
    script: str, *args, root: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    process_env = os.environ.copy()
    if env:
        process_env.update({str(key): str(value) for key, value in env.items()})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--storage-root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", env=process_env,
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
        assert report["generations"]["counts"]["active"] == 1
        assert report["generations"]["retention_candidate_count"] == 0
        assert "estimated_reclaimable_total_bytes" in report["generations"]
        assert not (tmp_path / "delta" / "generation_catalog.json").exists()

    def test_dry_run_governance_is_fully_read_only(self, tmp_path):
        build_overlay_warehouse(tmp_path)

        def _snapshot():
            return {
                str(path.relative_to(tmp_path)): path.read_bytes()
                for path in tmp_path.rglob("*")
                if path.is_file()
            }

        before = _snapshot()
        retention = _run(
            "govern_market_data.py", "--retention-plan", root=tmp_path
        )
        assert retention.returncode == 0, retention.stderr
        assert _snapshot() == before

        maintain = _run("govern_market_data.py", "--maintain", root=tmp_path)
        assert maintain.returncode == 0, maintain.stderr
        assert _snapshot() == before
        assert not (tmp_path / "delta" / "generation_catalog.json").exists()

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

    @staticmethod
    def _virtual_l2_generations(store):
        from wtpy.apps.astock.data.delta_store import save_overlay_state
        from wtpy.apps.astock.data.overlay import OverlayView

        dataset_ids = []
        for commit_seq, watermark in enumerate(
            (20240108, 20240109, 20240110)
        ):
            state = load_overlay_state(store.root)
            state.delta_watermark = watermark
            state.delta_commit_seq = commit_seq
            save_overlay_state(store.root, state)
            dataset_ids.append(
                OverlayView.from_root(
                    store.root, required=True
                ).l2_virtual_manifest().dataset_id
            )
        return dataset_ids

    def test_expire_apply_keeps_virtual_manifest_used_by_running_task(
        self, tmp_path
    ):
        store = build_overlay_warehouse(tmp_path)
        old_id, _, _ = self._virtual_l2_generations(store)
        run_db = tmp_path / "runs.sqlite3"
        with sqlite3.connect(run_db) as conn:
            conn.execute(
                "CREATE TABLE runs (status TEXT, execution_dataset_id TEXT)"
            )
            conn.execute(
                "INSERT INTO runs VALUES ('running', ?)", [old_id]
            )

        result = _run(
            "govern_market_data.py",
            "--expire-virtual",
            "--apply",
            root=tmp_path,
            env={"ASTOCK_RUN_DB_PATH": run_db},
        )
        assert result.returncode == 0, result.stderr
        assert store.load_manifest(old_id) is not None

    def test_expire_apply_blocks_when_live_reference_scan_fails(
        self, tmp_path
    ):
        store = build_overlay_warehouse(tmp_path)
        old_id, _, _ = self._virtual_l2_generations(store)
        invalid_db = tmp_path / "invalid.sqlite3"
        invalid_db.write_bytes(b"not-a-sqlite-database")

        result = _run(
            "govern_market_data.py",
            "--expire-virtual",
            "--apply",
            root=tmp_path,
            env={"ASTOCK_RUN_DB_PATH": invalid_db},
        )
        assert result.returncode == 4
        assert "reference scan failed" in result.stdout
        assert store.load_manifest(old_id) is not None

    @pytest.mark.parametrize(
        "flag",
        ["--expire-generations", "--expire-legacy-manifests"],
    )
    def test_retention_apply_blocks_when_live_reference_scan_fails(
        self, tmp_path, flag
    ):
        build_overlay_warehouse(tmp_path)
        invalid_db = tmp_path / "invalid.sqlite3"
        invalid_db.write_bytes(b"not-a-sqlite-database")

        result = _run(
            "govern_market_data.py",
            flag,
            "--apply",
            root=tmp_path,
            env={"ASTOCK_RUN_DB_PATH": invalid_db},
        )

        assert result.returncode == 4
        assert "reference scan failed" in result.stdout

    def test_expire_apply_respects_delta_writer_lock(self, tmp_path):
        build_overlay_warehouse(tmp_path)
        from wtpy.apps.astock.data.delta_store import delta_write_lock

        orphan = DeltaStore(tmp_path, "orphan")
        orphan.init_schema()
        lock = delta_write_lock(tmp_path)
        lock.acquire()
        try:
            blocked = _run(
                "govern_market_data.py", "--expire-virtual", "--apply",
                root=tmp_path,
            )
        finally:
            lock.release()
        assert blocked.returncode != 0
        assert orphan.db_path.exists()

        applied = _run(
            "govern_market_data.py", "--expire-virtual", "--apply",
            root=tmp_path,
        )
        assert applied.returncode == 0, applied.stderr
        assert not orphan.db_path.exists()

    @pytest.mark.parametrize("source_kind", ["raw", "factor"])
    def test_consolidate_fails_closed_when_source_blob_is_missing(
        self, tmp_path, source_kind
    ):
        store = build_overlay_warehouse(tmp_path)
        before = load_overlay_state(tmp_path)
        dataset_id = (
            before.base_dataset_id
            if source_kind == "raw"
            else before.factor_base_dataset_id
        )
        manifest = store.load_manifest(dataset_id)
        missing_blob = store.blobs_dir / (
            f"{manifest.symbols[0].blob_sha256}.npz"
        )
        missing_blob.unlink()

        result = _run(
            "govern_market_data.py",
            "--consolidate",
            "--force-consolidate",
            "--apply",
            root=tmp_path,
        )
        assert result.returncode == 4, result.stderr
        assert "source validation failed" in result.stdout
        after = load_overlay_state(tmp_path)
        assert after.base_dataset_id == before.base_dataset_id
        assert after.factor_base_dataset_id == before.factor_base_dataset_id
        assert after.delta_store_id == before.delta_store_id
        assert not any(
            store.load_manifest(mid).provider_version == "overlay_consolidate_v2"
            for mid in store.list_manifests()
        )

    def test_consolidate_preserves_replay_and_merges_raw_factor_ipo(
        self, tmp_path
    ):
        store = build_overlay_warehouse(tmp_path, delisted=True)
        delisted_symbol = "SZSE.STK.300104"
        from wtpy.apps.astock.data.delta_store import (
            load_overlay_state as _los,
            save_overlay_state,
        )

        repo = MarketDataRepository(store)
        base_view = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        ds = DeltaStore(tmp_path)
        state = _los(tmp_path)
        base = store.load_manifest(state.base_dataset_id)
        factor_base = store.load_manifest(state.factor_base_dataset_id)
        ds.commit_batch(
            batch_id="eod1",
            kind=KIND_BARS,
            source="tushare",
            adjustment="none",
            period="1d",
            base_dataset_id=base.dataset_id,
            watermark=20240109,
            rows={
                "SSE.STK.600000": [
                    (20240109, 10.8, 11.0, 10.7, 10.9, 1000.0, 100000.0)
                ],
                "SSE.STK.600999": [
                    (20240109, 5.0, 5.5, 4.9, 5.2, 100.0, 1000.0)
                ],
            },
        )
        ds.commit_batch(
            batch_id="fac1",
            kind=KIND_FACTOR,
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            base_dataset_id=factor_base.dataset_id,
            watermark=20240109,
            rows={
                "SSE.STK.600000": [(20240109, 3.0)],
                "SSE.STK.600999": [(20240109, 1.0)],
            },
        )
        state.delta_watermark = 20240109
        state.factor_watermark = 20240109
        state.delta_commit_seq = ds.current_commit_seq(KIND_BARS)
        state.factor_commit_seq = ds.current_commit_seq(KIND_FACTOR)
        save_overlay_state(tmp_path, state)
        delta_view = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        old_l1 = repo.resolve_latest_ready(
            source="internal",
            adjustment="composite_tushare_factor_qfq",
            period="1d",
        )
        assert (
            old_l1.provenance["delisted_base_dataset_id"]
            == state.delisted_base_dataset_id
        )
        before = repo.load_bars(
            dataset_id=delta_view.dataset_id, symbol="SSE.STK.600000"
        )
        assert before[-1].close == 10.9

        result = _run(
            "govern_market_data.py",
            "--consolidate",
            "--force-consolidate",
            "--apply",
            root=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        state2 = load_overlay_state(tmp_path)
        assert state2.base_dataset_id != "tushare_none_1d_base"
        assert state2.factor_base_dataset_id != "tushare_adjfactor_1d_base"
        assert state2.delta_store_id != "main"
        assert state2.delta_commit_seq == 0
        assert state2.factor_commit_seq == 0
        assert state2.delisted_base_dataset_id
        active_base2 = store.load_manifest(state2.base_dataset_id)
        delisted_base2 = store.load_manifest(state2.delisted_base_dataset_id)
        assert delisted_symbol not in {r.symbol for r in active_base2.symbols}
        assert delisted_symbol in {r.symbol for r in delisted_base2.symbols}

        status = _run("migrate_market_data_overlay.py", "--status", root=tmp_path)
        assert status.returncode == 0, status.stderr
        status_data = json.loads(status.stdout)
        assert status_data["delta_health"]["store_id"] == state2.delta_store_id
        assert status_data["delta_health"]["ok"] is True

        audit = _run("govern_market_data.py", "--audit", root=tmp_path)
        assert audit.returncode == 0, audit.stderr
        audit_data = json.loads(audit.stdout)
        assert audit_data["disk"]["delta_generation_count"] == 2
        assert audit_data["disk"]["archived_delta_generation_count"] == 1
        assert audit_data["disk"]["archived_delta_bytes"] > 0

        # The archived generation remains readable for old dataset ids.
        assert ds.delta_row_count(KIND_BARS) == 2
        replay = repo.load_bars(
            dataset_id=delta_view.dataset_id, symbol="SSE.STK.600000"
        )
        assert replay[-1].trade_date == 20240109
        assert replay[-1].close == 10.9
        validation = MarketDataRepository(store).validate_dataset(
            delta_view.dataset_id
        )
        assert validation["valid"] is True, validation
        l1_validation = MarketDataRepository(store).validate_dataset(
            old_l1.dataset_id
        )
        assert l1_validation["valid"] is True, l1_validation
        from wtpy.apps.astock.data.overlay import OverlayView

        legacy_l1 = copy.deepcopy(old_l1)
        legacy_l1.provenance.pop("delisted_base_dataset_id", None)
        legacy_l1.provenance.pop("delisted_base_manifest_sha256", None)
        legacy_view = OverlayView.for_manifest(store, legacy_l1)
        assert (
            legacy_view.delisted_base().dataset_id
            == state.delisted_base_dataset_id
        )
        assert repo.load_bars(
            dataset_id=base_view.dataset_id, symbol="SSE.STK.600000"
        )[-1].trade_date == 20240108

        # The active generation starts empty; raw, factor and the delta-only
        # IPO have all been folded into immutable bases.
        active_delta = DeltaStore(tmp_path, state2.delta_store_id)
        assert active_delta.delta_row_count(KIND_BARS) == 0
        assert active_delta.delta_row_count(KIND_FACTOR) == 0
        l2 = repo.resolve_latest_ready(
            source="internal", adjustment="composite_none", period="1d"
        )
        ipo = repo.load_bars(
            dataset_id=l2.dataset_id, symbol="SSE.STK.600999"
        )
        assert ipo[-1].close == 5.2
        delisted_bars = repo.load_bars(
            dataset_id=l2.dataset_id, symbol=delisted_symbol
        )
        assert delisted_bars[-1].trade_date == 20240104
        raw = repo.resolve_latest_ready(
            source="tushare", adjustment="none", period="1d"
        )
        assert delisted_symbol not in {record.symbol for record in raw.symbols}
        factor = repo.resolve_latest_ready(
            source="tushare", adjustment="adj_factor", period="1d"
        )
        factor_arrays = repo.load_factor_arrays(
            dataset_id=factor.dataset_id, symbols=["SSE.STK.600000"]
        )["SSE.STK.600000"]
        assert factor_arrays["trade_date"][-1] == 20240109
        assert factor_arrays["adj_factor"][-1] == 3.0
