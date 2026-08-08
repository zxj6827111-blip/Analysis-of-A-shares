# -*- coding: utf-8 -*-
"""P0-1: business failures must not exit with code 0.

sync_market_data.py's main() maps per-source results to process exit codes:
0 = all success, 1 = any source failed, 2 = warning/partial only. The UI and
schedulers rely on the code to stop showing failed syncs as done.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"

_MODULE = None


def _script():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "sync_exit_codes_test", SYNC_SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


class TestMainExitCodes:
    def _run_main(self, tmp_path, monkeypatch, argv, sync_fn):
        smd = _script()
        monkeypatch.setattr(smd, "get_storage_root", lambda: tmp_path / "md")
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full", sync_fn)
        monkeypatch.setattr(
            "sys.argv", ["sync_market_data.py"] + argv)
        return smd.main()

    def test_failed_source_returns_1(self, tmp_path, monkeypatch):
        """A failed source (e.g. missing universe file) must exit 1."""
        code = self._run_main(
            tmp_path, monkeypatch,
            argv=["--source", "tushare", "--adjustment", "adj_factor",
                  "--mode", "incremental"],
            sync_fn=lambda args, store: {
                "status": "failed", "error": "universe_file_required"},
        )
        assert code == 1

    def test_warning_source_returns_2(self, tmp_path, monkeypatch):
        code = self._run_main(
            tmp_path, monkeypatch,
            argv=["--source", "tushare", "--adjustment", "adj_factor",
                  "--mode", "incremental"],
            sync_fn=lambda args, store: {
                "status": "warning", "warning": "reconcile blocked"},
        )
        assert code == 2

    def test_partial_source_returns_2(self, tmp_path, monkeypatch):
        code = self._run_main(
            tmp_path, monkeypatch,
            argv=["--source", "tushare", "--adjustment", "adj_factor",
                  "--mode", "incremental"],
            sync_fn=lambda args, store: {"status": "partial"},
        )
        assert code == 2

    def test_all_success_returns_0(self, tmp_path, monkeypatch):
        code = self._run_main(
            tmp_path, monkeypatch,
            argv=["--source", "tushare", "--adjustment", "adj_factor",
                  "--mode", "incremental"],
            sync_fn=lambda args, store: {"status": "success",
                                         "dataset_status": "ready"},
        )
        assert code == 0

    def test_exit_code_helper_precedence(self, tmp_path, monkeypatch):
        """failed wins over partial; partial wins over success."""
        smd = _script()
        assert smd._exit_code_for_results(
            {"a": {"status": "success"}, "b": {"status": "failed"}}) == 1
        assert smd._exit_code_for_results(
            {"a": {"status": "success"}, "b": {"status": "partial"}}) == 2
        assert smd._exit_code_for_results(
            {"a": {"status": "success"}, "b": {"status": "warning"}}) == 2
        assert smd._exit_code_for_results(
            {"a": {"status": "success"}, "b": {"status": "ready"}}) == 0
        assert smd._exit_code_for_results({}) == 0

    def test_exit_code_consumes_dataset_status(self, tmp_path, monkeypatch):
        """A success top-level with a partial/building/failed dataset_status
        (e.g. factor freshness gate demotion) is not a success."""
        smd = _script()
        assert smd._exit_code_for_results(
            {"a": {"status": "success", "dataset_status": "partial"}}) == 2
        assert smd._exit_code_for_results(
            {"a": {"status": "success", "dataset_status": "building"}}) == 2
        assert smd._exit_code_for_results(
            {"a": {"status": "success", "dataset_status": "failed"}}) == 1
        assert smd._exit_code_for_results(
            {"a": {"status": "success", "dataset_status": "ready"}}) == 0
        assert smd._exit_code_for_results(
            {"a": {"status": "success", "dataset_status": None}}) == 0

    def test_main_returns_2_for_success_with_partial_dataset(
            self, tmp_path, monkeypatch):
        """Factor sync demoted by the freshness gate
        ({"status": "success", "dataset_status": "partial"}) -> exit 2."""
        smd = _script()
        monkeypatch.setattr(smd, "get_storage_root", lambda: tmp_path / "md")
        monkeypatch.setattr(
            smd, "sync_tushare_adj_factor_full",
            lambda args, store: {"status": "success",
                                 "dataset_status": "partial",
                                 "freshness": {"fresh_symbol_ratio": 0.5}})
        monkeypatch.setattr(
            "sys.argv", ["sync_market_data.py", "--source", "tushare",
                         "--adjustment", "adj_factor", "--mode", "incremental"])
        assert smd.main() == 2

    def test_derive_partial_returns_2(self, tmp_path, monkeypatch):
        """derive success with a partial dataset_status -> exit 2."""
        smd = _script()
        monkeypatch.setattr(smd, "get_storage_root", lambda: tmp_path / "md")
        monkeypatch.setattr(smd, "_auto_resolve_parents", lambda *a, **k: None)
        monkeypatch.setattr(
            smd, "derive_tushare_factor_qfq",
            lambda args, store: {"status": "success",
                                 "dataset_status": "partial"})
        monkeypatch.setattr(
            "sys.argv", ["sync_market_data.py", "--source", "internal",
                         "--mode", "derive", "--adjustment",
                         "tushare_factor_qfq",
                         "--raw-dataset-id", "r", "--factor-dataset-id", "f"])
        assert smd.main() == 2

    def test_preflight_missing_storage_root_returns_1(self, tmp_path, monkeypatch):
        smd = _script()
        monkeypatch.setattr(smd, "get_storage_root",
                            lambda: tmp_path / "does_not_exist")
        monkeypatch.setattr(
            "sys.argv", ["sync_market_data.py", "--preflight",
                         "--source", "tushare"])
        assert smd.main() == 1

    def test_source_all_prints_tushare_only_note(
            self, tmp_path, monkeypatch, capsys):
        """--source all (now Tushare-only by policy) must print an explicit
        note so TDX / local_vendor users are not silently skipped."""
        smd = _script()
        monkeypatch.setattr(smd, "get_storage_root", lambda: tmp_path / "md")
        monkeypatch.setattr(
            smd, "sync_tushare_full",
            lambda args, store: {"status": "success", "datasets": {}})
        monkeypatch.setattr(
            smd, "_reconcile_after_sync",
            lambda store, dry_run=False: {"status": "up_to_date"})
        monkeypatch.setattr(
            "sys.argv", ["sync_market_data.py", "--source", "all",
                         "--mode", "full"])
        code = smd.main()
        assert code == 0
        out = capsys.readouterr().out
        assert "Tushare-only" in out
        assert "TDX/local_vendor require explicit --source" in out


class TestSubprocessExitCode:
    def test_business_failure_exits_nonzero(self, tmp_path):
        """No --universe-file -> universe_file_required -> exit code 1
        (was 0 before the fix). Requires no network and no real data."""
        env = dict(os.environ)
        env["MARKET_DATA_ROOT"] = str(tmp_path / "md")
        env["TUSHARE_FACTOR_RAW_ROOT"] = str(tmp_path / "raw")
        r = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT),
             "--source", "tushare", "--adjustment", "adj_factor",
             "--mode", "incremental",
             "--storage-root", str(tmp_path / "md")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=180, encoding="utf-8", errors="replace",
        )
        assert r.returncode != 0, r.stdout[-2000:]
        assert "SYNC STATUS: failed" in r.stdout
