# -*- coding: utf-8 -*-
"""Tests for the automatic EOD market-data sync (startup + scheduled)."""

from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.api import _effective_data_lag, eod_sync_decide


# ------------------------------------------------------------ decide (pure)

def test_effective_data_lag_uses_max_of_raw_and_factor():
    """raw 最新(lag=0)但 factor 落后(lag=2)时,必须报告 2 而非 0。"""
    health = {
        "trading_day_lag": {"raw": 0, "factor": 2},
        "expected_latest_trading_day": 20260813,
        "formal_l2": {"max_date": 20260813},
        "formal_l1": {"max_date": 20260813},
    }
    assert _effective_data_lag(health) == 2


def test_effective_data_lag_retries_when_formal_pair_lags_but_raw_fresh():
    """2026-08-13 事故场景: raw/factor 最新,但正式 L1/L2 停在旧日期。

    factor 限流导致 reconcile 被 fail-closed 跳过,正式面落后一天;旧逻辑
    只看 raw lag(=0)会静默放弃重试。修复后至少报 1,触发当天重试。
    """
    health = {
        "trading_day_lag": {"raw": 0, "factor": 0},
        "expected_latest_trading_day": 20260813,
        "formal_l2": {"max_date": 20260812},
        "formal_l1": {"max_date": 20260812},
    }
    assert _effective_data_lag(health) == 1


def test_effective_data_lag_zero_when_everything_current():
    health = {
        "trading_day_lag": {"raw": 0, "factor": 0},
        "expected_latest_trading_day": 20260813,
        "formal_l2": {"max_date": 20260813},
        "formal_l1": {"max_date": 20260813},
    }
    assert _effective_data_lag(health) == 0


def test_effective_data_lag_raw_only_without_formal():
    health = {
        "trading_day_lag": {"raw": 1, "factor": None},
    }
    assert _effective_data_lag(health) == 1


def test_effective_data_lag_none_when_empty():
    assert _effective_data_lag({}) is None


def test_eod_sync_decide_triggers_on_friday_after_time():
    friday = _dt.datetime(2026, 8, 14, 18, 40)
    trigger, reason, today = eod_sync_decide(
        lag=2, now=friday, sync_time="18:30", sync_weekday=4, min_lag=1
    )
    assert trigger is True
    assert today == _dt.date(2026, 8, 14)
    assert "滞后" in reason


def test_eod_sync_decide_skips_non_scheduled_day():
    wed = _dt.datetime(2026, 8, 12, 18, 40)
    trigger, reason, today = eod_sync_decide(
        lag=5, now=wed, sync_weekday=4
    )
    assert trigger is False
    assert today is None


def test_eod_sync_decide_skips_before_time():
    friday_before = _dt.datetime(2026, 8, 14, 18, 20)
    trigger, reason, today = eod_sync_decide(
        lag=5, now=friday_before, sync_time="18:30", sync_weekday=4
    )
    assert trigger is False
    assert today is None


def test_eod_sync_decide_skips_when_fresh():
    friday = _dt.datetime(2026, 8, 14, 18, 40)
    trigger, reason, today = eod_sync_decide(
        lag=0, now=friday, sync_weekday=4
    )
    assert trigger is False
    trigger, reason, today = eod_sync_decide(
        lag=None, now=friday, sync_weekday=4
    )
    assert trigger is False


def test_eod_sync_decide_once_per_weekly_run():
    friday = _dt.datetime(2026, 8, 14, 18, 40)
    trigger, reason, today = eod_sync_decide(
        lag=2,
        now=friday,
        sync_weekday=4,
        last_trigger_day=_dt.date(2026, 8, 14),
    )
    assert trigger is False
    next_friday = _dt.datetime(2026, 8, 21, 18, 40)
    trigger, reason, today = eod_sync_decide(
        lag=2,
        now=next_friday,
        sync_weekday=4,
        last_trigger_day=_dt.date(2026, 8, 14),
    )
    assert trigger is True


# --------------------------------------------------- _auto_eod_sync (loop)

def _make_cfg_ctx(tmp_path):
    cfg = SimpleNamespace(market_data_root=tmp_path)
    ctx = SimpleNamespace(
        sync_state={"running": False},
        sync_lock=threading.Lock(),
    )
    return cfg, ctx


def test_auto_eod_sync_triggers_and_builds_command(monkeypatch, tmp_path):
    import subprocess
    import time as _time

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "00:00")
    monkeypatch.setenv("ASTOCK_MARKET_STORAGE_MODE", "overlay_v1")
    monkeypatch.setenv("ASTOCK_MARKET_GOVERNANCE_ENABLED", "0")
    monkeypatch.setenv("TUSHARE_TOKEN", "test_token_123")
    # state file lives under the test tree, never the real repo storage/
    state_path = tmp_path / "eod_sync_state.json"
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(state_path))

    cfg, ctx = _make_cfg_ctx(tmp_path)

    # Force the startup decision to trigger once; the polling loop then
    # sees the once-per-scheduled-run guard, like production.
    decide_n = {"n": 0}
    def _fake_decide(**k):
        decide_n["n"] += 1
        if decide_n["n"] == 1:
            return (True, "lag=3 个交易日", tmp_path)
        return (False, "今日已触发过自动同步", None)
    monkeypatch.setattr(api_mod, "eod_sync_decide", _fake_decide)

    calls = []
    class FakeProc:
        pid = 4242
        def wait(self, timeout=None):
            return 0  # simulate a successful child run for the watcher thread
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc(),
    )
    # exit the polling loop right after the startup check (the scheduler now
    # waits on wake_event instead of time.sleep once a run is in flight)
    def _break_sleep(_s):
        raise SystemExit("stop-loop")
    monkeypatch.setattr(_time, "sleep", _break_sleep)
    monkeypatch.setattr(
        threading.Event, "wait",
        lambda self, timeout=None: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )

    with pytest.raises(SystemExit, match="stop-loop"):
        _auto_eod_sync(cfg, ctx)

    assert len(calls) == 1, "exactly one sync process should be spawned"
    cmd = calls[0][0]
    assert "--source" in cmd and "tushare" in cmd
    mode_index = cmd.index("--mode")
    assert cmd[mode_index + 1] == "incremental"
    write_mode_index = cmd.index("--write-mode")
    assert cmd[write_mode_index + 1] == "delta"
    assert "--fresh" in cmd
    assert "--token" in cmd and "test_token_123" in cmd
    assert "--storage-root" in cmd and str(tmp_path) in cmd

    # trigger record is persisted for the UI status card (isolated state path)
    assert state_path.exists(), f"state file not written: {state_path}"
    import json as _json
    st = _json.loads(state_path.read_text(encoding="utf-8"))
    assert st.get("last_trigger_date")
    assert st.get("last_sync_started_at")
    assert st.get("enabled") is True


def test_auto_eod_sync_success_runs_governance(monkeypatch, tmp_path):
    import json as _json
    import subprocess
    import time as _time

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "00:00")
    monkeypatch.setenv("ASTOCK_MARKET_STORAGE_MODE", "overlay_v1")
    monkeypatch.setenv("ASTOCK_MARKET_GOVERNANCE_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_INDEX_ETF", "0")
    state_path = tmp_path / "eod_sync_state.json"
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(state_path))

    cfg, ctx = _make_cfg_ctx(tmp_path)
    decisions = {"n": 0}

    def _fake_decide(**_kwargs):
        decisions["n"] += 1
        if decisions["n"] == 1:
            return True, "weekly lag", _dt.date.today()
        return False, "already handled", None

    monkeypatch.setattr(api_mod, "eod_sync_decide", _fake_decide)
    calls = []

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def wait(self, timeout=None):
            return 0

    def _popen(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return FakeProc(4300 + len(calls))

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        threading.Event,
        "wait",
        lambda self, timeout=None: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )
    monkeypatch.setattr(
        _time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )

    with pytest.raises(SystemExit, match="stop-loop"):
        _auto_eod_sync(cfg, ctx)

    assert len(calls) == 2
    sync_cmd = calls[0][0]
    governance_cmd = calls[1][0]
    assert "sync_market_data.py" in " ".join(sync_cmd)
    assert "govern_market_data.py" in " ".join(governance_cmd)
    assert governance_cmd[-2:] == ["--maintain", "--apply"]
    assert "--storage-root" in governance_cmd
    assert str(tmp_path) in governance_cmd

    state = _json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_sync_exit_code"] == 0
    assert state["last_governance_exit_code"] == 0
    assert state["last_governance_finished_at"]


def test_auto_eod_sync_skips_when_fresh(monkeypatch, tmp_path):
    import subprocess
    import time as _time

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "00:00")
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(tmp_path / "eod_sync_state.json"))

    cfg, ctx = _make_cfg_ctx(tmp_path)
    monkeypatch.setattr(
        api_mod, "eod_sync_decide", lambda **k: (False, "数据已最新（lag=0）", None)
    )
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(_time, "sleep", lambda _s: (_ for _ in ()).throw(SystemExit("stop")))
    # the idle poll now waits on wake_event, not time.sleep
    monkeypatch.setattr(
        threading.Event, "wait",
        lambda self, timeout=None: (_ for _ in ()).throw(SystemExit("stop")),
    )

    with pytest.raises(SystemExit):
        _auto_eod_sync(cfg, ctx)
    assert calls == []


def test_auto_eod_sync_disabled_by_env(monkeypatch, tmp_path):
    import subprocess

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "0")
    calls = []
    monkeypatch.setattr(api_mod.subprocess, "Popen", lambda *a, **k: calls.append(a))

    cfg, ctx = _make_cfg_ctx(tmp_path)
    # disabled -> returns before the loop; no Popen at all
    _auto_eod_sync(cfg, ctx)
    assert calls == []


def test_auto_eod_sync_spawn_failure_records_failed_state(monkeypatch, tmp_path):
    """Popen raising must persist a non-zero exit record (never look "done").

    Regression: before the fix a spawn failure was only printed, so the day
    looked successful (missing last_sync_exit_code reads as 0) and the
    scheduler slept until tomorrow instead of retrying.
    """
    import json as _json
    import subprocess
    import time as _time

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "00:00")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_POLL_SECONDS", "60")
    state_path = tmp_path / "eod_sync_state.json"
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(state_path))

    cfg, ctx = _make_cfg_ctx(tmp_path)
    monkeypatch.setattr(
        api_mod, "eod_sync_decide", lambda **k: (True, "lag=3 个交易日", tmp_path)
    )
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    # stop the loop right after the startup check fires
    monkeypatch.setattr(_time, "sleep", lambda _s: (_ for _ in ()).throw(SystemExit("stop-loop")))
    monkeypatch.setattr(
        threading.Event, "wait",
        lambda self, timeout=None: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )

    with pytest.raises(SystemExit, match="stop-loop"):
        _auto_eod_sync(cfg, ctx)

    st = _json.loads(state_path.read_text(encoding="utf-8"))
    assert st.get("last_trigger_date") == _dt.date.today().strftime("%Y-%m-%d")
    assert st.get("last_sync_exit_code") != 0, "spawn failure must not look successful"
    assert st.get("retry_count") == 1
    assert st.get("pending_retry_at"), "retry must be re-armed for later"

    # startup check fires exactly once: the failed day must not re-fire in a
    # tight loop (the retry path waits for pending_retry_at instead)
    assert state_path.exists()


def test_auto_eod_sync_retry_respects_pending_interval(monkeypatch, tmp_path):
    """_check must not retry before pending_retry_at (restart bypass fix)."""
    import json as _json

    import wtpy.apps.astock.api as api_mod

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    state_path = tmp_path / "eod_sync_state.json"
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(state_path))

    future_pend = (_dt.datetime.now() + _dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    state_path.write_text(_json.dumps({
        "last_trigger_date": _dt.date.today().strftime("%Y-%m-%d"),
        "last_sync_started_at": "2026-08-10 18:31:00",
        "last_sync_exit_code": 1,
        "retry_count": 1,
        "pending_retry_at": future_pend,
    }), encoding="utf-8")

    # state says "failed today, retry 1/2 pending in the future": the startup
    # check must NOT clear the scheduled-run guard (would retry immediately,
    # bypassing the poll interval). Capture the last_trigger_day the decide
    # callback receives: with the fix it stays today; without it, the retry
    # branch would pass None and re-fire.
    captured = {}
    def _capture_decide(**k):
        captured["last_trigger_day"] = k.get("last_trigger_day")
        return (False, "no-trigger", None)
    monkeypatch.setattr(api_mod, "eod_sync_decide", _capture_decide)

    # drive the scheduler loop: let the pending-wait branch run once, then stop
    import time as _time
    import threading as _thr
    # The scheduler sleeps until sync_time (default 18:30) when the clock is
    # before it — CI runs at any hour, so never sleep for real. Without this
    # the loop would sleep to 18:30 UTC and the suite would hang for hours.
    monkeypatch.setattr(
        _time, "sleep",
        lambda _s: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )
    waits = {"n": 0}
    real_wait = _thr.Event.wait
    def _fake_wait(self, timeout=None):
        waits["n"] += 1
        if waits["n"] >= 2:
            raise SystemExit("stop-loop")
        return real_wait(self, timeout=0.01)
    monkeypatch.setattr(_thr.Event, "wait", _fake_wait)

    from wtpy.apps.astock.api import _auto_eod_sync
    cfg, ctx = _make_cfg_ctx(tmp_path)
    with pytest.raises(SystemExit, match="stop-loop"):
        _auto_eod_sync(cfg, ctx)

    assert captured.get("last_trigger_day") == _dt.date.today(), (
        "retry must not fire before pending_retry_at (scheduled-run guard kept)"
    )


def test_auto_eod_sync_runs_second_retry_then_exhausts_budget(
    monkeypatch, tmp_path
):
    import json as _json
    import subprocess
    import time as _time

    import wtpy.apps.astock.api as api_mod
    from wtpy.apps.astock.api import _auto_eod_sync

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "00:00")
    monkeypatch.setenv(
        "ASTOCK_EOD_SYNC_WEEKDAY", str(_dt.date.today().weekday())
    )
    monkeypatch.setenv("ASTOCK_EOD_SYNC_POLL_SECONDS", "60")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_MAX_RETRIES", "2")
    state_path = tmp_path / "eod_sync_state.json"
    monkeypatch.setenv("ASTOCK_EOD_STATE_PATH", str(state_path))
    state_path.write_text(_json.dumps({
        "last_trigger_date": _dt.date.today().strftime("%Y-%m-%d"),
        "last_sync_started_at": "2026-08-17 18:31:00",
        "last_sync_exit_code": 1,
        "retry_count": 2,
        "pending_retry_at": "2000-01-01 00:00:00",
    }), encoding="utf-8")

    monkeypatch.setattr(
        api_mod,
        "eod_sync_decide",
        lambda **_kwargs: (True, "retry due", _dt.date.today()),
    )
    calls = []

    class FailedProc:
        pid = 4402

        def wait(self, timeout=None):
            return 1

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, **kwargs: calls.append((list(cmd), kwargs)) or FailedProc(),
    )
    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        threading.Event,
        "wait",
        lambda self, timeout=None: (_ for _ in ()).throw(
            SystemExit("stop-loop")
        ),
    )
    monkeypatch.setattr(
        _time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(SystemExit("stop-loop")),
    )

    cfg, ctx = _make_cfg_ctx(tmp_path)
    with pytest.raises(SystemExit, match="stop-loop"):
        _auto_eod_sync(cfg, ctx)

    assert len(calls) == 1, "retry 2/2 must still be launched"
    state = _json.loads(state_path.read_text(encoding="utf-8"))
    assert state["retry_count"] == 3
    assert state["pending_retry_at"] is None


def test_eod_sync_status_api(tmp_path, monkeypatch):
    """/api/v1/eod-sync/status returns config + last trigger + data status."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.api_routes import system as system_mod
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data import dataset_store as ds_mod

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_TIME", "18:30")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_WEEKDAY", "invalid")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_MIN_LAG_DAYS", "invalid")
    monkeypatch.setenv("ASTOCK_EOD_SYNC_POLL_SECONDS", "1")
    # eod_sync/status resolves the state file via PROJECT_ROOT: pin it to the
    # test tree so the real repo storage/ is never read or written.
    monkeypatch.setattr(system_mod, "PROJECT_ROOT", tmp_path)

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)

    # write a trigger record so the endpoint shows a last-sync time (past
    # timestamps only — future-dated records are dropped as dirty)
    past_at = (_dt.datetime.now() - _dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    past_day = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
    state_path = tmp_path / "storage" / "astock" / "eod_sync_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"enabled": true, "last_trigger_date": "' + past_day + '", '
        '"last_sync_started_at": "' + past_at + '", "last_reason": "raw 数据滞后 1 个交易日", '
        '"sync_time": "18:30", "min_lag_days": 1, "poll_seconds": 1800}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ds_mod.DatasetStore, "load_manifest", lambda self, *a, **k: None)

    app = create_app(cfg)
    client = TestClient(app)
    r = client.get("/api/v1/eod-sync/status")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert j.get("enabled") is True
    assert j.get("sync_time") == "18:30"
    assert j.get("sync_weekday") == 4
    assert j.get("schedule_mode") == "weekly"
    assert j.get("min_lag_days") == 1
    assert j.get("poll_seconds") == 60
    assert j.get("last_sync_started_at") == past_at
    assert j.get("last_trigger_date") == past_day
    assert j.get("state_suspect") is False
    assert "last_sync_exit_code" in j
    assert "last_sync_finished_at" in j
    assert "retry_count" in j
    assert "pending_retry_at" in j
    # data status may be missing on an empty warehouse but never an error
    assert "data_status" in j


def test_eod_sync_status_api_sanitizes_future_records(tmp_path, monkeypatch):
    """Future-dated / unparseable state records are dropped and flagged
    state_suspect (a crash can persist a timestamp for a sync that never ran)."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.api_routes import system as system_mod
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data import dataset_store as ds_mod

    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "1")
    monkeypatch.setattr(system_mod, "PROJECT_ROOT", tmp_path)

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)

    future_at = (_dt.datetime.now() + _dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    future_day = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    state_path = tmp_path / "storage" / "astock" / "eod_sync_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"enabled": true, "last_trigger_date": "' + future_day + '", '
        '"last_sync_started_at": "' + future_at + '", "last_reason": "raw 数据滞后 1 个交易日", '
        '"sync_time": "18:30", "min_lag_days": 1, "poll_seconds": 1800}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ds_mod.DatasetStore, "load_manifest", lambda self, *a, **k: None)

    app = create_app(cfg)
    client = TestClient(app)
    r = client.get("/api/v1/eod-sync/status")
    assert r.status_code == 200
    j = r.json()
    assert j.get("last_sync_started_at") is None
    assert j.get("last_trigger_date") is None
    assert j.get("state_suspect") is True
    assert "data_status" in j


def test_eod_sync_status_cached_30s(tmp_path, monkeypatch):
    """Repeated /api/v1/eod-sync/status within 30s reuses the cached payload
    (the underlying tushare product health scan is not re-run)."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.api_routes import system as system_mod
    from wtpy.apps.astock.config import get_default_config
    from wtpy.apps.astock.data import tushare_product as tp_mod

    # Disable the background auto-sync thread so it cannot race the counter.
    monkeypatch.setenv("ASTOCK_EOD_SYNC_ENABLED", "0")
    # state file resolved via PROJECT_ROOT: keep it inside the test tree
    monkeypatch.setattr(system_mod, "PROJECT_ROOT", tmp_path)

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)

    calls = {"n": 0}

    def _fake_health(*a, **k):
        calls["n"] += 1
        return {"status": "healthy", "trading_day_lag": {"raw": 0}}

    app = create_app(cfg)
    monkeypatch.setattr(tp_mod, "tushare_product_data_health", _fake_health)

    client = TestClient(app)
    r1 = client.get("/api/v1/eod-sync/status")
    r2 = client.get("/api/v1/eod-sync/status")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert calls["n"] == 1
    assert r2.json()["data_status"] == "healthy"


# ------------------------------------------------- data_sync_start guards

def _client_for(tmp_path, monkeypatch):
    """TestClient with the system route's PROJECT_ROOT pinned to tmp_path so
    the EOD state file lives under the test tree, never the real repo."""
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app
    from wtpy.apps.astock.api_routes import system as system_mod
    from wtpy.apps.astock.config import get_default_config

    monkeypatch.setattr(system_mod, "PROJECT_ROOT", tmp_path)

    storage = tmp_path / "st"
    storage.mkdir()
    ind = tmp_path / "ind"
    ind.mkdir()
    cfg = get_default_config(storage_root=storage, indicator_dir=ind)
    md_root = Path(cfg.market_data_root)
    md_root.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    return TestClient(app), cfg, md_root


class _FakeSyncProc:
    """Stands in for subprocess.Popen in _run_sync_process (empty output)."""
    pid = 12345
    returncode = 0

    def __init__(self):
        import io
        self.stdout = io.StringIO()

    def wait(self, timeout=None):
        return 0


def test_data_sync_start_409_while_eod_child_alive(tmp_path, monkeypatch):
    """Manual tushare/factor start is refused while the EOD child lives;
    derive/ca are not guarded (they do not touch the same checkpoints)."""
    import json as _json
    import os as _os

    client, cfg, md_root = _client_for(tmp_path, monkeypatch)

    state_path = tmp_path / "storage" / "astock" / "eod_sync_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # this test process is alive, so _pid_alive(os.getpid()) is True
    state_path.write_text(
        _json.dumps({
            "sync_pid": _os.getpid(),
            "last_sync_started_at": "2026-08-10 18:38:33",
        }),
        encoding="utf-8",
    )

    r = client.post("/api/v1/data-sync/start",
                    json={"task": "tushare", "end_date": 20260810})
    assert r.status_code == 409
    assert "EOD" in r.json().get("detail", "")

    r = client.post("/api/v1/data-sync/start",
                    json={"task": "factor", "end_date": 20260810})
    assert r.status_code == 409

    # derive/ca never share the EOD raw/factor surface: no EOD guard
    r = client.post("/api/v1/data-sync/start",
                    json={"task": "derive", "end_date": 20260810})
    assert r.status_code == 200


def test_data_sync_start_auto_resumes_leftover_checkpoint(tmp_path, monkeypatch):
    """A checkpoint left by an interrupted run must not fail closed on a bare
    manual start: the UI auto-appends --resume for tushare/factor."""
    import io as _io
    import subprocess as _sp

    client, cfg, md_root = _client_for(tmp_path, monkeypatch)

    sync_logs = md_root / "sync_logs"
    sync_logs.mkdir(parents=True, exist_ok=True)
    (sync_logs / "checkpoint_tushare_incremental_1d.json").write_text(
        '{"sync_run_id": "abc"}', encoding="utf-8"
    )

    calls = []
    monkeypatch.setattr(_sp, "Popen",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeSyncProc())

    r = client.post("/api/v1/data-sync/start",
                    json={"task": "tushare", "end_date": 20260810})
    assert r.status_code == 200
    assert len(calls) == 1
    assert "--resume" in calls[0]
    assert "--fresh" not in calls[0]

    # explicit --fresh still wins over the auto --resume
    calls.clear()
    r = client.post("/api/v1/data-sync/start",
                    json={"task": "tushare", "end_date": 20260810, "fresh": True})
    assert r.status_code == 200
    assert "--fresh" in calls[0]

    # factor task: same auto-resume, and the required universe file is taken
    # from the environment when no manifest exists
    universe = tmp_path / "universe.csv"
    universe.write_text("ts_code\n000001.SZ\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_FACTOR_UNIVERSE_FILE", str(universe))
    (sync_logs / "checkpoint_tushare_adj_factor_1d.json").write_text(
        '{"sync_run_id": "xyz"}', encoding="utf-8"
    )
    calls.clear()
    r = client.post("/api/v1/data-sync/start",
                    json={"task": "factor", "end_date": 20260810})
    assert r.status_code == 200
    assert "--resume" in calls[0]
    assert "--universe-file" in calls[0]

    # no checkpoint -> no auto --resume
    (sync_logs / "checkpoint_tushare_incremental_1d.json").unlink()
    calls.clear()
    r = client.post("/api/v1/data-sync/start",
                    json={"task": "tushare", "end_date": 20260810})
    assert r.status_code == 200
    assert "--resume" not in calls[0]
    assert "--fresh" not in calls[0]
