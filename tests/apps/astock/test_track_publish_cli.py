# -*- coding: utf-8 -*-
"""`track-publish`：显式替换某周发布指针（契约 §0 / contract.md:82）。

背景（2026-09-16 用户实操反馈）：契约早就写明"已发布 backfill 换 weekly_chain /
手工 recompute 转正 → 显式 track-publish（source=manual）"，但这个命令一直没实现。
结果是「某周已经有 A 规则的名单，想再补上 B 规则」在页面上只有 400、在 CLI 上
只会安静跳过——用户无路可走。本测试锁死这条显式路径的行为与审计。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss

WEEK = 20260731
OTHER_WEEK = 20260911
RULE_A = "txt_规则A"
RULE_B = "txt_规则B"


@pytest.fixture()
def env(tmp_path: Path):
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    (ind / "规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
    (ind / "规则B.txt").write_text("MA10:=MA(C,10);\nXG:CROSS(C,MA10);", encoding="utf-8")
    return get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )


def _payload(asof: int, rule_ids, *, scope: str = sc.RULES_SCOPE_ALL) -> dict:
    codes = ["SZSE.000001.SZ", "SZSE.000002.SZ"]
    return {
        "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": sc.new_snapshot_id(asof),
        "run_kind": "backfill",
        "rules_scope": scope,
        "scoped_rule_ids": list(rule_ids) if scope == sc.RULES_SCOPE_SUBSET else [],
        "week_id": asof, "asof": asof, "generated_at": f"{asof} 18:40:00", "status": "ok",
        "universe_size": len(codes), "universe_codes": codes,
        "universe_fingerprint": f"ufp{asof}", "name_snapshot_id": "ns1",
        "rule_fingerprints": {r: f"fp_{r}" for r in rule_ids},
        "data_version": {}, "content_fingerprint": f"cfp{asof}",
        "scanned": len(codes), "missing_count": 0, "no_data_codes": [],
        "rules": [
            {"rule_id": r, "sheet": r, "status": "ok", "count": 2,
             "matched": [{"code": codes[0], "close": 10.0},
                         {"code": codes[1], "close": 11.0}],
             "failed_codes": []}
            for r in rule_ids
        ],
        "duration_sec": 0.1,
    }


def _write_snapshot(cfg, asof: int, rule_ids, *, scope=sc.RULES_SCOPE_ALL) -> dict:
    """只落快照文件（不走发布）：模拟"跑完但没转正"的产物。"""
    payload = _payload(asof, rule_ids, scope=scope)
    sc.create_snapshot_file_exclusive(
        sc.snapshot_path(Path(cfg.storage_root), payload["snapshot_id"]), payload
    )
    return payload


def _publish(cfg, asof: int, rule_ids, *, scope=sc.RULES_SCOPE_ALL) -> dict:
    return ss.write_and_publish_snapshot(cfg, _payload(asof, rule_ids, scope=scope))


def _run(env, *argv: str) -> int:
    from wtpy.apps.astock.cli import main

    # 全局参数必须放在子命令之前（CLI 约定）
    return main(["--storage", str(env.storage_root), *argv])


def test_track_publish_replaces_pointer_with_reason(env, capsys):
    """子集周被 A 占住 → 显式把 B 的快照转正，并留下审计。"""
    first = _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)["snapshot_id"]
    target = _write_snapshot(env, WEEK, [RULE_B], scope=sc.RULES_SCOPE_SUBSET)["snapshot_id"]

    rc = _run(env, "track-publish", "--week", str(WEEK), "--snapshot", target,
              "--reason", "追加规则B")
    assert rc == 0, capsys.readouterr().out

    idx = sc.load_week_index(Path(env.storage_root))
    entry = idx["weeks"][str(WEEK)]
    assert entry["published_snapshot_id"] == target
    assert entry["scoped_rule_ids"] == [RULE_B]
    assert entry["run_kind"] == "backfill", "索引里的来源应取快照自身 run_kind"
    assert entry["published_by"] == "manual:追加规则B"
    assert entry["audit"]["previous_snapshot_id"] == first, "原指针必须记进审计"


def test_track_publish_requires_reason_and_snapshot(env, capsys):
    _publish(env, WEEK, [RULE_A])
    # 缺原因：给"为什么要写原因"的自解释文案，而不是 argparse 的 required 提示
    assert _run(env, "track-publish", "--week", str(WEEK), "--snapshot", "x") == 2
    assert "--reason 必填" in capsys.readouterr().out
    # 缺 --snapshot：argparse 层直接拒绝（用法错误）
    with pytest.raises(SystemExit):
        _run(env, "track-publish", "--week", str(WEEK), "--reason", "r")
    # 周格式非法
    assert _run(env, "track-publish", "--week", "2026-7-31", "--snapshot", "x",
                "--reason", "r") == 2
    assert "8 位日期" in capsys.readouterr().out


def test_track_publish_rejects_unknown_snapshot(env, capsys):
    _publish(env, WEEK, [RULE_A])
    rc = _run(env, "track-publish", "--week", str(WEEK), "--snapshot", "不存在",
              "--reason", "r")
    assert rc == 2
    assert "快照不存在" in capsys.readouterr().out


def test_track_publish_rejects_week_mismatch(env, capsys):
    """快照周归属不符必须拒绝：不能把别的周的快照塞进这一周。"""
    _publish(env, WEEK, [RULE_A])
    other = _write_snapshot(env, OTHER_WEEK, [RULE_B])["snapshot_id"]
    rc = _run(env, "track-publish", "--week", str(WEEK), "--snapshot", other,
              "--reason", "r")
    assert rc == 2
    assert "周归属不符" in capsys.readouterr().out
    idx = sc.load_week_index(Path(env.storage_root))
    assert idx["weeks"][str(WEEK)]["published_snapshot_id"] != other


def test_track_publish_rejects_unpublishable_snapshot(env, capsys):
    """门槛不过的快照不能靠手工路径绕过（契约：门槛同样校验）。"""
    _publish(env, WEEK, [RULE_A])
    bad = _payload(WEEK, [RULE_B])
    bad["rules"][0]["status"] = sc.RULE_STATUS_ERROR  # partial/error → 过不了发布门槛
    bad["missing_count"] = 0
    sc.create_snapshot_file_exclusive(
        sc.snapshot_path(Path(env.storage_root), bad["snapshot_id"]), bad
    )
    rc = _run(env, "track-publish", "--week", str(WEEK), "--snapshot",
              bad["snapshot_id"], "--reason", "r")
    assert rc == 2
    assert "未过发布门槛" in capsys.readouterr().out


def test_track_publish_allows_promoting_empty_week(env, capsys):
    """没有指针的周也能用它转正（先跑出快照再转正，是补算之外的合法入口）。"""
    target = _write_snapshot(env, WEEK, [RULE_A])["snapshot_id"]
    rc = _run(env, "track-publish", "--week", str(WEEK), "--snapshot", target,
              "--reason", "手工转正")
    assert rc == 0
    idx = sc.load_week_index(Path(env.storage_root))
    assert idx["weeks"][str(WEEK)]["published_snapshot_id"] == target
    assert idx["weeks"][str(WEEK)]["audit"]["previous_snapshot_id"] is None


def test_published_pointer_then_l2_sees_new_rule(env):
    """转正后读取层立刻看到新名单（L2 接口用发布指针 + 指纹匹配）。"""
    _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
    target = _write_snapshot(env, WEEK, [RULE_B], scope=sc.RULES_SCOPE_SUBSET)["snapshot_id"]
    assert _run(env, "track-publish", "--week", str(WEEK), "--snapshot", target,
                "--reason", "r") == 0
    snap = ss.load_published_snapshot_for_week(env, WEEK)
    assert snap is not None
    assert sc.scoped_rule_ids(snap) == [RULE_B]
    assert json.dumps(snap, ensure_ascii=False).find(RULE_B) >= 0


class TestAppendMissingRulesStep:
    """`_append_missing_rules`：CLI 侧「追加规则」这一步（周 × 规则 口径）。

    只测这一步（复核扫描用桩替代）：真实全市场扫描不在单测里跑。
    """

    def _args(self, env):
        import argparse

        return argparse.Namespace(
            storage=str(env.storage_root), tdx_root=None, indicator_dir=None
        )

    def test_scans_missing_rules_and_merges(self, env, monkeypatch, capsys):
        from wtpy.apps.astock import cli as cli_mod

        _publish(env, WEEK, [RULE_B], scope=sc.RULES_SCOPE_SUBSET)
        calls = []

        def fake_review(args):
            calls.append(args.rules)
            payload = _payload(WEEK, str(args.rules).split(","),
                               scope=sc.RULES_SCOPE_SUBSET)
            sc.create_snapshot_file_exclusive(
                sc.snapshot_path(Path(env.storage_root), payload["snapshot_id"]), payload
            )
            return 0

        monkeypatch.setattr(cli_mod, "cmd_review_weekly", fake_review)
        res = cli_mod._append_missing_rules(env, WEEK, [RULE_A], self._args(env))
        assert calls == [RULE_A], "只扫缺的那一条，不重跑已有规则"
        assert res["ok"] is True and res["added"] == [RULE_A]
        snap = ss.load_published_snapshot_for_week(env, WEEK)
        assert {r["rule_id"] for r in snap["rules"]} == {RULE_A, RULE_B}

    def test_skips_scan_when_rules_present(self, env, monkeypatch):
        from wtpy.apps.astock import cli as cli_mod

        _publish(env, WEEK, [RULE_A, RULE_B], scope=sc.RULES_SCOPE_SUBSET)

        def boom(_args):  # 不该被调用
            raise AssertionError("规则已在名单里时不应触发扫描")

        monkeypatch.setattr(cli_mod, "cmd_review_weekly", boom)
        res = cli_mod._append_missing_rules(env, WEEK, [RULE_A], self._args(env))
        assert res["reason"] == "rules_already_present"
        assert res["present"] == [RULE_A]

    def test_reports_review_failure(self, env, monkeypatch):
        from wtpy.apps.astock import cli as cli_mod

        _publish(env, WEEK, [RULE_B], scope=sc.RULES_SCOPE_SUBSET)
        monkeypatch.setattr(cli_mod, "cmd_review_weekly", lambda _a: 3)
        res = cli_mod._append_missing_rules(env, WEEK, [RULE_A], self._args(env))
        assert res["ok"] is False and res["reason"] == "review_failed"
        assert res["review_exit_code"] == 3

    def test_reports_missing_extra_snapshot(self, env, monkeypatch):
        from wtpy.apps.astock import cli as cli_mod

        _publish(env, WEEK, [RULE_B], scope=sc.RULES_SCOPE_SUBSET)
        monkeypatch.setattr(cli_mod, "cmd_review_weekly", lambda _a: 0)  # 复核"成功"但没落盘
        res = cli_mod._append_missing_rules(env, WEEK, [RULE_A], self._args(env))
        assert res["ok"] is False and res["reason"] == "extra_snapshot_not_found"
