"""Cross-platform exclusive lock for market-data sync tasks.

Scope rule (documented contract):
  One lock guards one (market_data_root, source, adjustment, period) tuple.
  - Two tasks with the SAME tuple can never run concurrently.
  - Tasks with different tuples (e.g. different source, or different data
    root) use different lock files and MAY run concurrently.

Implementation:
  The holder keeps an OS-level byte-range lock on an open file handle for
  the whole task (msvcrt.locking on Windows, fcntl.flock on POSIX).  The
  OS releases the lock automatically when the process exits or dies, so a
  crashed holder can never block future runs forever (stale locks
  self-heal).  The lock file additionally stores holder metadata as JSON
  so a blocked task can report who is holding the lock; metadata left by
  a dead holder is detected and reported as recovered.

`fcntl` is only imported on POSIX platforms — importing this module on
Windows is always safe.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional


# The exclusive byte is locked far beyond any metadata JSON so that reads of
# the metadata region are never blocked by Windows' mandatory byte locks.
_LOCK_BYTE_OFFSET = 1024 * 1024


class SyncLockHeldError(RuntimeError):
    """Raised when another live process already holds the sync lock."""

    def __init__(self, message: str, holder: Optional[dict] = None):
        super().__init__(message)
        self.holder = holder or {}


def _pid_alive(pid: int) -> Optional[bool]:
    """Best-effort liveness probe. Returns None when undeterminable.

    NOTE: never uses os.kill on Windows (os.kill(pid, 0) would TERMINATE
    the target process there).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


class SyncTaskLock:
    """Exclusive lock for one (data_root, source, adjustment, period) sync task."""

    def __init__(
        self,
        market_data_root: Path | str,
        *,
        source: str,
        adjustment: str,
        period: str,
        sync_run_id: str = "",
    ):
        self.market_data_root = Path(market_data_root)
        self.source = source
        self.adjustment = adjustment
        self.period = period
        self.sync_run_id = sync_run_id
        lock_dir = self.market_data_root / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        safe = f"{source}_{adjustment}_{period}".replace("/", "_")
        self.lock_path = lock_dir / f"sync_{safe}.lock"
        self._fd: Optional[int] = None
        self.recovered_stale: Optional[dict] = None

    # ------------------------------------------------------------------
    def _metadata(self) -> dict:
        return {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "start_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": self.source,
            "adjustment": self.adjustment,
            "period": self.period,
            "market_data_root": str(self.market_data_root),
            "sync_run_id": self.sync_run_id,
        }

    @staticmethod
    def probe(lock_path: Path) -> Optional[dict]:
        """Read holder metadata without acquiring. None if unreadable."""
        try:
            raw = Path(lock_path).read_text(encoding="utf-8").strip()
            if not raw:
                return None
            return json.loads(raw)
        except Exception:
            return None

    # ------------------------------------------------------------------
    def acquire(self) -> "SyncTaskLock":
        prior = self.probe(self.lock_path)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise SyncLockHeldError(self._held_message(prior), prior)
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise SyncLockHeldError(self._held_message(prior), prior)
        except SyncLockHeldError:
            os.close(fd)
            raise
        except Exception:
            os.close(fd)
            raise

        # Lock acquired. If metadata from a previous holder is present it
        # was stale (its OS lock died with its process) — record & report.
        if prior and prior.get("pid") not in (None, os.getpid()):
            alive = _pid_alive(int(prior.get("pid") or -1))
            self.recovered_stale = {**prior, "holder_alive": alive}

        self._fd = fd
        payload = json.dumps(self._metadata(), ensure_ascii=False, indent=1)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        return self

    def _held_message(self, holder: Optional[dict]) -> str:
        if holder:
            pid = holder.get("pid")
            alive = _pid_alive(int(pid or -1))
            return (
                f"Sync lock held: {self.lock_path} | holder pid={pid} "
                f"host={holder.get('hostname')} start={holder.get('start_time')} "
                f"sync_run_id={holder.get('sync_run_id')} alive={alive}"
            )
        return f"Sync lock held: {self.lock_path} (holder metadata unavailable)"

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            try:
                meta = self._metadata()
                meta["released_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                payload = json.dumps(meta, ensure_ascii=False, indent=1)
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            except Exception:
                pass
            if sys.platform == "win32":
                import msvcrt

                try:
                    os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    def __enter__(self) -> "SyncTaskLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
