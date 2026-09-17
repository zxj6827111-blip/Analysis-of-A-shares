# -*- coding: utf-8 -*-
"""阶段 1 快照层测试：screen_snapshots（组装/发布/cache-first 组合语义）。

合成 summary——不依赖真实数据根、不跑全市场扫描。契约测试（阶段 0 的
test_screen_contract.py）已覆盖身份/周算法/发布策略原语；本文件覆盖
服务层把这些原语接到 run_weekly_review 结果上的行为。
"""

from __future__ import annotations

import json

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss


def _summary(**over):
    """最小 run_weekly_review 摘要（两条规则 / 三只票）。"""
    s = {
        "asof": 20260911,
        "generated_at": "2026-09-11 18:40:00",
        "universe_codes": ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ"],
        "status": "ok",
        "universe_size": 3,
        "universe_fingerprint": "ufp1",
        "name_snapshot_id": "ns1",
        "rule_fingerprints": {"r1": "fp_r1", "r2": "fp_r2"},
        "scanned": 3,
        "missing_count": 0,
        "no_data_codes": [],
        "errors": [],
        "rules": [
            {
                "rule_id": "r1", "sheet": "规则一", "count": 2,
                "matched": [
                    {"code": "SZSE.000001.SZ", "close": 10.0},
                    {"code": "SZSE.000002.SZ", "close": 20.0},
                ],
            },
            {
                "rule_id": "r2", "sheet": "规则二", "count": 1,
                "matched": [{"code": "SZSE.000002.SZ", "close": 20.0}],
            },
        ],
        "duration_sec": 0.1,
    }
    s.update(over)
    return s


def _codes():
    return ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ"]


class TestBuildSnapshot:
    def test_snapshot_structure_and_fingerprint(self, tmp_path):
        """组装快照：完整结构 + content_fingerprint 存在（幂等提示用）。"""
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        payload = ss.build_snapshot_payload(
            cfg, _summary(), rule_ids=["r1", "r2"],
        )
        # universe_codes 回源（summary 不带时由 _resolve_codes 补）会走
        # cfg.storage_root 下的 universe.json —— 合成环境用注入避免依赖：
        assert payload["snapshot_id"].startswith("20260911_")
        assert payload["week_id"] == 20260911
        assert payload["run_kind"] == "weekly_chain"
        assert payload["schema_version"] == ss.SNAPSHOT_SCHEMA_VERSION
        assert {r["rule_id"] for r in payload["rules"]} == {"r1", "r2"}
        for r in payload["rules"]:
            assert r["status"] == sc.RULE_STATUS_OK
            assert r["failed_codes"] == []
        assert payload["content_fingerprint"]

    def test_snapshot_is_immutable_and_published_once(self, tmp_path):
        """同 asof 重跑 → 新 snapshot_id 新文件；已有指针不替换（契约 §0）。"""
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        p1 = ss.build_snapshot_payload(cfg, _summary(), rule_ids=["r1", "r2"])
        # 直接注入 universe_codes，避免回源依赖
        r1 = ss.write_and_publish_snapshot(cfg, p1)
        assert r1["published"] is True

        p2 = ss.build_snapshot_payload(cfg, _summary(), rule_ids=["r1", "r2"])
        assert p2["snapshot_id"] != p1["snapshot_id"]
        r2 = ss.write_and_publish_snapshot(cfg, p2)
        # 重试产生的新快照保留，但指针不动 → 统计只计一次
        assert r2["published"] is False and r2["reason"] == "already_published"

        idx = sc.load_week_index(tmp_path)
        assert idx["weeks"]["20260911"]["published_snapshot_id"] == p1["snapshot_id"]
        # 两个快照文件都在（并列保留，供版本对比）
        assert sc.snapshot_path(tmp_path, p1["snapshot_id"]).exists()
        assert sc.snapshot_path(tmp_path, p2["snapshot_id"]).exists()

    def test_partial_error_isolated_per_rule(self, tmp_path):
        """r2 在某票报错只进 r2 的 failed_codes；r1 命中照常可用（契约 §规则组②）。"""
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        summ = _summary(errors=[{"code": "SZSE.000003.SZ", "rule": "r2", "error": "boom"}])
        payload = ss.build_snapshot_payload(cfg, summ, rule_ids=["r1", "r2"])
        by_id = {r["rule_id"]: r for r in payload["rules"]}
        assert by_id["r2"]["failed_codes"] == ["SZSE.000003.SZ"]
        assert by_id["r2"]["status"] == sc.RULE_STATUS_PARTIAL
        assert by_id["r1"]["failed_codes"] == []
        assert by_id["r1"]["status"] == sc.RULE_STATUS_OK

    def test_partial_snapshot_not_published(self, tmp_path):
        """partial 快照保留诊断但过不了发布门槛（契约 §6）。

        门槛不过要**优雅跳过**（published=False + verdict 如实上报），
        绝不抛异常——真实数据上（大面积停牌）把质量不达标变成链路事故
        是错的（审查后修正）。
        """
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        summ = _summary(errors=[{"code": "SZSE.000003.SZ", "rule": "r2", "error": "boom"}])
        payload = ss.build_snapshot_payload(cfg, summ, rule_ids=["r1", "r2"])
        out = ss.write_and_publish_snapshot(cfg, payload)
        assert out["published"] is False
        assert out["reason"] == "quality_gate:rule_partial_or_error"
        assert out["verdict"]["publishable"] is False
        # 快照文件保留（诊断），指针未动
        assert sc.snapshot_path(tmp_path, payload["snapshot_id"]).exists()
        assert sc.load_week_index(tmp_path)["weeks"] == {}

    def test_no_data_over_threshold_graceful_skip(self, tmp_path):
        """no_data 超阈值同样优雅跳过（不崩周五链）。"""
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        summ = _summary(
            universe_size=100,
            no_data_codes=[f"SZSE.0000{i:02d}.SZ" for i in range(20)],
        )
        payload = ss.build_snapshot_payload(cfg, summ, rule_ids=["r1", "r2"])
        out = ss.write_and_publish_snapshot(cfg, payload)
        assert out["published"] is False
        assert out["reason"] == "quality_gate:no_data_over_threshold"
        assert out["verdict"]["no_data_count"] == 20
        assert out["verdict"]["threshold_used"] == 0.05  # 实际阈值如实记录
        assert sc.load_week_index(tmp_path)["weeks"] == {}


class TestSnapshotCovers:
    def _snap(self, tmp_path, **over):
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        payload = ss.build_snapshot_payload(cfg, _summary(**over), rule_ids=["r1", "r2"])
        sc.create_snapshot_file_exclusive(
            sc.snapshot_path(tmp_path, payload["snapshot_id"]), payload
        )
        return payload

    def test_subset_covered(self, tmp_path):
        snap = self._snap(tmp_path)
        cov = ss.snapshot_covers(snap, ["r1"])
        assert cov["covered"] == ["r1"] and cov["uncovered"] == [] and cov["stale"] == []

    def test_missing_and_stale_rules_uncovered(self, tmp_path):
        snap = self._snap(tmp_path)
        cov = ss.snapshot_covers(
            snap, ["r1", "rX"], current_rule_fps={"r1": "fp_r1", "rX": "fp_x"}
        )
        assert cov["covered"] == ["r1"]
        assert cov["uncovered"] == ["rX"]
        # 指纹变化（规则公式已改）→ stale，不得把旧命中当新规则结果
        cov2 = ss.snapshot_covers(
            snap, ["r1"], current_rule_fps={"r1": "fp_CHANGED"}
        )
        assert cov2["stale"] == ["r1"] and cov2["covered"] == []

    def test_error_rule_not_covered(self, tmp_path):
        snap = self._snap(tmp_path, errors=[
            {"code": c, "rule": "r2", "error": "boom"} for c in _codes()
        ])
        snap["rules"] = [
            {**r, "status": (sc.RULE_STATUS_ERROR if r["rule_id"] == "r2" else r["status"])}
            for r in snap["rules"]
        ]
        cov = ss.snapshot_covers(snap, ["r1", "r2"])
        assert cov["covered"] == ["r1"] and "r2" in cov["uncovered"]


class TestCombineSnapshotHits:
    def _snap(self, tmp_path, **over):
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        payload = ss.build_snapshot_payload(cfg, _summary(**over), rule_ids=["r1", "r2"])
        return payload

    def test_any_combination(self, tmp_path):
        snap = self._snap(tmp_path)
        out = ss.combine_snapshot_hits(snap, rule_ids=["r1", "r2"], match_mode="any")
        codes = [h["code"] for h in out["hits"]]
        assert codes == ["SZSE.000001.SZ", "SZSE.000002.SZ"]  # r1∪r2
        assert out["complete"] is True
        # 命中规则表按 sheet 名标注
        h2 = next(h for h in out["hits"] if h["code"] == "SZSE.000002.SZ")
        assert sorted(h2["hit_rules"]) == ["规则一", "规则二"]

    def test_all_combination(self, tmp_path):
        snap = self._snap(tmp_path)
        out = ss.combine_snapshot_hits(snap, rule_ids=["r1", "r2"], match_mode="all")
        codes = [h["code"] for h in out["hits"]]
        assert codes == ["SZSE.000002.SZ"]  # r1∩r2

    def test_all_with_error_rule_yields_indeterminate_not_partial_intersection(self, tmp_path):
        """all 含失败规则 → 不得输出"剩余规则交集"冒充完整结果（契约 §规则组②）。"""
        snap = self._snap(tmp_path, errors=[
            {"code": "SZSE.000001.SZ", "rule": "r2", "error": "boom"},
            {"code": "SZSE.000003.SZ", "rule": "r2", "error": "boom"},
        ])
        snap = {
            **snap,
            "rules": [
                {**r, "failed_codes": (
                    ["SZSE.000001.SZ", "SZSE.000003.SZ"] if r["rule_id"] == "r2"
                    else r["failed_codes"]
                )}
                for r in snap["rules"]
            ],
        }
        out = ss.combine_snapshot_hits(snap, rule_ids=["r1", "r2"], match_mode="all")
        # 000001 只在 r1 命中、r2 失败 → indeterminate，绝不进 hits
        assert [h["code"] for h in out["hits"]] == ["SZSE.000002.SZ"]
        ind = {e["code"]: e for e in out["indeterminate"]}
        assert "SZSE.000001.SZ" in ind
        assert ind["SZSE.000001.SZ"]["reasons"]["r2"] == sc.TICKET_ERROR
        assert out["complete"] is False

    def test_picked_out_of_universe_not_silent_miss(self, tmp_path):
        """picked 越界票 → not_in_universe，不静默当 miss（验收子用例 4）。"""
        snap = self._snap(tmp_path)
        out = ss.combine_snapshot_hits(
            snap, rule_ids=["r1"], match_mode="any",
            codes=["SZSE.000001.SZ", "SZSE.999999.SZ"],
        )
        assert [h["code"] for h in out["hits"]] == ["SZSE.000001.SZ"]
        assert out["not_in_universe"] == ["SZSE.999999.SZ"]
        assert out["complete"] is False

    def test_picked_scope_filters_hits(self, tmp_path):
        snap = self._snap(tmp_path)
        out = ss.combine_snapshot_hits(
            snap, rule_ids=["r1", "r2"], match_mode="any",
            codes=["SZSE.000002.SZ"],
        )
        assert [h["code"] for h in out["hits"]] == ["SZSE.000002.SZ"]
        assert out["complete"] is True

    def test_no_data_is_indeterminate_not_miss(self, tmp_path):
        """asof 无 K 线的票 → no_data，any 模式不宣称 miss（进 indeterminate）。"""
        snap = self._snap(tmp_path, no_data_codes=["SZSE.000003.SZ"])
        out = ss.combine_snapshot_hits(snap, rule_ids=["r1"], match_mode="any")
        assert [h["code"] for h in out["hits"]] == ["SZSE.000001.SZ", "SZSE.000002.SZ"]
        ind = {e["code"] for e in out["indeterminate"]}
        assert "SZSE.000003.SZ" in ind
        assert out["complete"] is False


class TestLoadPublished:
    def test_latest_published_roundtrip(self, tmp_path):
        cfg = type("C", (), {"storage_root": tmp_path, "indicator_dir": None,
                             "mapping_path": None})()
        p = ss.build_snapshot_payload(cfg, _summary(), rule_ids=["r1", "r2"])
        ss.write_and_publish_snapshot(cfg, p)
        latest = ss.latest_published_snapshot(cfg)
        assert latest is not None
        assert latest["snapshot_id"] == p["snapshot_id"]
        assert latest["asof"] == 20260911
        # 无发布周 → None
        assert ss.load_published_snapshot_for_week(cfg, 20260918) is None
