# -*- coding: utf-8 -*-
"""「指定规则补算」（子集快照 rules_scope=subset）测试（2026-09-16）。

背景：全市场扫描成本与规则数近似线性（实测单规则约为全量的 1/6~1/10），
"验证某条规则在过去某周选出了什么"不需要跑全部规则。子集快照是**部分
名单**，三处硬约束必须锁死，否则会破坏既有语义：

1. **不写 review_{asof}.json**：那是周五链/导出共享的全规则结果，被子集
   覆盖会让该周导出直接错数据（persist=False）；
2. **发布护栏**：只补"没有发布指针 且 早于数据面最新信号周"的历史周——
   最新的周归周五链，子集（部分名单）占住发布指针会让链的全量快照被
   "已有指针不替换"永久挡住；
3. **读取方如实标注**：L0/L1/L2 与导出必须带出 rules_scope/scoped_rule_ids，
   否则"这周只有这条规则跑过"会被读成"其他规则当周空仓"。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss

WEEK = 20260731
LATER_WEEK = 20260911
RULE_A = "txt_规则A"
RULE_B = "txt_规则B"


# ---------------------------------------------------------------------------
# 夹具与合成产物
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    """tmp 隔离的配置 + 两条可执行规则（注册表真实解析出 txt_规则A/B）。"""
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    (ind / "规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
    (ind / "规则B.txt").write_text("MA10:=MA(C,10);\nXG:CROSS(C,MA10);", encoding="utf-8")
    cfg = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    return cfg


def _snap_payload(asof: int, rule_ids, *, scope: str = sc.RULES_SCOPE_ALL) -> dict:
    """合成快照（结构对齐 build_snapshot_payload；够过发布门槛）。"""
    codes = ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ"]
    return {
        "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": sc.new_snapshot_id(asof),
        "run_kind": "backfill",
        "rules_scope": scope,
        "scoped_rule_ids": list(rule_ids) if scope == sc.RULES_SCOPE_SUBSET else [],
        "week_id": asof, "asof": asof,
        "generated_at": f"{asof} 18:40:00", "status": "ok",
        "universe_size": len(codes), "universe_codes": codes,
        "universe_fingerprint": f"ufp{asof}", "name_snapshot_id": "ns1",
        "rule_fingerprints": {r: f"fp_{r}" for r in rule_ids},
        "data_version": {}, "content_fingerprint": f"cfp{asof}",
        "scanned": len(codes), "missing_count": 0, "no_data_codes": [],
        "rules": [
            {"rule_id": r, "sheet": r, "status": "ok", "count": 2,
             "matched": [{"code": "SZSE.000001.SZ", "close": 10.0},
                         {"code": "SZSE.000002.SZ", "close": 11.0}],
             "failed_codes": []}
            for r in rule_ids
        ],
        "duration_sec": 0.1,
    }


def _publish(cfg, asof: int, rule_ids, *, scope: str = sc.RULES_SCOPE_ALL) -> dict:
    return ss.write_and_publish_snapshot(cfg, _snap_payload(asof, rule_ids, scope=scope))


def _write_week_index(cfg, weeks: dict) -> None:
    p = sc.snapshot_index_path(Path(cfg.storage_root))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"schema_version": "1", "weeks": weeks}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_track(cfg, snap_id: str, asof: int, rule_ids) -> None:
    """按契约结构落 track 产物 + current 指针（读取层测试用）。"""
    rows = [
        {"code": "SZSE.000001.SZ", "rule_id": rule_ids[0], "name": "测试一",
         "ret_close_sig": 0.05, "ret_close_exec": 0.03, "fill_status": "ok",
         "close_week_end": 10.5, "daily": [], "status": "ok"},
    ]
    aggs = [
        {"rule_id": r, "selected_count": 2, "valid_sig_count": 2,
         "valid_exec_count": 1, "pending_count": 0, "missing_count": 0,
         "unbuyable_count": 0, "unknown_count": 0,
         "win_rate_sig": 0.5, "win_rate_exec": 1.0,
         "mean_ret_close_sig": 0.05, "mean_ret_close_exec": 0.03,
         "mean_excess_sig": 0.01, "max_gain_weekday_dist": {}}
        for r in rule_ids
    ]
    product = {
        "schema_version": "2", "snapshot_id": snap_id, "week_id": asof, "asof": asof,
        "completion": "complete", "tracking_revision_id": "rev1", "algo_version": "1",
        "data_version": {}, "benchmark_data_version": {},
        "coverage": {"signal_close": 1.0, "week_first_open": 0.5, "excess": 1.0},
        "rows": rows, "rule_aggregates": aggs,
        "signal_week": [asof - 4, asof], "track_week": [asof + 3, asof + 7],
        "track_week_dates": [asof + 3, asof + 4, asof + 5, asof + 6, asof + 7],
        "short_week": False, "window_ended": True,
    }
    root = Path(cfg.storage_root)
    path = sc.track_path(root, snap_id, "rev1")
    if not path.exists():
        sc.create_snapshot_file_exclusive(path, product)
    cur = sc.track_current_path(root, snap_id)
    tmp = cur.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tracking_revision_id": "rev1"}), encoding="utf-8")
    tmp.replace(cur)


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    c = TestClient(create_app(env))
    yield c, env
    c.close()


@pytest.fixture()
def fake_runner(monkeypatch):
    """替身：不真起子进程，直接标记 done（分钟级 CLI 不进单测）。"""
    from wtpy.apps.astock.api_routes import track_backfill as tb

    calls = []

    def _fake(ctx, job):
        calls.append(dict(job))
        with ctx.track_backfill_lock:
            job["status"] = "done"
            job["exit_code"] = 0
            job["finished_at"] = tb._now()
            job["output"] = ["[TRACK] 指定规则补算完成"]

    monkeypatch.setattr(tb, "_run_backfill_job", _fake)
    return calls


# ---------------------------------------------------------------------------
# 1. 契约层：规则范围语义与发布护栏
# ---------------------------------------------------------------------------


class TestContractScope:
    def test_scope_defaults_to_all_for_legacy_snapshot(self):
        """旧快照（无 rules_scope 字段）必须按 all 处理（向后兼容）。"""
        assert sc.snapshot_rules_scope({}) == sc.RULES_SCOPE_ALL
        assert sc.snapshot_rules_scope({"rules_scope": None}) == sc.RULES_SCOPE_ALL
        assert sc.snapshot_rules_scope({"rules_scope": "weird"}) == sc.RULES_SCOPE_ALL
        assert sc.snapshot_rules_scope({"rules_scope": "subset"}) == sc.RULES_SCOPE_SUBSET

    def test_scoped_rule_ids_from_field_then_fallback(self):
        """有字段用字段；缺字段时按快照规则表兜底（读取方永远拿得到规则集）。"""
        snap = {
            "rules_scope": "subset", "scoped_rule_ids": [RULE_A],
            "rules": [{"rule_id": RULE_A}, {"rule_id": RULE_B}],
        }
        assert sc.scoped_rule_ids(snap) == [RULE_A]
        snap2 = {"rules_scope": "subset", "rules": [{"rule_id": RULE_A}]}
        assert sc.scoped_rule_ids(snap2) == [RULE_A]
        assert sc.scoped_rule_ids({"rules_scope": "all", "scoped_rule_ids": [RULE_A]}) == []

    def test_subset_notice_says_other_rules_have_no_list(self):
        """提示必须说清"其他规则当周没有名单"，不能留"当周空仓"的误读空间。"""
        txt = sc.subset_scope_notice([RULE_A, RULE_B], ["规则A", "规则B"])
        assert "指定规则补算" in txt and "2 条" in txt
        assert "规则A" in txt and "规则B" in txt
        assert "不代表" in txt

    def test_publish_subset_allowed_on_empty_historical_week(self, env):
        idx = {"20260731": {}, str(LATER_WEEK): {"published_snapshot_id": "sid"}}
        _write_week_index(env, idx)
        d = sc.publish_decision(
            Path(env.storage_root), WEEK, "sid_new", "backfill",
            rules_scope=sc.RULES_SCOPE_SUBSET,
        )
        assert d["publish"] is True
        assert d["reason"] == "subset_backfill_fills_empty_week"

    def test_publish_subset_refuses_latest_or_newer_week(self, env):
        """最新发布周及之后归周五链：子集（部分名单）不得占指针。"""
        _write_week_index(env, {str(LATER_WEEK): {"published_snapshot_id": "sid"}})
        # 该周本身已有指针 → already_published（更早的判定，同样拒绝）
        d0 = sc.publish_decision(
            Path(env.storage_root), LATER_WEEK, "sid_new", "backfill",
            rules_scope=sc.RULES_SCOPE_SUBSET,
        )
        assert d0["publish"] is False and d0["reason"] == "already_published"
        # 更新的一周（尚无指针，但对链来说是"未来周"）→ 历史周护栏拒绝
        d1 = sc.publish_decision(
            Path(env.storage_root), LATER_WEEK + 7, "sid_new", "backfill",
            rules_scope=sc.RULES_SCOPE_SUBSET,
        )
        assert d1["publish"] is False
        assert d1["reason"] == "subset_scope_not_historical_week"
        assert d1["latest_published_week"] == LATER_WEEK

    def test_publish_subset_never_replaces_existing_pointer(self, env):
        _write_week_index(env, {str(WEEK): {"published_snapshot_id": "sid_old"}})
        d = sc.publish_decision(
            Path(env.storage_root), WEEK, "sid_new", "backfill",
            rules_scope=sc.RULES_SCOPE_SUBSET,
        )
        assert d["publish"] is False and d["reason"] == "already_published"

    def test_unknown_scope_rejected(self, env):
        with pytest.raises(ValueError):
            sc.publish_decision(
                Path(env.storage_root), WEEK, "sid", "backfill", rules_scope="nope"
            )

    def test_publish_snapshot_records_scope_in_week_index(self, env):
        """索引冗余一份规则范围：读取方不加载快照也能区分全量周/子集周。"""
        pub = _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
        assert pub["published"] is True and pub["rules_scope"] == "subset"
        idx = sc.load_week_index(Path(env.storage_root))
        entry = idx["weeks"][str(WEEK)]
        assert entry["rules_scope"] == "subset"
        assert entry["scoped_rule_ids"] == [RULE_A]

    def test_full_snapshot_scope_recorded_as_all(self, env):
        _publish(env, WEEK, [RULE_A, RULE_B])
        idx = sc.load_week_index(Path(env.storage_root))
        entry = idx["weeks"][str(WEEK)]
        assert entry["rules_scope"] == "all" and entry["scoped_rule_ids"] == []


# ---------------------------------------------------------------------------
# 2. 快照组装：rules_scope 落进产物
# ---------------------------------------------------------------------------


class TestBuildPayloadScope:
    @staticmethod
    def _summary(asof: int, rule_ids):
        return {
            "asof": asof, "status": "ok", "generated_at": "2026-01-01 00:00:00",
            "universe_size": 3,
            "universe_codes": ["SZSE.000001.SZ", "SZSE.000002.SZ"],
            "universe_fingerprint": "ufp", "name_snapshot_id": "ns",
            "rule_fingerprints": {r: "fp_" + r for r in rule_ids},
            "scanned": 2, "missing_count": 0, "no_data_codes": [], "errors": [],
            "rules": [{"rule_id": r, "sheet": r, "count": 1, "matched": []}
                      for r in rule_ids],
            "duration_sec": 0.01,
        }

    def test_subset_payload_carries_scope_and_ids(self, env):
        p = ss.build_snapshot_payload(
            env, self._summary(WEEK, [RULE_A]), rule_ids=[RULE_A],
            run_kind="backfill", rules_scope=sc.RULES_SCOPE_SUBSET,
        )
        assert p["rules_scope"] == "subset" and p["scoped_rule_ids"] == [RULE_A]
        assert [r["rule_id"] for r in p["rules"]] == [RULE_A]

    def test_all_payload_has_empty_scoped_ids(self, env):
        p = ss.build_snapshot_payload(
            env, self._summary(WEEK, [RULE_A, RULE_B]), rule_ids=[RULE_A, RULE_B],
        )
        assert p["rules_scope"] == "all" and p["scoped_rule_ids"] == []

    def test_unknown_scope_raises(self, env):
        with pytest.raises(ValueError):
            ss.build_snapshot_payload(
                env, self._summary(WEEK, [RULE_A]), rule_ids=[RULE_A],
                rules_scope="nope",
            )


# ---------------------------------------------------------------------------
# 3. CLI：review-weekly 子集模式的 persist=False 与发布
# ---------------------------------------------------------------------------


class TestReviewCliSubset:
    @pytest.fixture()
    def fake_review(self, monkeypatch):
        """替身 review：记录入参并返回合成 summary（不跑全市场扫描）。

        persist=True 时按真实语义落 review_{asof}.json——否则"子集不覆盖
        复核文件"的断言就是空转（写文件的真实函数已被替身换掉）。
        """
        from wtpy.apps.astock.service import indicator_review as ir

        calls = {}

        def _fake(cfg, asof=None, *, rule_ids=None, **kw):
            calls["asof"] = asof
            calls["rule_ids"] = list(rule_ids or [])
            calls["persist"] = kw.get("persist")
            rs = list(rule_ids or [])
            summary = TestBuildPayloadScope._summary(int(asof or 0), rs)
            if kw.get("persist"):
                d = Path(cfg.storage_root) / "indicator_review"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"review_{int(asof or 0)}.json").write_text(
                    json.dumps(summary, ensure_ascii=False), encoding="utf-8"
                )
            return summary

        monkeypatch.setattr(ir, "run_weekly_review", _fake)
        return calls

    def _args(self, env, *extra):
        from wtpy.apps.astock import cli

        return cli.build_parser().parse_args(
            ["--storage", str(env.storage_root), "--indicator-dir", str(env.indicator_dir),
             "review-weekly", *extra]
        )

    def test_subset_review_does_not_write_review_file(self, env, fake_review):
        """子集复核必须 persist=False：绝不覆盖 review_{asof}.json。"""
        from wtpy.apps.astock import cli

        review_dir = Path(env.storage_root) / "indicator_review"
        review_dir.mkdir(parents=True, exist_ok=True)
        existing = review_dir / f"review_{WEEK}.json"
        existing.write_text(json.dumps({"asof": WEEK, "status": "ok",
                                        "rules": [{"rule_id": RULE_A}]}),
                            encoding="utf-8")
        rc = cli.cmd_review_weekly(self._args(
            env, "--asof", str(WEEK), "--rules", RULE_A, "--publish-scope", "subset",
        ))
        assert rc == 0
        assert fake_review["persist"] is False, "子集模式必须 persist=False"
        assert fake_review["rule_ids"] == [RULE_A]
        # 关键断言：既有全规则复核文件一字未动
        assert json.loads(existing.read_text(encoding="utf-8"))["status"] == "ok"
        assert "scoped_rule_ids" not in existing.read_text(encoding="utf-8")

    def test_subset_review_publishes_subset_snapshot(self, env, fake_review, capsys):
        from wtpy.apps.astock import cli

        rc = cli.cmd_review_weekly(self._args(
            env, "--asof", str(WEEK), "--rules", RULE_A, "--publish-scope", "subset",
        ))
        assert rc == 0
        capsys.readouterr()
        snap = ss.load_published_snapshot_for_week(env, WEEK)
        assert snap is not None
        assert sc.snapshot_rules_scope(snap) == sc.RULES_SCOPE_SUBSET
        assert sc.scoped_rule_ids(snap) == [RULE_A]
        # run_kind 缺省为 backfill（子集只用于历史周补算）
        assert snap["run_kind"] == "backfill"

    def test_subset_scope_requires_explicit_rules(self, env, fake_review, capsys):
        from wtpy.apps.astock import cli

        assert cli.cmd_review_weekly(
            self._args(env, "--publish-scope", "subset")
        ) == 2
        assert "必须显式指定 --rules" in capsys.readouterr().out
        assert cli.cmd_review_weekly(
            self._args(env, "--rules", "all", "--publish-scope", "subset")
        ) == 2

    def test_scope_all_requires_rules_all(self, env, fake_review, capsys):
        """--publish-scope all 配子集规则 = 把部分名单当全量发布，必须挡住。"""
        from wtpy.apps.astock import cli

        assert cli.cmd_review_weekly(
            self._args(env, "--rules", RULE_A, "--publish-scope", "all")
        ) == 2
        assert "必须与 --rules all 同时使用" in capsys.readouterr().out

    def test_unknown_rule_rejected(self, env, fake_review, capsys):
        from wtpy.apps.astock import cli

        assert cli.cmd_review_weekly(self._args(
            env, "--rules", "txt_不存在", "--publish-scope", "subset",
        )) == 2
        assert "规则不可执行或不存在" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. CLI：track-weekly --rules 的解析与回填护栏
# ---------------------------------------------------------------------------


def _backfill_args(env, **over) -> argparse.Namespace:
    base = dict(
        week=str(WEEK), backfill=1, rules=RULE_A, force=False, run_kind="backfill",
        previous_week=False, storage=str(env.storage_root),
        indicator_dir=str(env.indicator_dir), tdx_root=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


class TestBackfillRulesResolve:
    def test_rules_requires_backfill(self, env):
        from wtpy.apps.astock import cli

        with pytest.raises(ValueError) as e:
            cli._resolve_backfill_rules(env, _backfill_args(env, backfill=0))
        assert "仅与 --backfill 同用" in str(e.value)

    def test_unknown_rule_rejected(self, env):
        from wtpy.apps.astock import cli

        with pytest.raises(ValueError) as e:
            cli._resolve_backfill_rules(env, _backfill_args(env, rules="txt_不存在"))
        assert "规则不可执行或不存在" in str(e.value)

    def test_dedup_keeps_order(self, env):
        from wtpy.apps.astock import cli

        got = cli._resolve_backfill_rules(
            env, _backfill_args(env, rules=f"{RULE_B},{RULE_A},{RULE_B}")
        )
        assert got == [RULE_B, RULE_A]

    def test_no_rules_means_full_backfill(self, env):
        from wtpy.apps.astock import cli

        assert cli._resolve_backfill_rules(env, _backfill_args(env, rules=None)) is None


class TestBackfillGuard:
    """子集回填的逐周护栏与"复核完成但未发布"的如实上报。"""

    @pytest.fixture()
    def patched(self, env, monkeypatch):
        from wtpy.apps.astock import cli
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        calls = {"review": []}
        monkeypatch.setattr(cli, "_resolve_track_week", lambda cfg, args, **kw: WEEK)
        monkeypatch.setattr(tracksvc, "past_signal_weeks", lambda cal, a, n, **kw: [WEEK])
        monkeypatch.setattr(
            cli, "cmd_review_weekly",
            lambda ns: (calls["review"].append(ns), 0)[1],
        )
        return env, cli, tracksvc, calls, monkeypatch

    def test_subset_skips_latest_signal_week(self, patched, capsys):
        """最新信号周归周五链：子集补算直接跳过，不白跑一次扫描。"""
        env, cli, tracksvc, calls, mp = patched
        mp.setattr(tracksvc, "latest_signal_week", lambda cfg, cal=None: WEEK)
        rc = cli._cmd_track_backfill(
            _backfill_args(env), env, object(), 1, subset_rules=[RULE_A]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert calls["review"] == [], "护栏命中时不得发起 review"
        assert "skipped_subset_scope" in out
        assert "只支持历史周" in out

    def test_report_when_review_publishes_nothing(self, patched, capsys):
        """复核 exit 0 但被门槛/护栏拒绝发布 → 如实报 review_not_published，
        不能退化成含糊的 skipped（用户看不到真实原因）。"""
        env, cli, tracksvc, calls, mp = patched
        mp.setattr(tracksvc, "latest_signal_week", lambda cfg, cal=None: LATER_WEEK)
        mp.setattr(
            tracksvc, "should_recompute",
            lambda cfg, wk, **kw: (False, "no_published_snapshot"),
        )
        rc = cli._cmd_track_backfill(
            _backfill_args(env), env, object(), 1, subset_rules=[RULE_A]
        )
        out = capsys.readouterr().out
        assert "review_not_published" in out
        assert rc == 3, "未产出结果按可重试上报"

    def test_review_called_with_subset_scope_and_reports_skip(self, patched, capsys):
        env, cli, tracksvc, calls, mp = patched
        mp.setattr(tracksvc, "latest_signal_week", lambda cfg, cal=None: LATER_WEEK)

        def _should(cfg, wk, **kw):
            return (False, "no_published_snapshot") if not calls["review"] \
                else (False, "complete_and_revision_matches")

        mp.setattr(tracksvc, "should_recompute", _should)

        def _review(ns):
            calls["review"].append(ns)
            _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
            return 0

        mp.setattr(cli, "cmd_review_weekly", _review)
        rc = cli._cmd_track_backfill(
            _backfill_args(env), env, object(), 1, subset_rules=[RULE_A]
        )
        assert rc == 0
        ns = calls["review"][0]
        assert ns.publish_scope == "subset" and ns.rules == RULE_A
        assert "skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. API：track/backfill 的 rule_ids 校验、命令拼装与快速失败护栏
# ---------------------------------------------------------------------------


class TestBackfillApiRules:
    def test_rule_ids_appended_to_cmd(self, client):
        from wtpy.apps.astock.api_routes import track_backfill as tb

        _, cfg = client
        ctx = type("C", (), {"cfg": cfg})()
        cmd = tb._build_cmd(
            ctx, week=str(WEEK), weeks_back=None, rule_ids=[RULE_A, RULE_B]
        )  # type: ignore[arg-type]
        i = cmd.index("track-weekly")
        assert cmd[i + 1:] == [
            "--week", str(WEEK), "--backfill", "1", "--rules", f"{RULE_A},{RULE_B}"
        ], "全局参数仍须在子命令前，规则在最后"

    def test_no_rules_no_flag(self, client):
        from wtpy.apps.astock.api_routes import track_backfill as tb

        _, cfg = client
        ctx = type("C", (), {"cfg": cfg})()
        cmd = tb._build_cmd(ctx, week=str(WEEK), weeks_back=None)  # type: ignore[arg-type]
        assert "--rules" not in cmd

    def test_unknown_rule_400(self, client):
        c, env = client
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": ["txt_不存在"]})
        assert r.status_code == 400
        assert "不可执行或不存在" in r.json()["detail"]

    def test_empty_rule_ids_400(self, client):
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": ["", "  "]})
        assert r.status_code == 400
        assert "不能为空" in r.json()["detail"]

    def test_too_many_rules_400(self, client):
        from wtpy.apps.astock.api_routes import track_backfill as tb

        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK),
                         "rule_ids": [f"r{i}" for i in range(tb.MAX_SUBSET_RULES + 1)]})
        assert r.status_code == 400
        assert "最多" in r.json()["detail"]

    def test_rule_ids_need_week(self, client):
        c, _ = client
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"weeks_back": 3, "rule_ids": [RULE_A]})
        assert r.status_code == 400
        assert "需要同时给出 week" in r.json()["detail"]

    def test_existing_pointer_400(self, client):
        """该周已有发布名单：直接看单规则结果即可，不给子集补算。"""
        c, env = client
        _write_week_index(env, {str(WEEK): {
            "published_snapshot_id": "sid_old", "rules_scope": "all",
        }})
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": [RULE_A]})
        assert r.status_code == 400
        assert "已有全量发布快照" in r.json()["detail"]

    def test_existing_subset_pointer_400_mentions_resubmit(self, client):
        c, env = client
        _write_week_index(env, {str(WEEK): {
            "published_snapshot_id": "sid_old", "rules_scope": "subset",
        }})
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": [RULE_A]})
        assert r.status_code == 400
        assert "重新提交包含全部目标规则" in r.json()["detail"]

    def test_latest_signal_week_400(self, client, monkeypatch):
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        c, _ = client
        monkeypatch.setattr(
            tracksvc, "latest_signal_week", lambda cfg, cal=None: WEEK
        )
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": [RULE_A]})
        assert r.status_code == 400
        assert "最新的周归周五链" in r.json()["detail"]

    def test_submit_ok_and_status_carries_rule_ids(self, client, monkeypatch, fake_runner):
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        c, _ = client
        monkeypatch.setattr(tracksvc, "latest_signal_week", lambda cfg, cal=None: 0)
        r = c.post("/api/v1/bagua/track/backfill",
                   json={"week": str(WEEK), "rule_ids": [RULE_A, RULE_A]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rule_ids"] == [RULE_A], "重复规则应去重"
        assert "指定规则" in body["message"]
        for _ in range(50):
            st = c.get("/api/v1/bagua/track/backfill/status").json()
            if not st["running"]:
                break
        job = st["jobs"][0]
        assert job["rule_ids"] == [RULE_A]
        assert job["exit_code"] == 0

    def test_dup_detection_includes_rule_set(self, client, monkeypatch):
        """同周不同规则集不算重复（否则"补完 A 再补 B"会被 409 挡住）。"""
        from wtpy.apps.astock.api_routes import track_backfill as tb
        from wtpy.apps.astock.service import screen_tracking as tracksvc

        c, _ = client
        monkeypatch.setattr(tracksvc, "latest_signal_week", lambda cfg, cal=None: 0)
        release = []

        def _blocking(ctx, job):
            with ctx.track_backfill_lock:
                job["status"] = "running"
            release.append(job)
            while not job.get("_go"):
                time.sleep(0.01)

        monkeypatch.setattr(tb, "_run_backfill_job", _blocking)
        r1 = c.post("/api/v1/bagua/track/backfill",
                    json={"week": str(WEEK), "rule_ids": [RULE_A]})
        assert r1.status_code == 200
        for _ in range(100):  # 等首个任务真的进入 running（dedup 只看在途）
            if release:
                break
            time.sleep(0.01)
        assert release, "首个任务未进入在途状态"
        dup_same = c.post("/api/v1/bagua/track/backfill",
                          json={"week": str(WEEK), "rule_ids": [RULE_A]})
        assert dup_same.status_code == 409, "同周同规则集必须判重"
        other_rules = c.post("/api/v1/bagua/track/backfill",
                             json={"week": str(WEEK), "rule_ids": [RULE_B]})
        assert other_rules.status_code == 200, "不同规则集不得判重"
        for job in release:
            job["_go"] = True
        for _ in range(100):
            st = c.get("/api/v1/bagua/track/backfill/status").json()
            if not st["running"]:
                break
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# 6. 读取层：L0/L1/L2 必须带出规则范围
# ---------------------------------------------------------------------------


class TestReadLayerScope:
    def test_l2_reports_scope_and_notice(self, client):
        c, env = client
        pub = _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
        assert pub["published"] is True
        sid = pub["snapshot_id"]
        _write_track(env, sid, WEEK, [RULE_A])
        j = c.get(f"/api/v1/bagua/track/weeks/{WEEK}").json()
        assert j["rules_scope"] == "subset"
        assert j["scoped_rule_ids"] == [RULE_A]
        assert j["scope_notice"] and "不代表" in j["scope_notice"]

    def test_l2_full_week_has_no_scope_notice(self, client):
        c, env = client
        pub = _publish(env, WEEK, [RULE_A, RULE_B])
        _write_track(env, pub["snapshot_id"], WEEK, [RULE_A, RULE_B])
        j = c.get(f"/api/v1/bagua/track/weeks/{WEEK}").json()
        assert j["rules_scope"] == "all"
        assert j["scope_notice"] is None

    def test_l0_marks_subset_weeks(self, client):
        c, env = client
        pub = _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
        _write_track(env, pub["snapshot_id"], WEEK, [RULE_A])
        body = c.get("/api/v1/bagua/track/rules").json()
        row = next(r for r in body["rules"] if r["rule_id"] == RULE_A)
        assert row["subset_weeks"] == 1
        assert row["tracked_weeks"] == 1

    def test_l1_week_rows_carry_scope(self, client):
        c, env = client
        pub = _publish(env, WEEK, [RULE_A], scope=sc.RULES_SCOPE_SUBSET)
        _write_track(env, pub["snapshot_id"], WEEK, [RULE_A])
        body = c.get(f"/api/v1/bagua/track/rules/{RULE_A}/weeks").json()
        row = next(w for w in body["weeks"] if w["week_id"] == WEEK)
        assert row["rules_scope"] == "subset"
        assert row["scoped_rule_ids"] == [RULE_A]
