"""Cross-platform sync lock: import safety, exclusivity, stale recovery, metadata."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wtpy.apps.astock.data.sync_lock import SyncTaskLock, SyncLockHeldError


class TestImportSafety:
    def test_module_importable_without_fcntl_on_windows(self):
        # Importing the module must never require fcntl on win32.
        import wtpy.apps.astock.data.sync_lock as m
        src = Path(m.__file__).read_text(encoding="utf-8")
        # fcntl may only be imported inside a non-win32 branch, never at module top level.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("import fcntl") or stripped.startswith("from fcntl"):
                assert line.startswith("    ") or line.startswith("\t"), (
                    "fcntl imported at module top level — breaks Windows"
                )

    def test_cli_script_has_no_unconditional_fcntl(self):
        script = Path(__file__).resolve().parents[3] / "scripts" / "sync_market_data.py"
        src = script.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped == "import fcntl" or stripped.startswith("import fcntl "):
                pytest.fail(f"scripts/sync_market_data.py:{i} unconditionally imports fcntl")


class TestExclusivity:
    def test_acquire_release_and_metadata(self, tmp_path):
        lock = SyncTaskLock(tmp_path, source="local_vendor", adjustment="none",
                            period="1d", sync_run_id="run_x")
        lock.acquire()
        try:
            meta = SyncTaskLock.probe(lock.lock_path)
            assert meta["pid"] == os.getpid()
            assert meta["source"] == "local_vendor"
            assert meta["adjustment"] == "none"
            assert meta["period"] == "1d"
            assert meta["sync_run_id"] == "run_x"
            assert meta["hostname"]
            assert meta["start_time"]
            assert meta["market_data_root"] == str(tmp_path)
        finally:
            lock.release()

    def test_second_holder_same_scope_rejected(self, tmp_path):
        a = SyncTaskLock(tmp_path, source="local_vendor", adjustment="none", period="1d")
        b = SyncTaskLock(tmp_path, source="local_vendor", adjustment="none", period="1d")
        a.acquire()
        try:
            with pytest.raises(SyncLockHeldError) as ei:
                b.acquire()
            assert ei.value.holder.get("pid") == os.getpid()
        finally:
            a.release()
        # after release the scope is acquirable again
        b.acquire()
        b.release()

    def test_different_scopes_do_not_conflict(self, tmp_path):
        a = SyncTaskLock(tmp_path, source="local_vendor", adjustment="none", period="1d")
        b = SyncTaskLock(tmp_path, source="tushare", adjustment="qfq", period="1d")
        a.acquire()
        try:
            b.acquire()
            b.release()
        finally:
            a.release()

    def test_reacquire_after_release(self, tmp_path):
        lock = SyncTaskLock(tmp_path, source="s", adjustment="a", period="p")
        lock.acquire()
        lock.release()
        lock2 = SyncTaskLock(tmp_path, source="s", adjustment="a", period="p")
        lock2.acquire()
        lock2.release()


HOLD_SCRIPT = r"""
import sys, time
sys.path.insert(0, {proj!r})
from wtpy.apps.astock.data.sync_lock import SyncTaskLock
lock = SyncTaskLock({root!r}, source="local_vendor", adjustment="none",
                    period="1d", sync_run_id="child_hold")
lock.acquire()
print("LOCKED", flush=True)
time.sleep({hold})
lock.release()
print("RELEASED", flush=True)
"""

DIE_SCRIPT = r"""
import os, sys
sys.path.insert(0, {proj!r})
from wtpy.apps.astock.data.sync_lock import SyncTaskLock
lock = SyncTaskLock({root!r}, source="local_vendor", adjustment="none",
                    period="1d", sync_run_id="child_dead")
lock.acquire()
print("LOCKED", flush=True)
os._exit(1)  # die without releasing — simulates crash
"""


def _proj_root() -> str:
    return str(Path(__file__).resolve().parents[3])


class TestCrossProcess:
    def test_live_holder_blocks_other_process(self, tmp_path):
        code = HOLD_SCRIPT.format(proj=_proj_root(), root=str(tmp_path), hold=15)
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        try:
            assert p.stdout.readline().strip() == "LOCKED"
            mine = SyncTaskLock(tmp_path, source="local_vendor",
                                adjustment="none", period="1d")
            with pytest.raises(SyncLockHeldError) as ei:
                mine.acquire()
            assert ei.value.holder.get("sync_run_id") == "child_hold"
            assert ei.value.holder.get("pid") == p.pid
        finally:
            p.kill()
            p.wait(timeout=10)

    def test_stale_lock_recovered_after_holder_death(self, tmp_path):
        code = DIE_SCRIPT.format(proj=_proj_root(), root=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=30)
        assert "LOCKED" in p.stdout
        # holder died without release: OS dropped its lock; metadata remains
        assert SyncTaskLock.probe(
            tmp_path / ".locks" / "sync_local_vendor_none_1d.lock"
        )["sync_run_id"] == "child_dead"
        mine = SyncTaskLock(tmp_path, source="local_vendor",
                            adjustment="none", period="1d", sync_run_id="recoverer")
        mine.acquire()  # must succeed — stale lock self-heals
        try:
            assert mine.recovered_stale is not None
            assert mine.recovered_stale.get("sync_run_id") == "child_dead"
            assert mine.recovered_stale.get("holder_alive") in (False, None)
            # metadata now points at us
            meta = SyncTaskLock.probe(mine.lock_path)
            assert meta["sync_run_id"] == "recoverer"
        finally:
            mine.release()

    def test_killed_holder_lock_freed(self, tmp_path):
        code = HOLD_SCRIPT.format(proj=_proj_root(), root=str(tmp_path), hold=60)
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "LOCKED"
        p.kill()
        p.wait(timeout=10)
        deadline = time.time() + 10
        last_err = None
        while time.time() < deadline:
            mine = SyncTaskLock(tmp_path, source="local_vendor",
                                adjustment="none", period="1d")
            try:
                mine.acquire()
                mine.release()
                return
            except SyncLockHeldError as e:  # pragma: no cover - transient on slow OS
                last_err = e
                time.sleep(0.5)
        pytest.fail(f"lock not freed after holder killed: {last_err}")
