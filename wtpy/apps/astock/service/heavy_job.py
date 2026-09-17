# -*- coding: utf-8 -*-
"""heavy-job 全局锁与抢锁失败的持久化重试（契约 §7）。

为什么需要（9/13 OOM 教训）：周五链、手动 CLI 回填、网页现算三类入口都可能
同时启动"全市场扫描/跟踪结算"这类重任务——不互斥会双跑同一周（浪费资源、
两进程同时 publish 同一 week 指针 last-wins）并叠高内存峰值。契约要求一把
全局锁 + 抢锁失败不丢任务（持久化待办 + 服务运行期有界退避重试，不依赖重启、
不等到下周五）。

设计：
- 锁键固定 `heavy_job:screen_track`（**不复用** sync_lock 的
  (root,source,adjustment,period) 键体系——那是行情同步的粒度）；
- 字节范围锁由**执行方自己持有**（子进程/worker 线程），父进程不持锁——
  否则父持锁后子进程再申请会自我阻塞；
- OS 在进程退出时自动释放锁（崩溃不留死锁）；
- 抢锁失败 → record_pending_job（按 task_key 记账，退避 5/15/30 分钟，
  耗尽保留欠账）+ 返回 skippable 结果，绝不静默丢弃。
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import screen_contract as sc

logger = logging.getLogger(__name__)

#: 锁文件字节偏移：远离元数据区，避免 Windows 强制字节锁挡住元数据读取
_LOCK_BYTE_OFFSET = 1024 * 1024

#: 同进程同线程可重入（backfill 循环内会进程内调用 review-weekly——若锁
#: 不可重入会自我阻塞）。键 = (锁路径, 线程 ident)：同线程重入放行，
#: 同进程其他线程仍互斥（API worker 与主线程并发场景）。
_REENTRANT_DEPTH: Dict[tuple, int] = {}
_REENTRANT_GUARD = threading.Lock()


class HeavyJobLockHeld(RuntimeError):
    """另一进程/线程正持有 heavy-job 锁。"""

    def __init__(self, message: str, holder: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.holder = holder or {}


def heavy_job_lock_path(storage_root: Path) -> Path:
    safe = sc.HEAVY_JOB_LOCK_KEY.replace(":", "_")
    return Path(storage_root) / ".locks" / f"{safe}.lock"


class HeavyJobLock:
    """跨进程独占锁（字节范围锁；进程退出自动释放，无陈旧锁问题）。"""

    def __init__(self, storage_root: Path, *, task_key: str = ""):
        self.storage_root = Path(storage_root)
        self.task_key = task_key
        self.lock_path = heavy_job_lock_path(self.storage_root)
        self._fd: Optional[int] = None
        self._reentrant = False

    def _reentrancy_key(self) -> tuple:
        return (str(self.lock_path), threading.get_ident())

    def _metadata(self) -> Dict[str, Any]:
        return {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "task_key": self.task_key,
            "lock_key": sc.HEAVY_JOB_LOCK_KEY,
        }

    @staticmethod
    def probe(lock_path: Path) -> Optional[Dict[str, Any]]:
        """读持有者元数据（不申请锁）；不可读 → None。"""
        import json

        try:
            raw = Path(lock_path).read_text(encoding="utf-8").strip()
            if not raw:
                return None
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return None

    def acquire(self) -> "HeavyJobLock":
        import json

        # 同线程重入：直接放行（不重复申请 OS 锁，避免自我阻塞）
        rkey = self._reentrancy_key()
        with _REENTRANT_GUARD:
            if _REENTRANT_DEPTH.get(rkey, 0) > 0:
                _REENTRANT_DEPTH[rkey] += 1
                self._reentrant = True
                return self

        prior = self.probe(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise HeavyJobLockHeld(
                        f"heavy-job 锁被占用（{sc.HEAVY_JOB_LOCK_KEY}）",
                        holder=prior,
                    ) from None
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise HeavyJobLockHeld(
                        f"heavy-job 锁被占用（{sc.HEAVY_JOB_LOCK_KEY}）",
                        holder=prior,
                    ) from None
            # 元数据写在文件头（锁字节远在 1MB 偏移，读写互不阻塞）
            os.lseek(fd, 0, os.SEEK_SET)
            payload = json.dumps(self._metadata(), ensure_ascii=False).encode("utf-8")
            os.write(fd, payload + b" " * max(0, 4096 - len(payload)))
            self._fd = fd
            with _REENTRANT_GUARD:
                _REENTRANT_DEPTH[rkey] = 1
            return self
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._reentrant:
            rkey = self._reentrancy_key()
            with _REENTRANT_GUARD:
                depth = _REENTRANT_DEPTH.get(rkey, 0)
                if depth > 1:
                    _REENTRANT_DEPTH[rkey] = depth - 1
                else:
                    _REENTRANT_DEPTH.pop(rkey, None)
            self._reentrant = False
            return
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with _REENTRANT_GUARD:
                _REENTRANT_DEPTH.pop(self._reentrancy_key(), None)
            os.close(fd)

    def __enter__(self) -> "HeavyJobLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 抢锁失败 → 待办 + 服务运行期有界退避重试
# ---------------------------------------------------------------------------


def record_lock_skip(storage_root: Path, task_key: str) -> Dict[str, Any]:
    """抢锁失败记账（持久化待办；退避表与耗尽语义在 screen_contract）。"""
    return sc.record_pending_job(storage_root, task_key, reason="skipped_locked")


def record_runner_exit(
    storage_root: Path, task_key: str, rc: int
) -> Optional[Dict[str, Any]]:
    """重试 runner 收到子进程退出码后的记账（单一记账方语义，契约 §7）。

    - 0（完成）/ 3（可重试，子进程内已按真实 completion 记账）→ 不记，返回 None；
    - 1/2（配置/计算异常）→ 标欠账（exhausted，保留待人工处理）；
    - 其余码（信号杀死/OOM/崩溃——Windows 下是 0xC00000xx 大数）→ 按有界
      退避记账。**绝不落进"不记账"路径**：待办 recorded_at 不推进会立即又
      到期，每 120s 空转一次且（max_per_pass=1）占死队列头，饿死其他待办。
    """
    if rc in (0, 3):
        return None
    if rc in (1, 2):
        return sc.record_pending_job(
            storage_root, task_key, reason=f"non_retryable_exit_{rc}",
            mark_exhausted=True,
        )
    return sc.record_pending_job(
        storage_root, task_key, reason=f"unexpected_exit_{rc}"
    )


def record_runner_spawn_failure(
    storage_root: Path, task_key: str
) -> Dict[str, Any]:
    """Popen 启动失败的记账：有界退避（同 unexpected_exit 的空转/饿死理由）。"""
    return sc.record_pending_job(storage_root, task_key, reason="spawn_failed")


def pending_retry_due(
    storage_root: Path, *, now_ts: Optional[float] = None
) -> List[Dict[str, Any]]:
    """到期可重试的待办任务（next_retry_in_minutes 到点）。

    只返回**未耗尽**的待办：耗尽项保留为欠账（UI 展示 + 手动补跑入口），
    自动重试不再无休止地撞。
    """
    data = sc.load_pending_jobs(Path(storage_root))
    out: List[Dict[str, Any]] = []
    now = float(now_ts if now_ts is not None else time.time())
    for key, job in (data.get("jobs") or {}).items():
        if job.get("exhausted"):
            continue
        mins = job.get("next_retry_in_minutes")
        recorded = str(job.get("recorded_at") or "")
        due_at = None
        if recorded and mins is not None:
            try:
                t = time.mktime(time.strptime(recorded, "%Y-%m-%d %H:%M:%S"))
                due_at = t + float(mins) * 60.0
            except Exception:  # noqa: BLE001
                due_at = None
        if due_at is None or now >= due_at:
            out.append({**job, "task_key": key, "due_at": due_at})
    return out


def retry_due_jobs(
    storage_root: Path,
    *,
    runner,
    now_ts: Optional[float] = None,
    max_per_pass: int = 1,
) -> List[Dict[str, Any]]:
    """对到期待办逐个尝试执行（服务运行期调用；不依赖重启）。

    ``runner(task_key) -> bool``：执行成功返回 True → 清待办；失败/抢锁再败
    → 由 runner 自行 record_lock_skip（记账递增、退避推进），本函数不重复记账。
    每轮限制次数（默认 1）：重任务串行，避免一次扫描把内存打满。
    """
    results: List[Dict[str, Any]] = []
    for job in pending_retry_due(storage_root, now_ts=now_ts)[: max(1, int(max_per_pass))]:
        key = str(job["task_key"])
        try:
            done = bool(runner(key))
        except Exception as e:  # noqa: BLE001 — 单任务失败不影响其他待办
            logger.warning("heavy_job 重试 %s 失败: %s", key, e)
            done = False
        if done:
            # 只清待办：TaskState 由服务层（compute_weekly_tracking）按真实
            # completion 写权威值——这里再写一次会覆盖成 complete，丢掉
            # no_trading_week/pending 语义（自查修正）
            sc.clear_pending_job(Path(storage_root), key)
        results.append({"task_key": key, "done": done})
    return results


def run_with_heavy_lock(
    cfg,
    task_key: str,
    *,
    fn,
) -> Dict[str, Any]:
    """执行方持锁调用 ``fn()``；抢锁失败 → 记账 + 返回 skipped。

    锁失效/异常仍照常释放（with 语义）——绝不因为业务异常把锁留成死锁。
    """
    lock = HeavyJobLock(Path(cfg.storage_root), task_key=task_key)
    try:
        lock.acquire()
    except HeavyJobLockHeld as e:
        info = record_lock_skip(Path(cfg.storage_root), task_key)
        return {
            "skipped_locked": True,
            "task_key": task_key,
            "holder": e.holder,
            "pending": info,
            "reason": "heavy_job_lock_held",
        }
    try:
        return {"skipped_locked": False, "value": fn()}
    finally:
        lock.release()
