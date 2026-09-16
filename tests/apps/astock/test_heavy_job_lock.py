# -*- coding: utf-8 -*-
"""阶段 2 契约 §7 测试：heavy-job 锁 / 抢锁待办 / 服务运行期有界退避重试。

覆盖评审要求的子用例：「服务不重启，抢锁失败的任务仍最终得到补跑」
（retry_due_jobs 直接驱动，不需要重启进程）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.service import heavy_job as hj
from wtpy.apps.astock.service import screen_contract as sc


class TestHeavyJobLock:
    def test_same_thread_reentrant(self, tmp_path):
        """同线程可重入：backfill 循环内进程内调用 review-weekly 不自锁。"""
        l1 = hj.HeavyJobLock(tmp_path, task_key="backfill_8")
        l2 = hj.HeavyJobLock(tmp_path, task_key="track_20260911")
        l1.acquire()
        try:
            l2.acquire()  # 同线程第二次申请：放行（不自我阻塞）
            l2.release()
        finally:
            l1.release()
        # 释放后可再次正常获取
        l3 = hj.HeavyJobLock(tmp_path)
        l3.acquire()
        l3.release()

    def test_cross_thread_exclusion_with_holder_info(self, tmp_path):
        """跨线程互斥 + 抢锁者能看到持有者信息（pid/task_key）。"""
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="track_20260911")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        th = threading.Thread(target=hold)
        th.start()
        try:
            assert acquired.wait(3)
            with pytest.raises(hj.HeavyJobLockHeld) as ei:
                hj.HeavyJobLock(tmp_path, task_key="track_20260918").acquire()
            holder = ei.value.holder or {}
            assert holder.get("task_key") == "track_20260911"
            assert holder.get("lock_key") == sc.HEAVY_JOB_LOCK_KEY
        finally:
            release.set()
            th.join(5)

    def test_lock_released_on_exception(self, tmp_path):
        """业务异常不得留死锁（with 语义必释放）。"""
        with pytest.raises(RuntimeError):
            with hj.HeavyJobLock(tmp_path, task_key="x"):
                raise RuntimeError("boom")
        lk = hj.HeavyJobLock(tmp_path, task_key="y")
        lk.acquire()
        lk.release()


class TestLockSkipAndRetry:
    def test_run_with_heavy_lock_records_pending_when_held(self, tmp_path):
        """抢锁失败 → skipped + 持久化待办（绝不静默丢弃）。"""
        cfg = type("C", (), {"storage_root": tmp_path})()
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="holder")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        th = threading.Thread(target=hold)
        th.start()
        try:
            assert acquired.wait(3)
            out = hj.run_with_heavy_lock(cfg, "track_20260911", fn=lambda: {"n": 1})
            assert out["skipped_locked"] is True
            assert out["pending"]["attempts"] == 1
            assert out["pending"]["next_retry_in_minutes"] == 5
            jobs = sc.load_pending_jobs(tmp_path)["jobs"]
            assert "track_20260911" in jobs
        finally:
            release.set()
            th.join(5)

    def test_retry_due_jobs_completes_without_restart(self, tmp_path):
        """★评审子用例：服务不重启，抢锁失败的任务最终被补跑成功。

        路径：抢锁失败记待办 → 锁释放（另一任务结束）→ 退避到点后
        retry_due_jobs 驱动 runner 成功 → 待办清除 + 状态置 complete。
        注：持锁必须用**独立线程**——同线程申请是可重入放行的（backfill
        嵌套语义），模拟"另一个重任务在跑"要跨线程。
        """
        cfg = type("C", (), {"storage_root": tmp_path})()
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="holder")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        th = threading.Thread(target=hold)
        th.start()
        assert acquired.wait(3)
        # 1) 锁被别的重任务占着 → 记待办
        out = hj.run_with_heavy_lock(cfg, "track_20260911", fn=lambda: {"n": 1})
        assert out["skipped_locked"] is True
        release.set()
        th.join(5)  # 持有者结束（模拟另一个重任务跑完）

        # 2) 退避未到：不重试
        assert hj.pending_retry_due(tmp_path, now_ts=time.time()) == []
        # 3) 退避到点（5 分钟后）：同一进程内直接驱动重试（不重启）
        calls = []

        def runner(task_key: str) -> bool:
            calls.append(task_key)
            return True  # 补跑成功

        res = hj.retry_due_jobs(
            tmp_path, runner=runner, now_ts=time.time() + 6 * 60,
        )
        assert res == [{"task_key": "track_20260911", "done": True}]
        assert calls == ["track_20260911"]
        # 待办清除；TaskState 必须由服务层按真实 completion 写——重试助手
        # 不得代写（否则会把 no_trading_week/pending 覆盖成 complete）
        assert sc.load_pending_jobs(tmp_path)["jobs"] == {}
        assert sc.load_task_state(tmp_path, "track_20260911") is None

    def test_retry_failure_keeps_debt_until_exhausted(self, tmp_path):
        """补跑失败：退避递增（5→15→30），耗尽后保留欠账不再自动重试。"""
        cfg = type("C", (), {"storage_root": tmp_path})()
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="holder")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        th = threading.Thread(target=hold)
        th.start()
        assert acquired.wait(3)
        hj.run_with_heavy_lock(cfg, "track_20260911", fn=lambda: None)
        release.set()
        th.join(5)

        def fail_runner(task_key: str) -> bool:
            hj.record_lock_skip(tmp_path, task_key)  # 模拟再次失败/再次被占
            return False

        t = time.time()
        for i, expected_backoff in enumerate((15, 30), start=2):
            hj.retry_due_jobs(tmp_path, runner=fail_runner, now_ts=t + i * 3600)
            info = sc.load_pending_jobs(tmp_path)["jobs"]["track_20260911"]
            assert info["attempts"] == i
            assert info["next_retry_in_minutes"] == expected_backoff
        # 第 4 次 → 耗尽（保留欠账，不再自动重试）
        hj.retry_due_jobs(tmp_path, runner=fail_runner, now_ts=t + 5 * 3600)
        info = sc.load_pending_jobs(tmp_path)["jobs"]["track_20260911"]
        assert info["attempts"] == 4 and info["exhausted"] is True
        assert hj.pending_retry_due(tmp_path, now_ts=t + 99 * 3600) == []

    def test_max_per_pass_serializes_heavy_jobs(self, tmp_path):
        """每轮最多补 1 个（9/13 OOM 教训：重任务绝不并发）。"""
        for wk in (20260904, 20260911, 20260918):
            sc.record_pending_job(tmp_path, f"track_{wk}", reason="skipped_locked")
        seen = []
        hj.retry_due_jobs(
            tmp_path, runner=lambda k: seen.append(k) or True,
            now_ts=time.time() + 24 * 3600, max_per_pass=1,
        )
        assert len(seen) == 1

    def test_pending_jobs_ordered_by_due_isolation(self, tmp_path):
        """不同周任务各自记账：一个耗尽不影响另一个的自动重试。"""
        for _ in range(4):
            hj.record_lock_skip(tmp_path, "track_20260911")
        hj.record_lock_skip(tmp_path, "track_20260918")
        due = hj.pending_retry_due(tmp_path, now_ts=time.time() + 24 * 3600)
        keys = [d["task_key"] for d in due]
        assert keys == ["track_20260918"]  # 0911 已耗尽，仅欠账展示


class TestCliWiring:
    def test_track_week_holds_lock(self, tmp_path, monkeypatch):
        """CLI 单周路径在锁内计算；锁被占 → 退出 3 + 待办。"""
        from wtpy.apps.astock import cli as climod

        cfg = type(
            "C", (),
            {"storage_root": tmp_path, "market_data_root": tmp_path,
             "calendar_path": tmp_path / "cal.json"},
        )()

        import argparse

        args = argparse.Namespace(
            week="20260911", previous_week=False, force=False,
            run_kind="weekly_chain", backfill=0,
        )
        called = {}

        class FakeTracksvc:
            TRACKING_ALGO_VERSION = "1"

            @staticmethod
            def week_task_key(week):
                return f"track_{int(week)}"

            @staticmethod
            def compute_weekly_tracking(cfg_, week, **kw):
                called["ran"] = True
                return {"completion": sc.TRACK_COMPLETE, "week_id": week}

        monkeypatch.setattr(climod, "tracksvc", FakeTracksvc, raising=False)
        # 正常路径：锁内计算
        rc, out = climod._run_one_track_week(cfg, 20260911, args)
        assert rc == climod._TRACK_EXIT_OK
        assert called.get("ran") is True
        assert out["completion"] == sc.TRACK_COMPLETE

        # 锁被占：另一线程持锁 → 本线程（新线程模拟）抢锁失败
        acquired = threading.Event()
        release = threading.Event()
        result = {}

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="holder")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        def run_in_thread():
            rc2, out2 = climod._run_one_track_week(cfg, 20260918, args)
            result["rc"] = rc2
            result["out"] = out2

        th_hold = threading.Thread(target=hold)
        th_hold.start()
        assert acquired.wait(3)
        th_run = threading.Thread(target=run_in_thread)
        th_run.start()
        th_run.join(10)
        release.set()
        th_hold.join(5)

        assert result["rc"] == climod._TRACK_EXIT_RETRYABLE
        assert result["out"]["completion"] == "skipped_locked"
        assert "track_20260918" in sc.load_pending_jobs(tmp_path)["jobs"]

    def test_retryable_completion_records_real_reason(self, tmp_path, monkeypatch):
        """★审查 🟡：exit 3 的待办原因按真实 completion 记（pending ≠ 锁占用），
        且 key 用周身份（track_{week_id}），单一记账方不再双重累加。"""
        from wtpy.apps.astock import cli as climod

        cfg = type(
            "C", (),
            {"storage_root": tmp_path, "market_data_root": tmp_path,
             "calendar_path": tmp_path / "cal.json"},
        )()
        import argparse

        args = argparse.Namespace(
            week="20260911", previous_week=False, force=False,
            run_kind="weekly_chain", backfill=0,
        )

        class FakeTracksvc:
            TRACKING_ALGO_VERSION = "1"

            @staticmethod
            def week_task_key(week):
                return f"track_{int(week)}"

            @staticmethod
            def compute_weekly_tracking(cfg_, week, **kw):
                return {
                    "completion": sc.TRACK_PENDING,
                    "week_id": week,
                    "reason": "track_week_not_covered_yet",
                }

        monkeypatch.setattr(climod, "tracksvc", FakeTracksvc, raising=False)
        rc, out = climod._run_one_track_week(cfg, 20260911, args)
        assert rc == climod._TRACK_EXIT_RETRYABLE
        assert out["completion"] == sc.TRACK_PENDING
        jobs = sc.load_pending_jobs(tmp_path)["jobs"]
        # 周身份 key + 真实原因（不是 skipped_locked）+ 恰好一次记账
        assert jobs["track_20260911"]["attempts"] == 1
        assert jobs["track_20260911"]["reason"] == "track_week_not_covered_yet"

    def test_terminal_state_clears_week_pending(self, tmp_path, monkeypatch):
        """★审查 🟡：终态清欠账要清**两个体系的 key**——CLI 锁待办按周记
        track_{week_id}，TaskState 按快照记；只清快照 key 会留下周键残留，
        待办到期后被幂等重跑一次（浪费一轮全市场读取）。"""
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        snap_key = tracksvc.tracking_task_key("snap_x_1")
        week_key = tracksvc.week_task_key(20260911)
        assert week_key == "track_20260911"
        assert snap_key != week_key  # 两个体系确实不同
        # 两个 key 都有欠账（抢锁失败记了周键；旧版本曾记过快照键）
        sc.record_pending_job(tmp_path, week_key, reason="skipped_locked")
        sc.record_pending_job(tmp_path, snap_key, reason="skipped_locked")
        # 终态清理（compute_weekly_tracking 完成路径调用同一助手）
        tracksvc._clear_terminal_pending(
            tmp_path, task_key=snap_key, week_id=20260911
        )
        jobs = sc.load_pending_jobs(tmp_path)["jobs"]
        assert "track_20260911" not in jobs
        assert snap_key not in jobs  # 快照体系的欠账也一并清


class TestHeavyJobCommandMapping:
    """契约 §7 补跑映射：待办 task_key → CLI 命令（api.py 纯函数）。"""

    def test_track_week_mapping(self, tmp_path):
        from wtpy.apps.astock.api import _heavy_job_command

        cmd = _heavy_job_command("track_20260911", tmp_path)
        assert cmd is not None
        assert "track-weekly" in cmd
        assert cmd[cmd.index("--week") + 1] == "20260911"
        assert cmd[cmd.index("--storage") + 1] == str(tmp_path)

    def test_backfill_mapping(self, tmp_path):
        from wtpy.apps.astock.api import _heavy_job_command

        cmd = _heavy_job_command("backfill_8", tmp_path)
        assert cmd[cmd.index("--backfill") + 1] == "8"

    def test_review_all_mapping(self, tmp_path):
        from wtpy.apps.astock.api import _heavy_job_command

        cmd = _heavy_job_command("review_all_20260911", tmp_path)
        assert "review-weekly" in cmd
        assert cmd[cmd.index("--rules") + 1] == "all"
        assert cmd[cmd.index("--asof") + 1] == "20260911"

    def test_unknown_task_key_returns_none(self, tmp_path):
        from wtpy.apps.astock.api import _heavy_job_command

        assert _heavy_job_command("nonsense_key", tmp_path) is None
        assert _heavy_job_command("", tmp_path) is None


class TestRunnerExitBookkeeping:
    """★审查 🟡 回归锁：runner 对子进程退出码的记账（单一记账方）。

    0/3 不记（3 已由子进程按真实 completion 记）；1/2 标欠账；
    其余异常码（信号杀死/OOM/崩溃——Windows 下是 0xC00000xx 大数）
    必须按有界退避记账：不记则待办立即又到期，每 120s 空转一次且
    占死 max_per_pass=1 的队列头，饿死其他待办。
    """

    def test_completion_codes_not_recorded(self, tmp_path):
        # 0（完成）与 3（可重试，子进程已记账）都不写
        assert hj.record_runner_exit(tmp_path, "track_20260911", 0) is None
        assert hj.record_runner_exit(tmp_path, "track_20260911", 3) is None
        assert sc.load_pending_jobs(tmp_path)["jobs"] == {}

    def test_unexpected_exit_codes_bounded_retry(self, tmp_path):
        # 信号杀死（-1）/ 未知码（4）/ Windows 崩溃码（0xC0000005）
        for i, rc in enumerate((-1, 4, 3221225477), start=1):
            info = hj.record_runner_exit(tmp_path, "track_20260911", rc)
            assert info["reason"] == f"unexpected_exit_{rc}"
            assert info["exhausted"] is False
            assert info["attempts"] == i
            assert info["next_retry_in_minutes"] is not None
        # 第 4 次 → 耗尽（保留欠账，不再自动重试）
        info = hj.record_runner_exit(tmp_path, "track_20260911", 3221225477)
        assert info["exhausted"] is True

    def test_non_retryable_codes_marked_exhausted(self, tmp_path):
        for rc in (1, 2):
            info = hj.record_runner_exit(tmp_path, "track_20260911", rc)
            assert info["reason"] == f"non_retryable_exit_{rc}"
            assert info["exhausted"] is True

    def test_spawn_failure_bounded_retry(self, tmp_path):
        info = hj.record_runner_spawn_failure(tmp_path, "track_20260911")
        assert info["reason"] == "spawn_failed"
        assert info["attempts"] == 1
        assert info["next_retry_in_minutes"] == 5
        assert info["exhausted"] is False

    def test_task_key_format_validation(self, tmp_path):
        """★审查 🟡：另一体系的 key（tracking_task_key 产的 track_snap_xxx）
        不得被解析成非法 --week——否则 CLI exit 2 → 误标 exhausted 欠账。"""
        from wtpy.apps.astock.api import _heavy_job_command

        # 非 8 位日期：TaskState 身份键（按快照记）误入待办文件时
        assert _heavy_job_command(
            "track_snap_20260911_20260915701172_30256_0001_59d9c6", tmp_path
        ) is None
        assert _heavy_job_command("track_202695", tmp_path) is None  # 6 位
        assert _heavy_job_command("track_", tmp_path) is None
        assert _heavy_job_command("backfill_abc", tmp_path) is None
        assert _heavy_job_command("backfill_0", tmp_path) is None   # 0 不是合法回填数
        assert _heavy_job_command("backfill_00", tmp_path) is None
        assert _heavy_job_command("review_all_202695", tmp_path) is None
        # 合法键不受影响
        assert _heavy_job_command("track_20260911", tmp_path) is not None
        assert _heavy_job_command("backfill_8", tmp_path) is not None

    def test_review_all_zero_maps_latest_asof(self, tmp_path):
        """review_all_0 = 手动不带 --asof（0 是缺省记号）：合法任务，
        映射为不带 --asof 的最新数据面重算，不当非法键标欠账。"""
        from wtpy.apps.astock.api import _heavy_job_command

        cmd = _heavy_job_command("review_all_0", tmp_path)
        assert cmd is not None
        assert "review-weekly" in cmd
        assert "--asof" not in cmd  # 缺省 asof：按最新数据面

    def test_single_bookkeeper_no_double_record(self, tmp_path):
        """★审查发现：抢锁失败只记一次账（单一记账方）。

        run_with_heavy_lock 内部已 record_lock_skip；服务端 runner 曾对
        rc=3 再补记一次 → 一次失败 attempts +2、退避 5→30 跳档、
        4 次重试预算被腰斩。回归锁住：记账后 attempts 必须恰好 +1。
        """
        cfg = type("C", (), {"storage_root": tmp_path})()
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            lk = hj.HeavyJobLock(tmp_path, task_key="holder")
            lk.acquire()
            acquired.set()
            release.wait(5)
            lk.release()

        th = threading.Thread(target=hold)
        th.start()
        try:
            assert acquired.wait(3)
            out = hj.run_with_heavy_lock(
                cfg, "track_20260911", fn=lambda: {"n": 1}
            )
            assert out["skipped_locked"] is True
            # 模拟服务端 runner 的 rc=3 分支：**不再补记**（修复后语义），
            # attempts 保持 1、退避保持 5 分钟
            jobs = sc.load_pending_jobs(tmp_path)["jobs"]
            assert jobs["track_20260911"]["attempts"] == 1
            assert jobs["track_20260911"]["next_retry_in_minutes"] == 5
            assert jobs["track_20260911"]["reason"] == "skipped_locked"
        finally:
            release.set()
            th.join(5)
