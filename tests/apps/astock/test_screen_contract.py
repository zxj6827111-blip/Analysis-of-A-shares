# -*- coding: utf-8 -*-
"""阶段 0 契约测试：screen_contract 的身份/周算法/版本/覆盖率/发布/任务状态。

纯函数级——不依赖真实数据根、不跑全市场扫描（阶段 2 才做真实数据基准）。
对应方案 v4.1 阶段 0 验收清单。
"""

from __future__ import annotations

import json

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.service import screen_contract as sc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_snapshot(tmp_path, **over):
    """最小可发布快照（rules 全 ok、no_data 空、universe_size 覆盖分母）。"""
    snap = {
        "snapshot_id": over.get("snapshot_id", "s1"),
        "week_id": over.get("week_id", 20260911),
        "asof": over.get("asof", 20260911),
        "run_kind": over.get("run_kind", "weekly_chain"),
        "status": "ok",
        "universe_size": over.get("universe_size", 100),
        "no_data_codes": over.get("no_data_codes", []),
        "rules": over.get(
            "rules",
            [{"rule_id": "r1", "status": "ok", "matched": [], "failed_codes": []}],
        ),
    }
    path = sc.snapshot_path(tmp_path, snap["snapshot_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return snap


def _cal(dates):
    return sorted(int(d) for d in dates)


# 正常五日周（2026-09-07 周一 ~ 09-11 周五，全交易日）
NORMAL_CAL = _cal([20260907, 20260908, 20260909, 20260910, 20260911,
                   20260914, 20260915, 20260916, 20260917, 20260918])


# ---------------------------------------------------------------------------
# 1. snapshot_id 唯一性 + O_EXCL 独占创建
# ---------------------------------------------------------------------------


class TestSnapshotIdentity:
    def test_new_snapshot_id_unique_same_process_same_ms(self):
        """同进程同毫秒批量申请也不碰撞（独立运行 ID，不靠时间戳保证）。"""
        ids = {sc.new_snapshot_id(20260911) for _ in range(200)}
        assert len(ids) == 200

    def test_new_snapshot_id_contains_asof(self):
        assert sc.new_snapshot_id(20260911).startswith("20260911_")

    def test_create_exclusive_never_overwrites(self, tmp_path):
        p = sc.screen_snapshots_dir(tmp_path) / "snap_x.json"
        sc.create_snapshot_file_exclusive(p, {"a": 1})
        with pytest.raises(FileExistsError):
            sc.create_snapshot_file_exclusive(p, {"a": 2})
        # 首写内容不被第二次尝试破坏
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}

    def test_create_exclusive_cleans_up_on_write_failure(self, tmp_path, monkeypatch):
        """写入中途异常时删除半截文件，不留 0 字节孤儿冒充快照。"""
        p = sc.screen_snapshots_dir(tmp_path) / "snap_y.json"
        orig = json.dump

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(sc.json, "dump", boom)
        with pytest.raises(RuntimeError):
            sc.create_snapshot_file_exclusive(p, {"a": 1})
        assert not p.exists()
        monkeypatch.setattr(sc.json, "dump", orig)

    def test_content_fingerprint_stable_and_sensitive(self):
        fp_args = dict(
            rule_fingerprints={"r1": "abc", "r2": "def"},
            universe_fingerprint="uni1",
            name_snapshot_id="ns1",
            data_version={"delta_watermark": 5, "delta_commit_seq": 3},
            asof=20260911,
        )
        base = sc.content_fingerprint(**fp_args)
        # 稳定：键序无关
        assert sc.content_fingerprint(
            rule_fingerprints={"r2": "def", "r1": "abc"}, **{k: v for k, v in fp_args.items() if k != "rule_fingerprints"}
        ) == base
        # 敏感：规则指纹 / universe / 数据版本 / asof 任一变化 → 变
        assert sc.content_fingerprint(**{**fp_args, "rule_fingerprints": {"r1": "abX", "r2": "def"}}) != base
        assert sc.content_fingerprint(**{**fp_args, "universe_fingerprint": "uni2"}) != base
        assert sc.content_fingerprint(
            **{**fp_args, "data_version": {"delta_watermark": 5, "delta_commit_seq": 4}}
        ) != base
        assert sc.content_fingerprint(**{**fp_args, "asof": 20260918}) != base


# ---------------------------------------------------------------------------
# 2. 自然周对齐
# ---------------------------------------------------------------------------


class TestNaturalWeek:
    def test_normal_week(self):
        w = sc.natural_week_window(NORMAL_CAL, 20260911)
        assert w["anomaly"] is None
        assert w["signal_week"] == [20260907, 20260911]
        assert w["track_week"] == [20260914, 20260918]
        assert w["week_id"] == 20260911
        assert w["no_trading_week"] is False
        assert w["short_week"] is False

    def test_short_track_week(self):
        # 跟踪周只有 3 个交易日（周四周五休市）
        cal = _cal([20260907, 20260908, 20260909, 20260910, 20260911,
                    20260914, 20260915, 20260916])
        w = sc.natural_week_window(cal, 20260911)
        assert w["anomaly"] is None
        assert w["track_week"] == [20260914, 20260916]
        assert w["short_week"] is True
        assert w["no_trading_week"] is False

    def test_whole_week_holiday_is_no_trading_week(self):
        # 跟踪周整周休市（如春节）：no_trading_week=True，不借用相邻周
        cal = _cal([20260907, 20260908, 20260909, 20260910, 20260911,
                    20260921, 20260922])
        w = sc.natural_week_window(cal, 20260911)
        assert w["no_trading_week"] is True
        assert w["track_week"] is None
        assert w["track_week_dates"] == []

    def test_cross_month_week(self):
        # 2026-09-28(周一)~09-30(周三) + 10-01/02 假设休市：信号周只有 3 日
        cal = _cal([20260928, 20260929, 20260930,
                    20261005, 20261006, 20261007, 20261008, 20261009])
        w = sc.natural_week_window(cal, 20260930)
        assert w["anomaly"] is None
        assert w["signal_week"] == [20260928, 20260930]
        assert w["track_week"] == [20261005, 20261009]
        assert w["short_week"] is True

    def test_signal_date_not_trading_day_fails_closed(self):
        w = sc.natural_week_window(NORMAL_CAL, 20260912)  # 周六
        assert w["anomaly"] == "signal_date_not_trading_day"

    def test_midweek_signal_date_not_reanchored(self):
        # 把周三当信号日传入（周五才是周内最后交易日）→ anomaly，不静默换锚
        w = sc.natural_week_window(NORMAL_CAL, 20260909)
        assert w["anomaly"] == "signal_date_not_week_last_trading_day"


# ---------------------------------------------------------------------------
# 3. data_version 与 tracking_revision_id
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, **kw):
        self.delta_store_id = kw.get("delta_store_id", "main")
        self.base_dataset_id = kw.get("base_dataset_id", "d1")
        self.base_manifest_sha256 = kw.get("base_manifest_sha256", "b1")
        self.delisted_base_dataset_id = kw.get("delisted_base_dataset_id", "")
        self.delisted_base_manifest_sha256 = kw.get("delisted_base_manifest_sha256", "")
        self.factor_base_dataset_id = kw.get("factor_base_dataset_id", "f1")
        self.factor_base_manifest_sha256 = kw.get("factor_base_manifest_sha256", "fb1")
        self.supplement_factor_base_dataset_id = kw.get("supplement_factor_base_dataset_id", "")
        self.supplement_factor_base_manifest_sha256 = kw.get("supplement_factor_base_manifest_sha256", "")
        self.delta_watermark = kw.get("delta_watermark", 100)
        self.factor_watermark = kw.get("factor_watermark", 100)
        self.delta_commit_seq = kw.get("delta_commit_seq", 7)
        self.factor_commit_seq = kw.get("factor_commit_seq", 7)


class TestDataVersion:
    def test_extract_full_overlay_identity(self):
        dv = sc.data_version_from_overlay_state(_FakeState())
        assert dv["base_dataset_id"] == "d1"
        assert dv["base_manifest_sha256"] == "b1"
        assert dv["delta_watermark"] == 100
        assert dv["delta_commit_seq"] == 7
        assert dv["factor_watermark"] == 100
        assert dv["factor_commit_seq"] == 7

    def test_signature_sensitive_to_commit_seq_only(self):
        """watermark 不变、commit_seq 变化 → 签名必须变（同 cutoff 数据修订）。"""
        s1 = sc.data_version_signature(sc.data_version_from_overlay_state(_FakeState()))
        s2 = sc.data_version_signature(
            sc.data_version_from_overlay_state(_FakeState(delta_commit_seq=8))
        )
        assert s1 != s2

    def test_revision_binds_all_inputs(self):
        bars = sc.data_version_from_overlay_state(_FakeState())
        bench = {"benchmark_dataset_id": "idx1", "benchmark_commit_seq": 1}
        base_rev = sc.tracking_revision_id("snap1", bars, bench, "algo1", "schema1")
        # 快照变
        assert sc.tracking_revision_id("snap2", bars, bench, "algo1", "schema1") != base_rev
        # 行情 commit_seq 变
        assert sc.tracking_revision_id(
            "snap1", sc.data_version_from_overlay_state(_FakeState(delta_commit_seq=8)),
            bench, "algo1", "schema1",
        ) != base_rev
        # 基准补数（benchmark 版本变）→ 新 revision
        assert sc.tracking_revision_id(
            "snap1", bars, {"benchmark_dataset_id": "idx1", "benchmark_commit_seq": 2},
            "algo1", "schema1",
        ) != base_rev
        # 算法升级 → 新 revision
        assert sc.tracking_revision_id("snap1", bars, bench, "algo2", "schema1") != base_rev
        # schema 变 → 新 revision
        assert sc.tracking_revision_id("snap1", bars, bench, "algo1", "schema2") != base_rev


# ---------------------------------------------------------------------------
# 4. 分口径覆盖率（分母 0 → null）
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_normal_coverage(self):
        cov = sc.coverage_by_basis(
            selected_count=10, valid_sig_count=10,
            valid_exec_count=8, excess_valid_count=9,
        )
        assert cov[sc.RET_BASIS_SIGNAL] == 1.0
        assert cov[sc.RET_BASIS_OPEN] == 0.8
        assert cov["excess"] == 0.9

    def test_zero_denominator_returns_null_not_zero(self):
        """空仓周：应评估样本为 0 → 三口径覆盖率全部 null（不是 0/1）。"""
        cov = sc.coverage_by_basis(
            selected_count=0, valid_sig_count=0,
            valid_exec_count=0, excess_valid_count=0,
        )
        assert cov[sc.RET_BASIS_SIGNAL] is None
        assert cov[sc.RET_BASIS_OPEN] is None
        assert cov["excess"] is None


# ---------------------------------------------------------------------------
# 5. 发布策略
# ---------------------------------------------------------------------------


class TestPublishPolicy:
    def test_ok_snapshot_publishable_with_verdict_fields(self, tmp_path):
        snap = _mk_snapshot(tmp_path)
        v = sc.PublishPolicy().evaluate(snap)
        assert v["publishable"] is True
        assert v["no_data_ratio"] == 0.0
        assert v["threshold_used"] == 0.05  # 策略默认值如实记录

    def test_partial_rule_blocks_publish(self, tmp_path):
        snap = _mk_snapshot(
            tmp_path,
            rules=[
                {"rule_id": "r1", "status": "ok", "matched": [], "failed_codes": []},
                {"rule_id": "r2", "status": "partial", "matched": [], "failed_codes": ["c1"]},
            ],
        )
        v = sc.PublishPolicy().evaluate(snap)
        assert v["publishable"] is False
        assert v["verdict"] == "rule_partial_or_error"
        assert v["partial_or_error_rules"] == ["r2"]

    def test_no_data_threshold_boundary(self, tmp_path):
        # universe=100，no_data=5 恰好在 5% 阈值（<=）可发布
        snap = _mk_snapshot(tmp_path, universe_size=100,
                            no_data_codes=[f"c{i}" for i in range(5)])
        assert sc.PublishPolicy().evaluate(snap)["publishable"] is True
        # 6 个 → 超阈值不发布
        snap6 = _mk_snapshot(tmp_path, snapshot_id="s2", universe_size=100,
                             no_data_codes=[f"c{i}" for i in range(6)])
        v = sc.PublishPolicy().evaluate(snap6)
        assert v["publishable"] is False
        assert v["verdict"] == "no_data_over_threshold"

    def test_custom_threshold_recorded(self, tmp_path):
        snap = _mk_snapshot(tmp_path, universe_size=100,
                            no_data_codes=[f"c{i}" for i in range(8)])
        v = sc.PublishPolicy(no_data_max_ratio=0.10).evaluate(snap)
        assert v["publishable"] is True
        assert v["threshold_used"] == 0.10  # 产物记录实际阈值


class TestPublishDecision:
    def _pub(self, tmp_path, week=20260911, snap="s1"):
        return sc.publish_decision(tmp_path, week, snap, "weekly_chain")

    def test_first_weekly_chain_publishes(self, tmp_path):
        _mk_snapshot(tmp_path)
        d = self._pub(tmp_path)
        assert d["publish"] is True and d["reason"] == "weekly_chain_first_publish"

    def test_retry_never_replaces_published_pointer(self, tmp_path):
        """同周五链重试两次 → 汇总只计一次（指针不被自动替换）。"""
        _mk_snapshot(tmp_path)
        sc.publish_snapshot(tmp_path, 20260911, "s1", "weekly_chain", source="auto")
        d2 = sc.publish_decision(tmp_path, 20260911, "s1_later_retry", "weekly_chain")
        assert d2["publish"] is False and d2["reason"] == "already_published"

    def test_backfill_only_fills_empty_week(self, tmp_path):
        _mk_snapshot(tmp_path)
        _mk_snapshot(tmp_path, snapshot_id="bf1", week_id=20260911)
        # 无指针 → backfill 可补位
        assert sc.publish_decision(tmp_path, 20260911, "bf1", "backfill")["publish"] is True
        # 已有指针（哪怕是 backfill 发的）→ 不再替换
        sc.publish_snapshot(tmp_path, 20260911, "bf1", "backfill", source="auto")
        d = sc.publish_decision(tmp_path, 20260911, "wc1", "weekly_chain")
        assert d["publish"] is False

    def test_recompute_never_auto_publishes(self, tmp_path):
        _mk_snapshot(tmp_path)
        d = sc.publish_decision(tmp_path, 20260911, "rc1", "recompute")
        assert d["publish"] is False and d["reason"] == "recompute_never_auto_publish"
        # 已有指针时同样拒绝
        sc.publish_snapshot(tmp_path, 20260911, "s1", "weekly_chain", source="auto")
        assert sc.publish_decision(tmp_path, 20260911, "rc1", "recompute")["publish"] is False

    def test_publish_snapshot_rejects_wrong_week(self, tmp_path):
        _mk_snapshot(tmp_path, week_id=20260911)
        with pytest.raises(ValueError, match="周归属不符"):
            sc.publish_snapshot(tmp_path, 20260918, "s1", "weekly_chain")

    def test_publish_snapshot_rejects_partial_quality(self, tmp_path):
        _mk_snapshot(
            tmp_path, week_id=20260918,
            rules=[{"rule_id": "r1", "status": "error", "matched": [], "failed_codes": []}],
        )
        with pytest.raises(ValueError, match="发布门槛"):
            sc.publish_snapshot(tmp_path, 20260918, "s1", "weekly_chain")

    def test_manual_publish_audits_previous_pointer(self, tmp_path):
        _mk_snapshot(tmp_path)
        sc.publish_snapshot(tmp_path, 20260911, "s1", "weekly_chain", source="auto")
        _mk_snapshot(tmp_path, snapshot_id="s1b", week_id=20260911)
        entry = sc.publish_snapshot(tmp_path, 20260911, "s1b", "weekly_chain", source="manual")
        assert entry["published_snapshot_id"] == "s1b"
        assert entry["audit"]["previous_snapshot_id"] == "s1"
        idx = sc.load_week_index(tmp_path)
        assert idx["weeks"]["20260911"]["audit"]["previous_snapshot_id"] == "s1"


# ---------------------------------------------------------------------------
# 6. 任务状态与 heavy-job 待办（按任务身份管理）
# ---------------------------------------------------------------------------


class TestTaskState:
    def test_state_files_isolated_per_task(self, tmp_path):
        """不同周的任务状态互不覆盖（一 task_key 一文件）。"""
        sc.save_task_state(tmp_path, sc.TaskState(task_key="track_20260911", completion=sc.TRACK_COMPLETE))
        sc.save_task_state(tmp_path, sc.TaskState(task_key="track_20260918", completion=sc.TRACK_PENDING))
        a = sc.load_task_state(tmp_path, "track_20260911")
        b = sc.load_task_state(tmp_path, "track_20260918")
        assert a["completion"] == sc.TRACK_COMPLETE
        assert b["completion"] == sc.TRACK_PENDING

    def test_state_update_does_not_leak_to_other_week(self, tmp_path):
        sc.save_task_state(tmp_path, sc.TaskState(task_key="track_20260911", attempts=1))
        sc.save_task_state(tmp_path, sc.TaskState(task_key="track_20260911", attempts=2))
        # 更新 0911 不影响 0918
        sc.save_task_state(tmp_path, sc.TaskState(task_key="track_20260918", attempts=1))
        assert sc.load_task_state(tmp_path, "track_20260911")["attempts"] == 2
        assert sc.load_task_state(tmp_path, "track_20260918")["attempts"] == 1


class TestHeavyJobPending:
    def test_pending_recorded_and_backoff_bounded(self, tmp_path):
        j1 = sc.record_pending_job(tmp_path, "track_20260911", reason="skipped_locked")
        assert j1["attempts"] == 1
        assert j1["next_retry_in_minutes"] == 5
        j2 = sc.record_pending_job(tmp_path, "track_20260911", reason="skipped_locked")
        assert j2["attempts"] == 2 and j2["next_retry_in_minutes"] == 15
        j3 = sc.record_pending_job(tmp_path, "track_20260911", reason="skipped_locked")
        assert j3["attempts"] == 3 and j3["next_retry_in_minutes"] == 30
        j4 = sc.record_pending_job(tmp_path, "track_20260911", reason="skipped_locked")
        # 重试耗尽：保留欠账 + exhausted 标记（不静默丢弃）
        assert j4["attempts"] == 4 and j4["exhausted"] is True
        assert j4["next_retry_in_minutes"] is None

    def test_pending_jobs_isolated_per_task_key(self, tmp_path):
        """待办按任务身份管理：A 周耗尽不影响 B 周的待办。"""
        for _ in range(4):
            sc.record_pending_job(tmp_path, "track_20260911", reason="skipped_locked")
        j = sc.record_pending_job(tmp_path, "track_20260918", reason="skipped_locked")
        assert j["attempts"] == 1
        data = sc.load_pending_jobs(tmp_path)
        assert data["jobs"]["track_20260911"]["exhausted"] is True
        assert data["jobs"]["track_20260918"]["exhausted"] is False

    def test_clear_pending_job(self, tmp_path):
        sc.record_pending_job(tmp_path, "t1", reason="skipped_locked")
        sc.record_pending_job(tmp_path, "t2", reason="skipped_locked")
        sc.clear_pending_job(tmp_path, "t1")
        data = sc.load_pending_jobs(tmp_path)
        assert "t1" not in data["jobs"] and "t2" in data["jobs"]
        # 全清后文件删除不留空壳
        sc.clear_pending_job(tmp_path, "t2")
        assert not sc.heavy_job_pending_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# 7. 补偿判定（不看文件存在，看目标完成状态）
# ---------------------------------------------------------------------------


class TestCompensation:
    def test_no_product_after_window(self):
        assert sc.compensation_required(
            has_product=False, product_terminal=False,
            revision_matches=False, window_ended=True,
        ) is True

    def test_window_not_ended_never_compensates(self):
        """窗口未结束 → pending 不是 missing，不触发补偿。"""
        assert sc.compensation_required(
            has_product=False, product_terminal=False,
            revision_matches=False, window_ended=False,
        ) is False

    def test_stale_complete_still_compensates(self):
        """v4.1 补1：旧版本产物即使 complete 也被 revision 不一致捞回。"""
        assert sc.compensation_required(
            has_product=True, product_terminal=True,
            revision_matches=False, window_ended=True,
        ) is True

    def test_fresh_complete_not_compensated(self):
        assert sc.compensation_required(
            has_product=True, product_terminal=True,
            revision_matches=True, window_ended=True,
        ) is False

    def test_non_terminal_compensates(self):
        assert sc.compensation_required(
            has_product=True, product_terminal=False,
            revision_matches=True, window_ended=True,
        ) is True

    def test_should_complete_further_merges_state_and_revision(self):
        # 无状态 → 需要算
        assert sc.should_complete_further(None, window_ended=True, target_revision_id="r1") is True
        # complete 且 revision 一致 → 不需要
        assert sc.should_complete_further(
            {"completion": sc.TRACK_COMPLETE, "tracking_revision_id": "r1"},
            window_ended=True, target_revision_id="r1",
        ) is False
        # complete 但算法升级（revision 不一致）→ 重算（阶段验收子用例 1）
        assert sc.should_complete_further(
            {"completion": sc.TRACK_COMPLETE, "tracking_revision_id": "r0"},
            window_ended=True, target_revision_id="r1",
        ) is True
        # no_trading_week 终态 → 不算
        assert sc.should_complete_further(
            {"completion": sc.TRACK_NO_TRADING_WEEK, "tracking_revision_id": None},
            window_ended=True, target_revision_id="r1",
        ) is False
        # 窗口未结束 → 不算
        assert sc.should_complete_further(
            {"completion": sc.TRACK_PENDING}, window_ended=False, target_revision_id="r1",
        ) is False


# ---------------------------------------------------------------------------
# 8. 枚举完整性（防自造同义词的静态守卫）
# ---------------------------------------------------------------------------


class TestEnums:
    def test_all_statuses_registered(self):
        assert sc.RUN_KINDS == ("weekly_chain", "backfill", "recompute")
        assert sc.RULE_STATUSES == ("ok", "partial", "error")
        assert sc.TICKET_STATUSES == (
            "hit", "miss", "error", "no_data", "not_in_universe"
        )
        assert sc.TRACK_TERMINAL_STATUSES == ("complete", "no_trading_week")
        assert sc.FILL_STATUSES == ("ok", "limit_up_unbuyable", "no_bar", "unknown")
        assert sc.RET_BASES == ("signal_close", "week_first_open")

    def test_exit_codes_distinct(self):
        assert sc.EXIT_OK == 0 and sc.EXIT_RETRYABLE == 3 and sc.EXIT_RETRYABLE != sc.EXIT_OK
