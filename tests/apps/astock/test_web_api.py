"""API + service structural and functional tests (TestClient)."""

from __future__ import annotations

import sys
from pathlib import Path

import tests.apps.astock.conftest  # noqa: F401

import pytest

from wtpy.apps.astock.api import STATIC_DIR, create_app
from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service.backtest import BacktestRequest


def test_static_frontend_exists():
    index = STATIC_DIR / "index.html"
    assert index.is_file(), f"missing frontend {index}"
    text = index.read_text(encoding="utf-8")
    assert "entry_lag" in text or "entryLag" in text
    assert "/api/v1/backtests" in text
    assert "/api/v1/rules" in text


def test_api_health_and_rules(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    ind.mkdir()
    storage.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    client = TestClient(app)

    h = client.get("/api/v1/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True

    v = client.post(
        "/api/v1/rules/validate",
        json={"formula_text": "XG:C>0;", "name": "t"},
    )
    assert v.status_code == 200
    assert v.json()["ok"] is True

    c = client.post(
        "/api/v1/rules",
        json={"name": "api_rule", "formula_text": "XG:C>OPEN;"},
    )
    assert c.status_code == 200
    rid = c.json()["id"]
    assert rid.startswith("user_")

    lst = client.get("/api/v1/rules")
    assert lst.status_code == 200
    assert any(r["id"] == rid for r in lst.json())

    page = client.get("/")
    assert page.status_code == 200
    assert "回测" in page.text


def test_backtest_request_to_dict_includes_entry_lag():
    req = BacktestRequest(rule_ids=["x"], entry_lag=2, hold=3)
    d = req.to_dict()
    assert d["entry_lag"] == 2
    assert d["hold"] == 3
    assert d["rule_ids"] == ["x"]
    assert d["corporate_action_policy"] is None


def test_api_maps_corporate_action_policy_to_request(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.service.backtest import BacktestService

    captured = {}

    def fake_run(self, req, *, progress_cb=None):
        captured["request"] = req
        return {"run_id": "bt_ca_api", "status": "ok"}

    monkeypatch.setattr(BacktestService, "run", fake_run)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/api/v1/backtests",
        json={
            "rule_ids": ["test_rule"],
            "codes": ["SSE.STK.600000"],
            "corporate_action_policy": "event_ledger",
        },
    )

    assert response.status_code == 200
    assert captured["request"].corporate_action_policy == "event_ledger"

def test_factor_sync_start_adds_universe_file_from_env(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    # Bug 2 regression: the worker thread must receive (ctx, cmd, task_name);
    # a missing ctx makes _run_sync_process crash inside the thread while the
    # API already returned 200.
    assert len(started["args"]) == 3
    ctx_arg, cmd, task_arg = started["args"]
    assert hasattr(ctx_arg, "cfg") and hasattr(ctx_arg, "sync_state")
    assert task_arg == "factor"
    assert "--adjustment" in cmd
    assert "adj_factor" in cmd
    # Bug 1 regression: adj_factor tasks are ALWAYS incremental (window fetch
    # + parent merge), never a full-history refetch.
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "incremental"
    assert "--universe-file" in cmd
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)
    assert cmd[cmd.index("--end-date") + 1]


def test_factor_sync_start_passes_explicit_start_date(tmp_path, monkeypatch):
    """A user-pinned start_date must reach the adj_factor command line."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/api/v1/data-sync/start",
        json={"task": "factor", "start_date": 20260720},
    )
    assert response.status_code == 200
    cmd = started["args"][1]
    assert "--start-date" in cmd
    assert cmd[cmd.index("--start-date") + 1] == "20260720"


def test_sync_scripts_resolve_to_existing_files(tmp_path, monkeypatch):
    """Regression: sync script paths must stay anchored to the project root.

    system.py lives one package level deeper than the old api.py, so a stale
    `parents[3]` anchor resolves scripts to wtpy/scripts/... which does not
    exist and makes every UI-launched sync fail inside the worker thread
    (HTTP still returns 200, which is why route probes miss it).
    """
    pytest.importorskip("fastapi")
    from pathlib import Path

    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api_routes import system as system_routes

    script = Path(system_routes.SYNC_SCRIPT)
    assert script.is_file(), f"sync script missing: {script}"

    ca_script = system_routes.PROJECT_ROOT / "scripts" / "sync_ca_events.py"
    assert ca_script.is_file(), f"ca script missing: {ca_script}"

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)

    # tdx is DISABLED by the Tushare-only policy: returns a structured skip
    # without spawning any process.
    started.clear()
    client = TestClient(create_app(cfg))
    started.clear()  # create_app may spawn worker threads (JobStore)
    r = client.post("/api/v1/data-sync/start", json={"task": "tdx"})
    assert r.status_code == 200
    assert r.json()["skipped"] == "disabled_by_policy"
    assert "args" not in started

    for task in ("tushare", "factor", "derive", "ca"):
        started.clear()
        client = TestClient(create_app(cfg))
        r = client.post("/api/v1/data-sync/start", json={"task": task})
        assert r.status_code == 200, (task, r.text)
        args = started.get("args", ())
        assert len(args) == 3, f"{task}: thread must receive (ctx, cmd, task_name)"
        ctx_arg, cmd, task_arg = args
        assert hasattr(ctx_arg, "cfg") and hasattr(ctx_arg, "sync_state"), task
        assert task_arg == task
        # cmd = [sys.executable, "-u", <script>, ...] -> script at index 2
        assert Path(cmd[2]).is_file(), f"{task}: script missing: {cmd[2]}"

    # task=tushare runs the zero-config chain in the script: raw incremental
    # without a pinned --adjustment (factor + reconcile follow inside).
    started.clear()
    client = TestClient(create_app(cfg))
    r = client.post("/api/v1/data-sync/start", json={"task": "tushare"})
    assert r.status_code == 200
    cmd = started["args"][1]
    assert "--source" in cmd
    assert cmd[cmd.index("--source") + 1] == "tushare"
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "incremental"
    assert "--adjustment" not in cmd


def test_factor_sync_start_reuses_latest_manifest_universe(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUSHARE_FACTOR_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("ASTOCK_FACTOR_UNIVERSE_FILE", raising=False)
    universe = tmp_path / "manifest_universe.csv"
    universe.write_text(
        "canonical_symbol,inclusion_status\nSSE.STK.600000,included\n",
        encoding="utf-8",
    )
    storage = tmp_path / "st"
    indicators = tmp_path / "ind"
    storage.mkdir()
    indicators.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=indicators)
    store = DatasetStore(cfg.market_data_root)
    store.save_manifest(
        DatasetManifest(
            dataset_id="tushare_adjfactor_1d_test",
            source="tushare",
            adjustment="adj_factor",
            period="1d",
            status="ready",
            dataset_type="factor",
            data_cutoff_date=20260729,
            symbol_count=1,
            universe_file=str(universe),
        )
    )

    started = {}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            started["args"] = kwargs.get("args", ())

        def start(self):
            started["started"] = True

    import threading

    monkeypatch.setattr(threading, "Thread", FakeThread)
    client = TestClient(create_app(cfg))

    response = client.post("/api/v1/data-sync/start", json={"task": "factor"})

    assert response.status_code == 200
    cmd = started["args"][1]
    assert cmd[cmd.index("--universe-file") + 1] == str(universe)


def test_dashboard_overview_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    r = client.get("/api/v1/dashboard/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    for key in ("data", "sync", "ca", "universe", "findings", "watchlist"):
        assert key in d
    # Graceful on a bare server: data block reports the missing root, no crash.
    assert d["data"]["exists"] is False
    assert isinstance(d["findings"], list)
    assert isinstance(d["watchlist"]["count"], (int, type(None)))

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "关键发现" in page.text

    # 30s TTL cache: repeated call must not recompute (same generated_at).
    r2 = client.get("/api/v1/dashboard/overview")
    assert r2.status_code == 200
    assert r2.json()["generated_at"] == d["generated_at"]

    index = client.get("/")
    assert "/dashboard" in index.text


def test_quick_query_endpoint_and_page(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))

    # Structure is stable regardless of data availability.
    r = client.get("/api/v1/quick/600000")
    assert r.status_code == 200
    d = r.json()
    assert d["std_code"] in ("SSE.STK.600000", "sh600000")
    for key in ("code", "name", "std_code", "market", "gua", "gua_week", "related_runs"):
        assert key in d
    assert isinstance(d["related_runs"], list)
    # market degrades gracefully without warehouse data
    assert "market" in d and isinstance(d["market"], dict)

    # Daily + weekly hexagram blocks share the same shape.
    for gk in ("gua", "gua_week"):
        g = d[gk]
        assert isinstance(g, dict)
        if g.get("error") is None:
            assert g["period"] in ("DAY", "WEEK")

    # Same code is served from the 60s TTL cache (identical object id).
    r2 = client.get("/api/v1/quick/600000")
    assert r2.status_code == 200
    assert r2.json()["gua"] == d["gua"]

    # Chinese-name input resolves to a code (only when a non-empty name
    # cache is available; empty warehouses have no name data to resolve).
    _has_names = False
    try:
        from wtpy.apps.astock.service.stock_names import ensure_name_cache

        _has_names = bool(ensure_name_cache(cfg))
    except Exception:
        _has_names = False
    if _has_names:
        r = client.get("/api/v1/quick/平安银行")
        assert r.status_code == 200

    # Invalid input -> 4xx, not 500.
    r = client.get("/api/v1/quick/zzzzz")
    assert r.status_code in (400, 404)

    page = client.get("/quick.html?code=600000")
    assert page.status_code == 200
    assert "个股快速查询" in page.text
    assert "周卦" in page.text

    index = client.get("/")
    assert "quickCode" in index.text and "/quick.html" in index.text


class FakeSyncProc:
    """Stand-in for subprocess.Popen in _run_sync_process tests.

    poll_result=None means the child is still alive (poll() -> None);
    pass 0/1 for an already-exited child.
    """

    def __init__(self, lines=(), returncode=0, raise_on_iter=None, poll_result=None):
        self.stdout = _FakeSyncStdout(lines, raise_on_iter)
        self.returncode = returncode
        self._poll = poll_result
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._poll is None:
            self._poll = self.returncode
        return self.returncode

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminate_calls += 1
        if self._poll is None:
            self._poll = 1

    def kill(self):
        self.kill_calls += 1
        if self._poll is None:
            self._poll = 1


class _FakeSyncStdout:
    def __init__(self, lines, raise_on_iter):
        self._lines = list(lines)
        self._raise = raise_on_iter

    def __iter__(self):
        if self._raise is not None:
            raise self._raise
        return iter(self._lines)


def _sync_app(tmp_path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    app = create_app(cfg)
    return app, TestClient(app)


def _run_sync_with_proc(monkeypatch, ctx, proc):
    from wtpy.apps.astock.api_routes import system as system_routes

    def fake_popen(*args, **kwargs):
        return proc

    monkeypatch.setattr(system_routes.subprocess, "Popen", fake_popen)
    system_routes._run_sync_process(ctx, [sys.executable, "-u", "sync.py"], "tushare")


def test_sync_done_when_rc0_even_if_stop_requested_raced(tmp_path, monkeypatch):
    """Fix: a sync that already exited rc=0 must be reported done, never
    stopped, even when the user clicked stop while the reader was draining."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(lines=["[SYNC_PROGRESS] done=1 total=1 phase=raw"], returncode=0)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "done"
    assert ctx.sync_state["error"] is None
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None
    # progress lines still captured
    assert ctx.sync_state["progress_done"] == 1


def test_sync_terminated_by_stop_reports_stopped(tmp_path, monkeypatch):
    """A genuinely terminated process (rc!=0 + stop request) reports stopped."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(returncode=1)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "stopped"
    assert ctx.sync_state["error"] == "用户手动停止"


def test_sync_nonzero_without_stop_reports_error(tmp_path, monkeypatch):
    """Business failure (rc!=0, no stop request) stays an error."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(returncode=1)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "error"
    assert ctx.sync_state["error"] == "exit code 1"


def test_sync_reader_exception_during_stop_keeps_stopped(tmp_path, monkeypatch):
    """Fix: OSError from the stdout read loop while stopping (Windows
    TerminateProcess) must not turn an intentional stop into an error."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "stopping"
        ctx.sync_state["stop_requested"] = True
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(raise_on_iter=OSError("read failed"), poll_result=None)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert ctx.sync_state["status"] == "stopped"
    assert ctx.sync_state["error"] == "用户手动停止"


def test_sync_reader_exception_terminates_orphan_proc(tmp_path, monkeypatch):
    """Fix: an exception in the reader loop must terminate the still-alive
    child so it can never hold the SyncTaskLock for later runs."""
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"
    proc = FakeSyncProc(raise_on_iter=OSError("pipe closed"), poll_result=None)
    _run_sync_with_proc(monkeypatch, ctx, proc)
    assert proc.terminate_calls >= 1
    assert ctx.sync_state["status"] == "error"
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None


def test_sync_popen_failure_reports_error_and_clears(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    app, _client = _sync_app(tmp_path)
    ctx = app.state.astock
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["status"] = "running"
        ctx.sync_state["task"] = "tushare"

    from wtpy.apps.astock.api_routes import system as system_routes

    def boom_popen(*args, **kwargs):
        raise FileNotFoundError("no such script")

    monkeypatch.setattr(system_routes.subprocess, "Popen", boom_popen)
    system_routes._run_sync_process(ctx, ["python", "missing.py"], "tushare")
    assert ctx.sync_state["status"] == "error"
    assert "no such script" in (ctx.sync_state["error"] or "")
    assert ctx.sync_state["running"] is False
    assert ctx.sync_proc["proc"] is None


def test_sync_stop_endpoint_marks_stop_requested_but_payload_stays_clean(tmp_path):
    """stop_requested is an internal marker: set on stop, never leaked into
    the status payload consumed by renderSyncProgress / pollSyncStatus."""
    pytest.importorskip("fastapi")
    app, client = _sync_app(tmp_path)
    ctx = app.state.astock
    proc = FakeSyncProc(returncode=1, poll_result=None)
    with ctx.sync_lock:
        ctx.sync_state["running"] = True
        ctx.sync_state["task"] = "tushare"
        ctx.sync_state["status"] = "running"
        ctx.sync_proc["proc"] = proc
    r = client.post("/api/v1/data-sync/stop", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with ctx.sync_lock:
        assert ctx.sync_state["status"] == "stopping"
        assert ctx.sync_state["stop_requested"] is True
    st = client.get("/api/v1/data-sync/status").json()
    assert st["status"] == "stopping"
    assert "stop_requested" not in st


# ---------------------------------------------------------------------------
# P1-3: L1/L2 source-freshness tiles follow the ACTIVE product pair
# ---------------------------------------------------------------------------


def _save_product_manifest(store, dataset_id, source, adjustment, *, cutoff,
                           provenance, raw_dataset_id="", factor_dataset_id="",
                           dataset_type="bars"):
    """Publish a minimal ready manifest (no blobs needed for the API)."""
    from wtpy.apps.astock.data.dataset_store import DatasetManifest

    store.save_manifest(DatasetManifest(
        dataset_id=dataset_id,
        source=source,
        adjustment=adjustment,
        period="1d",
        status="ready",
        data_cutoff_date=cutoff,
        dataset_type=dataset_type,
        provenance=dict(provenance or {}),
        raw_dataset_id=raw_dataset_id,
        factor_dataset_id=factor_dataset_id,
        created_at=dataset_id,
        symbol_count=1,
        row_count=100,
    ))


def test_market_status_tiles_follow_active_pair(tmp_path):
    """P1-3 regression: the L1/L2 tiles must show the ACTIVE pair surfaces.

    An independent-latest composite face that is NOT tushare_only_v1 (and has
    no valid L2 parent) must never win a tile: the tile and the product block
    always come from the same validated pair.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)

    # Valid pair: OLDER ready L1 (tushare_only_v1) whose raw parent is the
    # matching formal L2 (composite_none, tushare_only_v1). The lineage is
    # complete (L2 parents + L1 factor parent exist and carry the formal
    # roles) so the strict fail-closed pair validation accepts it.
    _save_product_manifest(
        store, "raw_base_pair", "tushare", "none", cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "raw_supp_pair", "tushare", "none", cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "tushare_adjfactor_1d_pair", "tushare", "adj_factor",
        cutoff=20260701, dataset_type="factor",
        provenance={"data_policy": "tushare_only_v1"},
    )
    _save_product_manifest(
        store, "l2_pair_old", "internal", "composite_none", cutoff=20260701,
        provenance={
            "data_policy": "tushare_only_v1",
            "base_source": "tushare",
            "supplement_source": "tushare",
            "parents": [
                {"dataset_id": "raw_base_pair", "role": "base"},
                {"dataset_id": "raw_supp_pair", "role": "supplement"},
            ],
        },
    )
    _save_product_manifest(
        store, "l1_pair_old", "internal", "composite_tushare_factor_qfq",
        cutoff=20260701,
        provenance={"data_policy": "tushare_only_v1"},
        raw_dataset_id="l2_pair_old",
        factor_dataset_id="tushare_adjfactor_1d_pair",
    )
    # NEWER L1 face with NO tushare_only_v1 marker and no parent: freshest by
    # cutoff but NOT part of any valid pair.
    _save_product_manifest(
        store, "l1_orphan_new", "internal", "composite_tushare_factor_qfq",
        cutoff=20260730, provenance={},
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    assert d["exists"] is True
    tiles = {t["key"]: t for t in d["source_freshness"]}
    # Tiles follow the ACTIVE pair, never the independent freshest face.
    assert tiles["l1_product"]["dataset_id"] == "l1_pair_old"
    assert tiles["l1_product"]["status"] == "ready"
    assert tiles["l1_product"]["data_policy"] == "tushare_only_v1"
    assert tiles["l1_product"]["data_cutoff_date"] == 20260701
    assert tiles["l2_product"]["dataset_id"] == "l2_pair_old"
    assert tiles["l2_product"]["data_policy"] == "tushare_only_v1"
    # Tiles and the product block come from the SAME pair.
    assert d["product"]["active"] is True
    assert d["product"]["l1"]["dataset_id"] == tiles["l1_product"]["dataset_id"]
    assert d["product"]["l2"]["dataset_id"] == tiles["l2_product"]["dataset_id"]


def test_market_status_tiles_inactive_without_pair(tmp_path):
    """P1-3: with no valid pair the L1/L2 tiles degrade to inactive/None
    (existing field structure kept, product.active False)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    # Only an orphan composite face (no tushare_only_v1 marker, no parent).
    _save_product_manifest(
        store, "l1_orphan_only", "internal", "composite_tushare_factor_qfq",
        cutoff=20260730, provenance={},
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    for key in ("l1_product", "l2_product"):
        assert tiles[key]["status"] == "inactive"
        assert tiles[key]["dataset_id"] is None
        assert tiles[key]["data_cutoff_date"] is None
        assert tiles[key]["symbol_count"] == 0
    assert d["product"] == {"l1": None, "l2": None, "active": False}


# ---------------------------------------------------------------------------
# P2-1b: factor tile prefers the LATEST candidate (freshness gate aware)
# ---------------------------------------------------------------------------


def _save_factor_manifest(store, dataset_id, *, cutoff, status,
                          freshness=None, provenance=None, created_at=None):
    from wtpy.apps.astock.data.dataset_store import DatasetManifest

    prov = dict(provenance or {})
    if freshness is not None:
        prov["freshness"] = freshness
    store.save_manifest(DatasetManifest(
        dataset_id=dataset_id,
        source="tushare",
        adjustment="adj_factor",
        period="1d",
        status=status,
        dataset_type="factor",
        data_cutoff_date=cutoff,
        symbol_count=1,
        row_count=100,
        created_at=created_at or dataset_id,
        provenance=prov,
    ))


def test_market_status_factor_tile_prefers_latest_partial_with_freshness(tmp_path):
    """P2-1b regression: the newest factor surface wins the tile even when it
    is a freshness-gate-blocked partial, and the tile carries the freshness
    gate summary (an older ready factor must not shadow the stall)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)

    # OLD ready factor (no freshness metadata) vs NEW freshness-blocked partial.
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_ready_old", cutoff=20260701,
        status="ready",
    )
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_partial_fresh", cutoff=20260804,
        status="partial",
        freshness={
            "fresh_symbol_ratio": 0.5,
            "fresh_count": 2,
            "active_count": 4,
            "stale_active_symbols": [
                {"symbol": f"SZSE.STK.{600000 + i}", "factor_last_date": 20260701,
                 "raw_last_date": 20260804}
                for i in range(7)
            ],
            "p50_last_date": 20260730,
            "p10_last_date": 20260701,
            "raw_dataset_id": "tushare_none_1d_raw_x",
            "factor_dataset_id": "tushare_adjfactor_1d_partial_fresh",
            "fresh_tolerance_days": 3,
            "gate": "blocked",
            "reason": "freshness_below_threshold",
        },
    )
    # Raw tile must KEEP ready-first: a newer raw partial must not displace
    # the ready raw surface (raw has no freshness semantics).
    # 注意：真实 raw 数据集必须带 STK 符号（纯 ETF/指数数据集不能冒充
    # 股票地基），这里补上真实 symbols，否则会被 raw 卡的资产类别过滤排除。
    from wtpy.apps.astock.data.dataset_store import SymbolRecord

    def _raw_symbols():
        return [SymbolRecord(
            symbol="SSE.STK.600000", blob_sha256="x" * 64,
            first_date=20240101, last_date=20260804, row_count=100,
            quality="ok",
        )]

    store.save_manifest(DatasetManifest(
        dataset_id="tushare_none_1d_raw_ready", source="tushare",
        adjustment="none", period="1d", status="ready",
        dataset_type="bars", data_cutoff_date=20260804,
        symbol_count=1, row_count=100, created_at="raw_ready",
        symbols=_raw_symbols(),
    ))
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_none_1d_raw_partial", source="tushare",
        adjustment="none", period="1d", status="partial",
        dataset_type="bars", data_cutoff_date=20260805,
        symbol_count=1, row_count=100, created_at="raw_partial",
        symbols=_raw_symbols(),
    ))

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    # Factor tile shows the LATEST partial surface, not the old ready one.
    assert tiles["factor"]["dataset_id"] == "tushare_adjfactor_1d_partial_fresh"
    assert tiles["factor"]["status"] == "partial"
    assert tiles["factor"]["data_cutoff_date"] == 20260804
    assert tiles["factor"]["updated_to"] == 20260804
    # Freshness summary carried from provenance, stale sample capped at 5.
    f = tiles["factor"]["freshness"]
    assert f is not None
    assert f["gate"] == "blocked"
    assert f["reason"] == "freshness_below_threshold"
    assert f["fresh_symbol_ratio"] == 0.5
    assert f["fresh_count"] == 2
    assert f["active_count"] == 4
    assert f["p50"] == 20260730
    assert f["p10"] == 20260701
    assert len(f["stale_active_symbols"]) == 5
    assert f["stale_active_symbols"][0]["symbol"] == "SZSE.STK.600000"
    # Raw tile keeps ready priority even when a newer partial exists.
    assert tiles["tushare"]["dataset_id"] == "tushare_none_1d_raw_ready"
    assert tiles["tushare"]["status"] == "ready"


def test_market_status_factor_tile_old_manifest_without_freshness(tmp_path):
    """P2-1b: an old-format factor manifest without provenance freshness must
    not crash and the tile reports freshness=None (fields stay intact)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_legacy", cutoff=20260729, status="ready",
    )

    client = TestClient(create_app(cfg))
    d = client.get("/api/v1/market-data/status").json()
    tiles = {t["key"]: t for t in d["source_freshness"]}
    t = tiles["factor"]
    assert t["dataset_id"] == "tushare_adjfactor_1d_legacy"
    assert t["status"] == "ready"
    assert t["data_cutoff_date"] == 20260729
    assert t["freshness"] is None
    # Existing tile field structure preserved (frontend compatible).
    for key in ("source", "adjustment", "earliest_date", "latest_date",
                "updated_to", "symbol_count", "row_count", "created_at"):
        assert key in t


def test_data_health_factor_item_reports_gate(tmp_path):
    """P2-1b: /api/v1/system/data-health reports the LATEST factor surface
    (a freshness-blocked partial, not the old ready one) and its gate state."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    # Derive-path gate shape: status (not gate) — the report must read both.
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_ready_old", cutoff=20260701,
        status="ready",
    )
    _save_factor_manifest(
        store, "tushare_adjfactor_1d_partial_gate", cutoff=20260804,
        status="partial",
        freshness={
            "status": "blocked",
            "reason": "freshness_below_threshold",
            "fresh_symbol_ratio": 0.25,
            "fresh_count": 1,
            "active_count": 4,
            "stale_active_symbols": [
                {"symbol": "SZSE.STK.600000", "factor_last_date": 20260701,
                 "raw_last_date": 20260804}
            ],
            "factor_dataset_id": "tushare_adjfactor_1d_partial_gate",
            "raw_dataset_id": "tushare_none_1d_raw_x",
            "min_ratio": 0.9,
        },
    )

    client = TestClient(create_app(cfg))
    h = client.get("/api/v1/system/data-health").json()
    factor = h["current_freshness"]["tushare_factor"]
    assert factor["dataset_id"] == "tushare_adjfactor_1d_partial_gate"
    assert factor["status"] == "partial"
    assert factor["freshness_gate"] == "blocked"
    assert factor["fresh_symbol_ratio"] == 0.25
    assert factor["data_cutoff_date"] == 20260804
    # Data health itself remains fail-closed on the missing formal pair.
    assert h["status"] in ("stale", "warning")


# ---------------------------------------------------------------------------
# P2-1: recent sync errors carry the concrete failure detail
# ---------------------------------------------------------------------------


def test_data_health_recent_errors_carry_failure_details(tmp_path):
    """P2-1 regression: a partial sync log's result (missing_factor, missing
    list, counts, issues_sample) must reach /api/v1/system/data-health; old
    minimal logs keep working with graceful None fields."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data.dataset_store import DatasetStore

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    store = DatasetStore(cfg.market_data_root)
    store.save_sync_log("partial_rich", {
        "sync_run_id": "partial_rich",
        "dataset_id": "internal_composite_tushare_factor_qfq_1d_20260730_x",
        "result": {
            "status": "partial",
            "error": None,
            "missing_factor": 553,
            "missing": ["SSE.STK.600001", "SSE.STK.600002", "SSE.STK.600003",
                        "SSE.STK.600004", "SSE.STK.600005"],
            "imported": 5000, "eligible": 5100, "row_count": 120000,
            "failed": 2, "no_data": 553,
            "warning": "strict policy partial",
            "reason": "missing_factor",
        },
        "issues_sample": ["issue-a", "issue-b", "issue-c", "issue-d"],
    })
    store.save_sync_log("failed_minimal", {
        "sync_run_id": "failed_minimal",
        "dataset_id": "tushare_none_1d_x",
        "result": {"status": "failed", "error": "rate_limited"},
    })

    client = TestClient(create_app(cfg))
    h = client.get("/api/v1/system/data-health").json()
    errors = {e["sync_run_id"]: e for e in h["recent_sync_errors"]}
    rich = errors["partial_rich"]
    assert rich["status"] == "partial"
    assert rich["error"] is None
    assert rich["missing_factor"] == 553
    assert rich["missing_count"] == 5
    assert rich["imported"] == 5000
    assert rich["eligible"] == 5100
    assert rich["row_count"] == 120000
    assert rich["failed"] == 2
    assert rich["no_data"] == 553
    assert rich["warning"] == "strict policy partial"
    assert rich["reason"] == "missing_factor"
    # issues_sample: first 3 entries only.
    assert rich["issues_sample"] == ["issue-a", "issue-b", "issue-c"]
    # Old/minimal logs must not crash and keep graceful None defaults.
    old = errors["failed_minimal"]
    assert old["status"] == "failed"
    assert old["error"] == "rate_limited"
    assert old["missing_factor"] is None
    assert old["missing_count"] is None
    assert old["imported"] is None
    assert "issues_sample" not in old


def test_bagua_query_tdx_front_disabled_returns_400(tmp_path):
    """The disabled tdx_front price plane is a client error: the API must
    answer 400 with the clear disabled-source message (never a 500)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.config import get_default_config

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    client = TestClient(create_app(cfg))
    for url in ("/api/v1/bagua/query",):
        resp = client.get(
            url,
            params={"code": "600000", "date": "2026-08-04",
                    "adjust": "tdx_front"},
        )
        assert resp.status_code == 400, resp.text
        assert "已停用" in resp.text


def test_factor_sync_universe_reuse_ignores_non_factor_manifests(
        tmp_path, monkeypatch):
    """_latest_factor_universe_file must only reuse universe files from real
    factor manifests (dataset_type=factor), matching the sync script's
    selector — a bars manifest must not win the selection."""
    pytest.importorskip("fastapi")
    from wtpy.apps.astock.api_routes import system as system_routes

    from wtpy.apps.astock.data.dataset_store import DatasetManifest, DatasetStore

    monkeypatch.delenv("MARKET_DATA_ROOT", raising=False)
    monkeypatch.delenv("TUSHARE_FACTOR_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("ASTOCK_FACTOR_UNIVERSE_FILE", raising=False)
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    universe = tmp_path / "factor_universe.csv"
    universe.write_text("canonical_symbol,inclusion_status\n", encoding="utf-8")
    bars_universe = tmp_path / "bars_universe.csv"
    bars_universe.write_text("canonical_symbol,inclusion_status\n", encoding="utf-8")
    store = DatasetStore(cfg.market_data_root)
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_adjfactor_1d_fake_bars",
        source="tushare", adjustment="adj_factor", period="1d",
        status="ready", dataset_type="bars",  # NOT a factor dataset
        data_cutoff_date=20260804, symbol_count=1,
        universe_file=str(bars_universe),
    ))
    store.save_manifest(DatasetManifest(
        dataset_id="tushare_adjfactor_1d_real",
        source="tushare", adjustment="adj_factor", period="1d",
        status="ready", dataset_type="factor",
        data_cutoff_date=20260729, symbol_count=1,
        universe_file=str(universe),
    ))
    ctx = _FakeCtx(cfg)
    picked = system_routes._latest_factor_universe_file(ctx)
    assert picked == str(universe)


class _FakeCtx:
    """Minimal ApiContext stand-in for route helper tests."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sync_state = {}
        self.sync_proc = {}
        self.sync_lock = None