# -*- coding: utf-8 -*-
"""Delisted-pool auto-backfill: chain integration, reconcile task, warnings.

Covers the zero-config delisted pool fix (Gate B2 ``--auto-candidates``):
- auto candidate generation from the official roster (CSV shape + idempotency)
- token-missing failure is readable (exit 1, TUSHARE_TOKEN hint)
- sync_tushare_chain runs the delisted step between factor and reconcile and
  never reports success when the step failed
- ``--source internal --mode reconcile`` maps to sync_tushare_reconcile and
  classifies waiting_for_parent as warning (exit 2)
- API-level exit-2 classification -> status "warning" (not "error")
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = ROOT / "scripts" / "sync_market_data.py"
DELISTED_SCRIPT = ROOT / "scripts" / "sync_tushare_delisted.py"

_MODULE: dict = {}
_DELISTED_MODULE: dict = {}


def _smd():
    if _MODULE.get("m") is None:
        spec = importlib.util.spec_from_file_location(
            "sync_chain_delisted_smd", SYNC_SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore[union-attr]
        _MODULE["m"] = m
    return _MODULE["m"]


def _delisted():
    if _DELISTED_MODULE.get("m") is None:
        spec = importlib.util.spec_from_file_location(
            "sync_chain_delisted_b2", DELISTED_SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # type: ignore[union-attr]
        _DELISTED_MODULE["m"] = m
    return _DELISTED_MODULE["m"]


# ---------------------------------------------------------------------------
# --auto-candidates: CSV shape + idempotency + token-missing failure
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Stands in for TushareProvider: returns a fixed delisted roster.

    Reuses the real ``_to_ts_code`` static helper so ts_code conversion is
    exactly what production uses.
    """

    import wtpy.apps.astock.data.providers.tushare as _tushare_mod

    _to_ts_code = _tushare_mod.TushareProvider._to_ts_code

    def __init__(self, token=None):
        self.token = token

    def fetch_universe(self, *, include_delisted=False):
        from wtpy.apps.astock.data.providers.base import UniverseEntry

        return [
            UniverseEntry(
                symbol="SSE.STK.600001", name="邯郸钢铁",
                list_date=19970101, delist_date=20041230, status="delisted",
                source="tushare",
            ),
            UniverseEntry(
                symbol="SZSE.STK.300104", name="乐视网",
                list_date=20100812, delist_date=20200731, status="delisted",
                source="tushare",
            ),
            UniverseEntry(
                symbol="SSE.STK.T600018", name="退市整理标记股",
                list_date=19970101, delist_date=20041230, status="delisted",
                source="tushare",
            ),
            UniverseEntry(
                symbol="SSE.STK.600000", name="浦发银行",
                list_date=19991110, delist_date=None, status="listed",
                source="tushare",
            ),
        ]


def test_auto_candidates_csv_shape(tmp_path, monkeypatch):
    """Roster -> candidate CSV with ts_code/requested_start/requested_end."""
    import pandas as pd

    import wtpy.apps.astock.data.providers.tushare as tushare_mod

    monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeProvider)
    mod = _delisted()
    state_dir = tmp_path / "st"
    state_dir.mkdir(parents=True, exist_ok=True)
    csv_path = mod.auto_generate_candidates("", state_dir)

    df = pd.read_csv(csv_path)
    # only delisted entries, sorted by ts_code; T 前缀等非规范代码被过滤
    assert list(df["ts_code"]) == ["300104.SZ", "600001.SH"]
    assert int(df.iloc[0]["requested_start"]) == 20100812  # 乐视网 list_date
    assert int(df.iloc[0]["requested_end"]) == 20200731    # delist_date
    assert int(df.iloc[1]["requested_start"]) == 19970101
    assert int(df.iloc[1]["requested_end"]) == 20041230


def test_auto_candidates_idempotent(tmp_path, monkeypatch):
    """Same roster -> same candidate file (content-hash path), no rewrite."""
    import wtpy.apps.astock.data.providers.tushare as tushare_mod

    monkeypatch.setattr(tushare_mod, "TushareProvider", _FakeProvider)
    mod = _delisted()
    state_dir = tmp_path / "st"
    state_dir.mkdir(parents=True, exist_ok=True)
    p1 = mod.auto_generate_candidates("", state_dir)
    p2 = mod.auto_generate_candidates("", state_dir)
    assert p1 == p2
    assert p1.exists()
    assert p1.name.startswith("candidates_auto_")


def test_auto_candidates_token_missing_exits_1(tmp_path, monkeypatch, capsys):
    """Roster fetch failure (e.g. no token) -> exit 1 + TUSHARE_TOKEN hint."""
    class _AuthFailingProvider(_FakeProvider):
        def fetch_universe(self, *, include_delisted=False):
            from wtpy.apps.astock.data.providers.tushare import AuthenticationError

            raise AuthenticationError("no token configured")

    import wtpy.apps.astock.data.providers.tushare as tushare_mod

    monkeypatch.setattr(tushare_mod, "TushareProvider", _AuthFailingProvider)
    monkeypatch.setattr(
        "sys.argv",
        ["sync_tushare_delisted.py", "--auto-candidates", "--publish",
         "--state-dir", str(tmp_path / "st")],
    )
    monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path / "md"))
    code = _delisted().main()
    assert code == 1
    out = capsys.readouterr().out
    assert "TUSHARE_TOKEN" in out


# ---------------------------------------------------------------------------
# sync_tushare_chain: delisted step between factor and reconcile
# ---------------------------------------------------------------------------

def _chain_args():
    return SimpleNamespace(
        token="", universe_file=None, factor_raw_root=None, end_date=20260812,
        start_date=None, fresh=False, resume=False, cutoff=20260812,
        storage_root=None, tdx_root=None, batch_size=0, include_bse=False,
        include_delisted=False, limit=0, symbols="", asset_class="stocks",
        adjustment=None, mode="incremental", source="tushare",
    )


def test_chain_runs_delisted_step_and_succeeds(tmp_path, monkeypatch):
    smd = _smd()
    store = SimpleNamespace(root=tmp_path)

    def fake_raw(args, store, *, skip_reconcile_status=False):
        return {"status": "success", "sync_run_id": "raw_1", "datasets": {}}

    def fake_factor(args, store):
        return {"status": "success", "dataset_status": "ready",
                "dataset_id": "fac_1", "reconcile": None}

    calls = {"n": 0}

    def fake_delisted(store, args):
        calls["n"] += 1
        return {"status": "ok", "exit_code": 0}

    def fake_reconcile(store, *, dry_run=False):
        return {"status": "published", "l1_dataset_id": "l1_1",
                "l2_dataset_id": "l2_1", "missing": [], "issues": []}

    monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
    monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
    monkeypatch.setattr(smd, "_run_delisted_pool_sync", fake_delisted)
    monkeypatch.setattr(smd, "_reconcile_after_sync", fake_reconcile)
    monkeypatch.setattr(smd, "_latest_factor_universe_file_path",
                        lambda store: "/fake/universe.csv")
    monkeypatch.setattr(smd, "_resolve_factor_raw_root", lambda args: None)
    monkeypatch.setattr(smd, "_factor_raw_cache_dir", lambda store: tmp_path)

    chain = smd.sync_tushare_chain(_chain_args(), store)
    assert calls["n"] == 1                      # delisted step ran
    assert chain["delisted"]["status"] == "ok"
    assert chain["status"] == "success"
    assert chain["reconcile"]["status"] == "published"


def test_chain_delisted_failure_never_reports_success(tmp_path, monkeypatch):
    """A failed delisted step must demote the chain (reconcile would block
    without the pool, so 'success' would be a lie)."""
    smd = _smd()
    store = SimpleNamespace(root=tmp_path)

    def fake_raw(args, store, *, skip_reconcile_status=False):
        return {"status": "success", "sync_run_id": "raw_1", "datasets": {}}

    def fake_factor(args, store):
        return {"status": "success", "dataset_status": "ready",
                "dataset_id": "fac_1", "reconcile": None}

    def fake_delisted(store, args):
        return {"status": "error", "exit_code": 1,
                "error": "spawn failed: boom"}

    def fake_reconcile(store, *, dry_run=False):
        # even if reconcile somehow passed, the chain must stay warning
        return {"status": "published", "l1_dataset_id": "l1_1",
                "l2_dataset_id": "l2_1", "missing": [], "issues": []}

    monkeypatch.setattr(smd, "sync_tushare_incremental", fake_raw)
    monkeypatch.setattr(smd, "sync_tushare_adj_factor_full", fake_factor)
    monkeypatch.setattr(smd, "_run_delisted_pool_sync", fake_delisted)
    monkeypatch.setattr(smd, "_reconcile_after_sync", fake_reconcile)
    monkeypatch.setattr(smd, "_latest_factor_universe_file_path",
                        lambda store: "/fake/universe.csv")
    monkeypatch.setattr(smd, "_resolve_factor_raw_root", lambda args: None)
    monkeypatch.setattr(smd, "_factor_raw_cache_dir", lambda store: tmp_path)

    chain = smd.sync_tushare_chain(_chain_args(), store)
    assert chain["delisted"]["status"] == "error"
    assert chain["status"] == "warning"
    assert "delisted" in (chain.get("warning") or "")


# ---------------------------------------------------------------------------
# sync_tushare_reconcile (--source internal --mode reconcile)
# ---------------------------------------------------------------------------

def test_reconcile_task_published_is_success(tmp_path, monkeypatch):
    smd = _smd()
    store = SimpleNamespace(root=tmp_path)

    monkeypatch.setattr(smd, "_run_delisted_pool_sync",
                        lambda store, args: {"status": "ok", "exit_code": 0})
    monkeypatch.setattr(smd, "_reconcile_after_sync",
                        lambda store, *, dry_run=False: {
                            "status": "published", "l1_dataset_id": "l1_1",
                            "l2_dataset_id": "l2_1", "missing": [],
                            "issues": [],
                        })
    r = smd.sync_tushare_reconcile(_chain_args(), store)
    assert r["status"] == "success"
    assert r["delisted"]["status"] == "ok"
    assert r["reconcile"]["status"] == "published"


def test_reconcile_task_waiting_for_parent_is_warning(tmp_path, monkeypatch):
    smd = _smd()
    store = SimpleNamespace(root=tmp_path)

    monkeypatch.setattr(smd, "_run_delisted_pool_sync",
                        lambda store, args: {"status": "ok", "exit_code": 0})
    monkeypatch.setattr(smd, "_reconcile_after_sync",
                        lambda store, *, dry_run=False: {
                            "status": "waiting_for_parent",
                            "missing": ["tushare/factor"],
                            "issues": ["factor_date_lag"],
                        })
    r = smd.sync_tushare_reconcile(_chain_args(), store)
    assert r["status"] == "warning"
    assert "waiting_for_parent" in r["reconcile"]["status"]


def test_reconcile_task_delisted_failed_is_warning(tmp_path, monkeypatch):
    smd = _smd()
    store = SimpleNamespace(root=tmp_path)

    monkeypatch.setattr(smd, "_run_delisted_pool_sync",
                        lambda store, args: {
                            "status": "error", "exit_code": 1,
                            "error": "token missing"})
    monkeypatch.setattr(smd, "_reconcile_after_sync",
                        lambda store, *, dry_run=False: {
                            "status": "published", "l1_dataset_id": "l1_1",
                            "l2_dataset_id": "l2_1", "missing": [],
                            "issues": [],
                        })
    r = smd.sync_tushare_reconcile(_chain_args(), store)
    assert r["status"] == "warning"
    assert "delisted" in (r.get("warning") or "")


def test_main_dispatches_internal_reconcile(tmp_path, monkeypatch):
    smd = _smd()
    monkeypatch.setattr(smd, "get_storage_root", lambda: tmp_path / "md")
    seen = {}

    def fake_reconcile(args, store):
        seen["store_root"] = store.root
        return {"status": "success", "delisted": {"status": "ok"},
                "reconcile": {"status": "up_to_date"}}

    monkeypatch.setattr(smd, "sync_tushare_reconcile", fake_reconcile)
    monkeypatch.setattr(
        "sys.argv",
        ["sync_market_data.py", "--source", "internal", "--mode", "reconcile",
         "--storage-root", str(tmp_path / "md")],
    )
    monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path / "md"))
    code = smd.main()
    assert code == 0
    assert seen["store_root"] == tmp_path / "md"


# ---------------------------------------------------------------------------
# API-level exit-2 classification (system.py _run_sync_process)
# ---------------------------------------------------------------------------

class _FakeProc:
    """Minimal Popen stand-in: already-exited child with a return code."""

    def __init__(self, returncode):
        self.stdout = iter(())
        self.returncode = returncode
        self._poll = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self._poll

    def terminate(self):
        pass

    def kill(self):
        pass


def _run_sync_process_with(monkeypatch, returncode):
    from wtpy.apps.astock.api_routes import system as system_routes

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(market_data_root=Path(".")),
        sync_state={
            "running": True, "task": "reconcile", "status": "running",
            "started_at": None, "finished_at": None, "error": None,
            "output": [], "stop_requested": False,
        },
        sync_proc={"proc": None},
        sync_lock=threading.Lock(),
    )
    monkeypatch.setattr(system_routes.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(returncode))
    system_routes._run_sync_process(
        ctx, [sys.executable, "-u", "sync.py"], "reconcile")
    return ctx.sync_state


def test_exit_code_2_classified_as_warning(monkeypatch):
    st = _run_sync_process_with(monkeypatch, returncode=2)
    assert st["status"] == "warning"
    assert st["running"] is False
    assert "warning" in (st["error"] or "")


def test_exit_code_1_classified_as_error(monkeypatch):
    st = _run_sync_process_with(monkeypatch, returncode=1)
    assert st["status"] == "error"


def test_exit_code_0_classified_as_done(monkeypatch):
    st = _run_sync_process_with(monkeypatch, returncode=0)
    assert st["status"] == "done"


# ---------------------------------------------------------------------------
# 传统 --candidates 路径回归 + --cutoff 显式参数 + MARKET_DATA_ROOT 缺失
# ---------------------------------------------------------------------------

def test_manual_candidates_legacy_path_with_cutoff(tmp_path, monkeypatch):
    """--candidates 传统路径必须保持可用：读人工 CSV、--cutoff 显式传参、
    不触发 auto 候选（--auto-candidates 缺失时不该调用网络名单）。"""
    import pandas as pd

    cand = tmp_path / "candidates.csv"
    cand.write_text(
        "ts_code,requested_start,requested_end\n"
        "600001.SH,19970101,20041230\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setenv("MARKET_DATA_ROOT", str(tmp_path / "md"))

    class FakePro:
        def daily(self, ts_code, end_date):
            return pd.DataFrame()  # 空响应 -> NO_DATA，快速走完

    class FakeTs:
        def pro_api(self, token=None):
            return FakePro()

    monkeypatch.setitem(sys.modules, "tushare", FakeTs())
    mod = _delisted()
    monkeypatch.setattr(
        mod, "auto_generate_candidates",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("auto candidates must not run in manual mode")),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["sync_tushare_delisted.py", "--candidates", str(cand),
         "--cutoff", "20260801", "--state-dir", str(tmp_path / "st"),
         "--publish"],
    )
    assert mod.main() == 0
    import json

    st = json.loads((tmp_path / "st" / "sync_state.json").read_text(
        encoding="utf-8"))
    entry = st["symbols"]["600001.SH"]
    assert entry["status"] == "no_data"


def test_market_data_root_missing_fails_loudly(tmp_path, monkeypatch, capsys):
    """MARKET_DATA_ROOT 未设置 -> exit 1 + 明确提示（不再落到 Windows 默认路径）。"""
    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["sync_tushare_delisted.py", "--candidates", str(tmp_path / "c.csv"),
         "--publish"],
    )
    assert _delisted().main() == 1
    out = capsys.readouterr().out
    assert "MARKET_DATA_ROOT" in out


def test_auto_candidates_requires_market_data_root(tmp_path, monkeypatch, capsys):
    """--auto-candidates 同样先校验数据根（env 缺失时在拉名单前失败）。"""
    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["sync_tushare_delisted.py", "--auto-candidates",
         "--state-dir", str(tmp_path / "st")],
    )
    assert _delisted().main() == 1
    out = capsys.readouterr().out
    assert "MARKET_DATA_ROOT" in out
