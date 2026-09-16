# -*- coding: utf-8 -*-
"""阶段 3 前置件测试：跟踪只读路由（三级钻取数据源）。

合成快照 + 合成跟踪产物（按契约结构手写），不依赖真实数据根、
不跑跟踪计算。字段约定与 screen_tracking 服务产出对齐：
- track 产物：{completion, tracking_revision_id, coverage,
  rows: [{code, rule_id, ret_close_sig, ret_close_exec, fill_status, ...}],
  rule_aggregates: [{rule_id, selected_count, valid_sig_count, valid_exec_count,
    mean_ret_close_sig, mean_ret_close_exec, mean_excess_sig, ...}]}
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss


@pytest.fixture()
def track_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    (ind / "测试规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
    cfg = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    client = TestClient(create_app(cfg))
    yield client, cfg, storage
    client.close()


def _publish_snap(cfg, asof, matched, rule_id="txt_测试规则A", fp="fpA", run_kind="weekly_chain"):
    codes = matched + [f"SZSE.00000{i}.SZ" for i in range(900, 903)]
    payload = {
        "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": sc.new_snapshot_id(asof),
        "run_kind": run_kind, "week_id": asof, "asof": asof,
        "generated_at": f"{asof} 18:40:00", "status": "ok",
        "universe_size": len(codes), "universe_codes": codes,
        "universe_fingerprint": f"ufp{asof}", "name_snapshot_id": "ns1",
        "rule_fingerprints": {rule_id: fp}, "data_version": {},
        "content_fingerprint": f"cfp{asof}", "scanned": len(codes),
        "missing_count": 0, "no_data_codes": [],
        "rules": [{
            "rule_id": rule_id, "sheet": "测试A", "status": "ok",
            "count": len(matched),
            "matched": [{"code": c, "close": 10.0} for c in matched],
            "failed_codes": [],
        }],
        "duration_sec": 0.1,
    }
    return ss.write_and_publish_snapshot(cfg, payload)["snapshot_id"]


def _write_track(cfg, snap_id, asof, *, completion="complete",
                 rows=None, aggregates=None, rev="rev1", coverage=None):
    """按契约结构落 track 产物 + current 指针。"""
    rows = rows if rows is not None else [
        {"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
         "ret_close_sig": 0.05, "ret_close_exec": 0.03, "fill_status": "ok"},
        {"code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
         "ret_close_sig": -0.02, "ret_close_exec": None, "fill_status": "limit_up_unbuyable"},
    ]
    aggregates = aggregates if aggregates is not None else [{
        "rule_id": "txt_测试规则A", "selected_count": 2,
        "valid_sig_count": 2, "valid_exec_count": 1,
        "pending_count": 0, "missing_count": 0, "unbuyable_count": 1, "unknown_count": 0,
        "win_rate_sig": 0.5, "win_rate_exec": 1.0,
        "mean_ret_close_sig": 0.015, "mean_ret_close_exec": 0.03,
        "mean_excess_sig": 0.01,
        "mean_max_gain_sig": 0.08, "mean_giveback_sig": 0.05,
        "max_gain_weekday_dist": {"1": 1, "5": 1},
    }]
    product = {
        "schema_version": "1", "snapshot_id": snap_id,
        "week_id": asof, "asof": asof, "completion": completion,
        "tracking_revision_id": rev,
        "algo_version": "1", "data_version": {}, "benchmark_data_version": {},
        "coverage": coverage or {"signal_close": 1.0, "week_first_open": 0.5, "excess": 1.0},
        "rows": rows, "rule_aggregates": aggregates,
    }
    root = Path(cfg.storage_root)
    sc.create_snapshot_file_exclusive(
        sc.track_path(root, snap_id, rev), product
    ) if not sc.track_path(root, snap_id, rev).exists() else None
    cur = sc.track_current_path(root, snap_id)
    tmp = cur.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tracking_revision_id": rev}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cur)
    return product


class TestTrackRulesOverview:
    def test_l0_overview_aggregates(self, track_client):
        """L0：周等权胜率/均值、票次加权并列、样本不足标注。"""
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000002.SZ"])
        _write_track(cfg, sid, 20260911)

        r = client.get("/api/v1/bagua/track/rules")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        rule = body["rules"][0]
        assert rule["rule_id"] == "txt_测试规则A"
        assert rule["tracked_weeks"] == 1 and rule["settled_weeks"] == 1
        # 周等权（单周=该周均值）
        assert rule["weekly_equal_mean_ret_sig"] == 0.015
        assert rule["weekly_equal_valid_weeks_sig"] == 1
        # 票次加权：2 票 sig（0.05 + -0.02）均值 = 0.015
        assert rule["ticket_mean_ret_sig"] == 0.015
        assert rule["ticket_valid_count_sig"] == 2
        # 胜率 ≠ 加权收益（审查 🔴：曾用 mean_ret×票数冒充胜率，1 票胜
        # 1 票负 → 胜率必须 0.5，绝不是 0.015）；且 ∈[0,1]
        assert rule["ticket_win_rate_sig"] == 0.5
        assert 0.0 <= rule["ticket_win_rate_sig"] <= 1.0
        assert rule["ticket_win_rate_sig"] != rule["ticket_mean_ret_sig"]
        # exec 口径 1/2 票（涨停剔除）→ 均值 0.03、票数 1、胜率 1.0
        assert rule["weekly_equal_mean_ret_exec"] == 0.03
        assert rule["ticket_win_rate_exec"] == 1.0
        # 总样本 < 30 → 标注样本不足
        assert rule["insufficient_sample"] is True

    def test_l0_empty_market_week_win_rate_null(self, track_client):
        """空仓周胜率 null 而非 0（契约 §3）。"""
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260911, [])
        _write_track(
            cfg, sid, 20260911, rows=[],
            aggregates=[{
                "rule_id": "txt_测试规则A", "selected_count": 0,
                "valid_sig_count": 0, "valid_exec_count": 0,
                "win_rate_sig": None, "mean_ret_close_sig": None,
                "mean_ret_close_exec": None, "mean_excess_sig": None,
            }],
        )
        r = client.get("/api/v1/bagua/track/rules")
        rule = r.json()["rules"][0]
        assert rule["weekly_equal_win_rate_sig"] is None  # null 非 0
        assert rule["weekly_equal_mean_ret_sig"] is None
        assert rule["tracked_weeks"] == 1  # 空仓周也计入跟踪周数

    def test_l0_ticket_win_rate_multi_week_weighted(self, track_client):
        """★审查 🔴 回归：票次胜率 = Σ(周胜率×周票数)/Σ票数，不是收益加权。

        构造两周、同指纹段：
        - 周1：4 票，胜率 0.5（2 胜 2 负），周均值收益 -0.10
        - 周2：2 票，胜率 1.0，周均值收益 0.02
        真实票次胜率 = (0.5×4 + 1.0×2)/6 = 2/3 ≈ 0.666667
        旧 bug（mean_ret×票数加权）会算出 (−0.10×4 + 0.02×2)/6 ≈ −0.06
        ——负数当"胜率"，必然被边界断言抓死。
        """
        client, cfg, _ = track_client
        sid1 = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000002.SZ",
                                             "SZSE.000003.SZ", "SZSE.000004.SZ"])
        _write_track(
            cfg, sid1, 20260911,
            rows=[
                {"code": f"SZSE.00000{i}.SZ", "rule_id": "txt_测试规则A",
                 "ret_close_sig": v, "ret_close_exec": v, "fill_status": "ok"}
                for i, v in [(1, 0.05), (2, 0.03), (3, -0.15), (4, -0.26)]
            ],
            aggregates=[{
                "rule_id": "txt_测试规则A", "selected_count": 4,
                "valid_sig_count": 4, "valid_exec_count": 4,
                "win_rate_sig": 0.5, "win_rate_exec": 0.5,
                "mean_ret_close_sig": -0.0825, "mean_ret_close_exec": -0.0825,
                "mean_excess_sig": None,
            }],
        )
        sid2 = _publish_snap(cfg, 20260918, ["SZSE.000001.SZ", "SZSE.000002.SZ"])
        _write_track(
            cfg, sid2, 20260918,
            rows=[
                {"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                 "ret_close_sig": 0.02, "ret_close_exec": 0.02, "fill_status": "ok"},
                {"code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
                 "ret_close_sig": 0.02, "ret_close_exec": 0.02, "fill_status": "ok"},
            ],
            aggregates=[{
                "rule_id": "txt_测试规则A", "selected_count": 2,
                "valid_sig_count": 2, "valid_exec_count": 2,
                "win_rate_sig": 1.0, "win_rate_exec": 1.0,
                "mean_ret_close_sig": 0.02, "mean_ret_close_exec": 0.02,
                "mean_excess_sig": None,
            }],
        )
        r = client.get("/api/v1/bagua/track/rules")
        rule = r.json()["rules"][0]
        assert rule["settled_weeks"] == 2
        assert rule["ticket_valid_count_sig"] == 6
        # 真实票次胜率（周胜率加权），绝不是收益加权（那会是 -0.045）
        assert rule["ticket_win_rate_sig"] == pytest.approx(2 / 3)
        assert 0.0 <= rule["ticket_win_rate_sig"] <= 1.0
        # 周等权胜率 = (0.5 + 1.0)/2 = 0.75，与票次口径互相独立可验证
        assert rule["weekly_equal_win_rate_sig"] == pytest.approx(0.75)

    def test_l0_fingerprint_segments_isolated(self, track_client):
        """规则改公式 → 指纹分段，历史与新股不混算（契约 §3）。"""
        client, cfg, _ = track_client
        sid1 = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ"], fp="fpA")
        _write_track(cfg, sid1, 20260911)
        # 下周同规则换指纹（公式已改）
        sid2 = _publish_snap(cfg, 20260918, ["SZSE.000002.SZ"], fp="fpB")
        _write_track(cfg, sid2, 20260918)

        r = client.get("/api/v1/bagua/track/rules")
        rules = r.json()["rules"]
        assert len(rules) == 2  # 两段分开
        fps = {x["fingerprint"] for x in rules}
        assert fps == {"fpA", "fpB"}

    def test_l0_unsettled_week_counted_but_not_aggregated(self, track_client):
        """待结算周：计入 tracked_weeks，但不进胜率聚合。"""
        client, cfg, _ = track_client
        sid1 = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ"])
        _write_track(cfg, sid1, 20260911)
        sid2 = _publish_snap(cfg, 20260918, ["SZSE.000002.SZ"])  # 无 track 产物
        r = client.get("/api/v1/bagua/track/rules")
        rule = r.json()["rules"][0]
        assert rule["tracked_weeks"] == 2
        assert rule["settled_weeks"] == 1
        assert rule["weekly_equal_valid_weeks_sig"] == 1  # 只有 0911 进聚合


class TestTrackRuleWeeks:
    def test_l1_weeks_desc_order_with_status(self, track_client):
        client, cfg, _ = track_client
        sid1 = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ"])
        _write_track(cfg, sid1, 20260911)
        sid2 = _publish_snap(cfg, 20260918, ["SZSE.000002.SZ"])  # 待结算
        r = client.get("/api/v1/bagua/track/rules/txt_测试规则A/weeks")
        body = r.json()
        assert body["count"] == 2
        assert [w["week_id"] for w in body["weeks"]] == [20260918, 20260911]  # 倒序
        by_wid = {w["week_id"]: w for w in body["weeks"]}
        assert by_wid[20260918]["completion"] == "no_product"
        assert by_wid[20260911]["completion"] == "complete"
        assert by_wid[20260918]["selected_count"] == 1

    def test_l1_rule_absent_in_old_weeks_skipped(self, track_client):
        """快照没算该规则的周不出现在该规则周列表（如新规则上线前）。"""
        client, cfg, _ = track_client
        _publish_snap(cfg, 20260911, ["SZSE.000001.SZ"], rule_id="txt_测试规则A")
        sid2 = _publish_snap(cfg, 20260918, ["SZSE.000001.SZ"], rule_id="txt_测试规则A")
        _publish_snap(cfg, 20260904, ["SZSE.000001.SZ"], rule_id="txt_其他规则", fp="fpX")
        r = client.get("/api/v1/bagua/track/rules/txt_测试规则A/weeks")
        wids = [w["week_id"] for w in r.json()["weeks"]]
        assert 20260904 not in wids
        assert set(wids) == {20260911, 20260918}


class TestTrackWeekDetail:
    def test_l2_detail_rows_and_pending(self, track_client):
        client, cfg, _ = track_client
        sid = _publish_snap(
            cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ"]
        )
        # track 只结算了前两只 → 第三只进 pending_picks
        _write_track(
            cfg, sid, 20260911,
            rows=[
                {"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                 "ret_close_sig": 0.05, "fill_status": "ok"},
                {"code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
                 "ret_close_sig": -0.02, "fill_status": "ok"},
            ],
        )
        r = client.get("/api/v1/bagua/track/weeks/20260911")
        body = r.json()
        assert body["completion"] == "complete"
        assert len(body["rows"]) == 2
        assert [p["code"] for p in body["pending_picks"]] == ["SZSE.000003.SZ"]
        assert body["backfill_notice"] is None

    def test_l2_backfill_notice_attached(self, track_client):
        """回填周的明细带免责提示全文（契约 §9）。"""
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260904, ["SZSE.000001.SZ"], run_kind="backfill")
        _write_track(cfg, sid, 20260904)
        r = client.get("/api/v1/bagua/track/weeks/20260904")
        body = r.json()
        assert body["backfill"] is True
        assert "不代表当时实际发布名单" in body["backfill_notice"]

    def test_l2_404_when_no_published_week(self, track_client):
        client, _cfg, _ = track_client
        r = client.get("/api/v1/bagua/track/weeks/20260911")
        assert r.status_code == 404

    def test_l2_pending_picks_carry_name(self, track_client, monkeypatch):
        """待结算名单带名称：读取时解析（不来自产物），缺名→空串。

        名称绝不拿代码冒充；缺名是如实状态（前端显示代码 + 空名）。
        """
        from wtpy.apps.astock.service import stock_names as sn

        monkeypatch.setattr(
            sn,
            "resolve_stock_name",
            lambda _cfg, code, **_kw: {"000003": "国农科技"}.get(str(code), ""),
        )
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000003.SZ"])
        # track 只结算第一只 → 第二只（000003）进 pending
        _write_track(
            cfg, sid, 20260911,
            rows=[{"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                   "ret_close_sig": 0.05, "fill_status": "ok"}],
        )
        body = client.get("/api/v1/bagua/track/weeks/20260911").json()
        picks = body["pending_picks"]
        assert [p["code"] for p in picks] == ["SZSE.000003.SZ"]
        assert picks[0]["name"] == "国农科技"
        # 已结算行缺 name（手写的是 v1 形态产物）→ 如实回 None，不编造
        assert body["rows"][0].get("name") is None

    def test_l2_rule_filter(self, track_client):
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000002.SZ"])
        _write_track(
            cfg, sid, 20260911,
            rows=[
                {"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A", "ret_close_sig": 0.05},
                {"code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A", "ret_close_sig": -0.02},
                {"code": "SZSE.000001.SZ", "rule_id": "txt_测试规则B", "ret_close_sig": 0.1},
            ],
        )
        r = client.get(
            "/api/v1/bagua/track/weeks/20260911",
            params={"rule_id": "txt_测试规则A"},
        )
        rows = r.json()["rows"]
        assert len(rows) == 2
        assert {row["code"] for row in rows} == {"SZSE.000001.SZ", "SZSE.000002.SZ"}


# ---------------------------------------------------------------------------
# 与规则中心保持一致（2026-09-15 用户要求）：删除同步 / 同公式多 id 归并
# ---------------------------------------------------------------------------


class TestRuleCatalogSync:
    """L0/L1/L2 接「当前规则目录」（规则中心可见 ∪ 周五链预置复核规则）。

    覆盖用户提的三件事：规则中心删掉的规则不再出现；同一公式的两个 rule_id
    （tn6_ 与其配对源 txt_）不再重复成两行；归并后 L1/L2 仍能取到兄弟 id 的
    产物数据（否则被归并的周会出现缺口）。另测目录不可用时的 fail-open 降级。
    """

    @staticmethod
    def _patch(monkeypatch, catalog):
        from wtpy.apps.astock.api_routes import tracking as tr

        monkeypatch.setattr(tr, "_current_rule_catalog", lambda _ctx: catalog)

    @staticmethod
    def _catalog(*items):
        """items: (rule_id, name, executable, hidden)"""
        return {
            rid: {
                "id": rid, "name": name, "executable": exe,
                "hidden": hid, "source": "builtin",
            }
            for rid, name, exe, hid in items
        }

    @staticmethod
    def _publish_multi(cfg, asof, rules):
        """发布含多条规则的快照；rules: [(rule_id, fingerprint, [codes])]。"""
        codes = sorted({c for _, _, ms in rules for c in ms}) + [
            f"SZSE.00000{i}.SZ" for i in range(900, 903)
        ]
        payload = {
            "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": sc.new_snapshot_id(asof),
            "run_kind": "weekly_chain", "week_id": asof, "asof": asof,
            "generated_at": f"{asof} 18:40:00", "status": "ok",
            "universe_size": len(codes), "universe_codes": codes,
            "universe_fingerprint": f"ufp{asof}", "name_snapshot_id": "ns1",
            "rule_fingerprints": {rid: fp for rid, fp, _ in rules},
            "data_version": {}, "content_fingerprint": f"cfp{asof}",
            "scanned": len(codes), "missing_count": 0, "no_data_codes": [],
            "rules": [
                {
                    "rule_id": rid, "sheet": rid, "status": "ok",
                    "count": len(ms),
                    "matched": [{"code": c, "close": 10.0} for c in ms],
                    "failed_codes": [],
                }
                for rid, _fp, ms in rules
            ],
            "duration_sec": 0.1,
        }
        return ss.write_and_publish_snapshot(cfg, payload)["snapshot_id"]

    def test_l0_hides_rules_removed_from_catalog(self, track_client, monkeypatch):
        """规则中心删掉的规则不再出现在 L0（删除同步）。"""
        client, cfg, _ = track_client
        sid = self._publish_multi(cfg, 20260911, [
            ("txt_测试规则A", "fpA", ["SZSE.000001.SZ"]),
            ("txt_已删除规则", "fpB", ["SZSE.000002.SZ"]),
        ])
        assert sid
        self._patch(
            monkeypatch, self._catalog(("txt_测试规则A", "活跃规则", True, False))
        )
        body = client.get("/api/v1/bagua/track/rules").json()
        assert [r["rule_id"] for r in body["rules"]] == ["txt_测试规则A"]
        assert body["rules"][0]["rule_name"] == "活跃规则", "名称应由后端直出"

    def test_empty_catalog_keeps_everything(self, track_client, monkeypatch):
        """目录不可用（降级）→ 不过滤不去重，宁可多显示历史也不白屏。"""
        client, cfg, _ = track_client
        self._publish_multi(cfg, 20260911, [
            ("txt_测试规则A", "fpA", ["SZSE.000001.SZ"]),
            ("txt_老规则", "fpB", ["SZSE.000002.SZ"]),
        ])
        self._patch(monkeypatch, {})
        body = client.get("/api/v1/bagua/track/rules").json()
        assert sorted(r["rule_id"] for r in body["rules"]) == [
            "txt_测试规则A", "txt_老规则"
        ]

    def test_l0_merges_same_fingerprint_into_one_row(self, track_client, monkeypatch):
        """同公式两个 id（tn6_ 与其配对源 txt_）→ 只展示一行。"""
        client, cfg, _ = track_client
        self._publish_multi(cfg, 20260911, [
            ("txt_735金叉及趋势", "fpSame", ["SZSE.000001.SZ"]),
            ("tn6_735金叉及趋势", "fpSame", ["SZSE.000001.SZ"]),
        ])
        self._patch(monkeypatch, self._catalog(
            ("txt_735金叉及趋势", "735金叉（txt）", True, True),
            ("tn6_735金叉及趋势", "735金叉及趋势", True, False),
        ))
        body = client.get("/api/v1/bagua/track/rules").json()
        assert body["count"] == 1, "同指纹必须归并成一行"
        row = body["rules"][0]
        assert row["rule_id"] == "tn6_735金叉及趋势", "canonical 取目录里未隐藏的那条"
        assert row["rule_name"] == "735金叉及趋势"
        assert row["merged_rule_ids"] == ["txt_735金叉及趋势"], "归并掉的 id 要可追溯"
        assert row["tracked_weeks"] == 1

    def test_l1_resolves_sibling_rule_id_via_fingerprint(self, track_client, monkeypatch):
        """L1 用 canonical id + 指纹参数能取到兄弟 id 的周。

        真实场景：新导入的 tn6_ 规则在任何快照里都没出现过，历史周只有它的
        配对源 txt_——L0 归并后显示有历史，L1 必须按指纹组取数，否则空白。
        """
        client, cfg, _ = track_client
        # 快照只含 txt_（模拟「tn6_ 还没进过任何快照」）
        sid = self._publish_multi(cfg, 20260911, [
            ("txt_735金叉及趋势", "fpSame", ["SZSE.000001.SZ"]),
        ])
        _write_track(cfg, sid, 20260911, rows=[
            {"code": "SZSE.000001.SZ", "rule_id": "txt_735金叉及趋势",
             "ret_close_sig": 0.05, "ret_close_exec": 0.03, "fill_status": "ok"},
        ])
        self._patch(monkeypatch, self._catalog(
            ("txt_735金叉及趋势", "735金叉（txt）", True, True),
            ("tn6_735金叉及趋势", "735金叉及趋势", True, False),
        ))
        # 不带指纹：入口 id 从未出现在任何快照 → 查不到（说明为何要带 fp）
        bare = client.get(
            "/api/v1/bagua/track/rules/tn6_735金叉及趋势/weeks"
        ).json()
        assert bare["count"] == 0
        # 带 L0 行的指纹 → 按同指纹组取到兄弟 id 的周
        body = client.get(
            "/api/v1/bagua/track/rules/tn6_735金叉及趋势/weeks",
            params={"fingerprint": "fpSame"},
        ).json()
        assert body["count"] == 1, "canonical id + 指纹必须能取到兄弟 id 的周"
        assert body["weeks"][0]["recorded_rule_id"] == "txt_735金叉及趋势"
        assert body["rule_name"] == "735金叉及趋势"

    def test_l2_filter_matches_same_fingerprint_group(self, track_client, monkeypatch):
        """L2 按同指纹组过滤：canonical id 能查到写在兄弟 id 名下的产物行。"""
        client, cfg, _ = track_client
        sid = self._publish_multi(cfg, 20260911, [
            ("txt_735金叉及趋势", "fpSame", ["SZSE.000001.SZ"]),
            ("tn6_735金叉及趋势", "fpSame", ["SZSE.000001.SZ"]),
        ])
        _write_track(cfg, sid, 20260911, rows=[
            {"code": "SZSE.000001.SZ", "rule_id": "txt_735金叉及趋势",
             "ret_close_sig": 0.05, "fill_status": "ok"},
        ])
        self._patch(
            monkeypatch, self._catalog(("tn6_735金叉及趋势", "735金叉及趋势", True, False))
        )
        body = client.get(
            "/api/v1/bagua/track/weeks/20260911",
            params={"rule_id": "tn6_735金叉及趋势"},
        ).json()
        assert [r["code"] for r in body["rows"]] == ["SZSE.000001.SZ"]
        assert body["rows"][0]["code_disp"] == "000001", "展示用短码（去市场前缀）"

    def test_l1_flags_rule_removed_from_catalog(self, track_client, monkeypatch):
        """规则被删除后历史仍可查（产物不可变），但打标记供前端提示。"""
        client, cfg, _ = track_client
        sid = self._publish_multi(cfg, 20260911, [
            ("txt_已删除规则", "fpD", ["SZSE.000001.SZ"]),
        ])
        _write_track(cfg, sid, 20260911, rows=[
            {"code": "SZSE.000001.SZ", "rule_id": "txt_已删除规则",
             "ret_close_sig": 0.05, "fill_status": "ok"},
        ])
        self._patch(monkeypatch, self._catalog(("txt_别的规则", "别的", True, False)))
        body = client.get(
            "/api/v1/bagua/track/rules/txt_已删除规则/weeks"
        ).json()
        assert body["removed_from_catalog"] is True
        assert body["count"] == 1


class TestTrackV11Metrics:
    """V1.1 P0 数据口径测试：exec excess 聚合、trend_weeks、ui_summary。"""

    def test_l0_exec_excess_and_trend_weeks(self, track_client):
        client, cfg, _ = track_client
        # 构造连续 3 周
        for wid, exc1, exc2 in [
            (20260828, 0.01, 0.03),
            (20260904, -0.02, 0.00),
            (20260911, 0.04, None),
        ]:
            sid = _publish_snap(cfg, wid, ["SZSE.000001.SZ", "SZSE.000002.SZ"])
            rows = [
                {
                    "code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                    "ret_close_sig": 0.02, "ret_close_exec": 0.02,
                    "fill_status": "ok", "status": "ok",
                    "excess_exec": exc1,
                },
                {
                    "code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
                    "ret_close_sig": -0.01, "ret_close_exec": -0.01,
                    "fill_status": "ok", "status": "ok",
                    "excess_exec": exc2,
                },
            ]
            _write_track(cfg, sid, wid, rows=rows)

        r = client.get("/api/v1/bagua/track/rules")
        assert r.status_code == 200
        rule = r.json()["rules"][0]

        # 检查 L0 exec excess
        assert "weekly_equal_mean_excess_exec" in rule
        assert "weekly_equal_valid_weeks_excess_exec" in rule
        assert rule["weekly_equal_valid_weeks_excess_exec"] == 3
        # 3 周的 mean_excess_exec:
        # 第1周: (0.01 + 0.03)/2 = 0.02
        # 第2周: (-0.02 + 0.00)/2 = -0.01
        # 第3周: 0.04 (exc2 为 None，分母为 1)
        # 等权均值 = (0.02 - 0.01 + 0.04) / 3 = 0.016667
        assert rule["weekly_equal_mean_excess_exec"] == pytest.approx(0.016667, abs=1e-5)

        # 检查 trend_weeks
        tw = rule["trend_weeks"]
        assert len(tw) == 3
        # 时间升序
        assert [w["week_id"] for w in tw] == [20260828, 20260904, 20260911]
        assert tw[0]["settled"] is True
        assert tw[0]["mean_ret_exec"] == pytest.approx(0.03)  # 来自 aggregate 的 mean_ret_close_exec
        assert tw[0]["mean_excess_exec"] == pytest.approx(0.02)
        assert tw[2]["mean_excess_exec"] == pytest.approx(0.04)

    def test_l1_rule_weeks_has_exec_excess(self, track_client):
        client, cfg, _ = track_client
        sid = _publish_snap(cfg, 20260911, ["SZSE.000001.SZ", "SZSE.000002.SZ"])
        rows = [
            {
                "code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": 0.03, "ret_close_exec": 0.03,
                "fill_status": "ok", "status": "ok",
                "excess_exec": 0.015,
            },
            {
                "code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": 0.01, "ret_close_exec": 0.01,
                "fill_status": "ok", "status": "ok",
                "excess_exec": -0.005,
            },
        ]
        _write_track(cfg, sid, 20260911, rows=rows)

        r = client.get("/api/v1/bagua/track/rules/txt_测试规则A/weeks")
        assert r.status_code == 200
        week_entry = r.json()["weeks"][0]
        agg = week_entry["aggregate"]
        assert agg["excess_exec_valid_count"] == 2
        assert agg["mean_excess_exec"] == pytest.approx(0.005)
        assert agg["win_rate_excess_exec"] == 0.5

    def test_l2_ui_summary_metrics_and_exclusions(self, track_client):
        client, cfg, _ = track_client
        # 4 只票: 1买入正收益且超额, 1买入负收益且基准缺(excess=None), 1涨停买不进, 1无K线
        sid = _publish_snap(
            cfg, 20260911,
            ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ", "SZSE.000004.SZ"]
        )
        rows = [
            {
                "code": "SZSE.000001.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": 0.06, "ret_close_exec": 0.05,
                "fill_status": "ok", "status": "ok",
                "excess_exec": 0.03,
            },
            {
                "code": "SZSE.000002.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": -0.01, "ret_close_exec": -0.02,
                "fill_status": "ok", "status": "ok",
                "excess_exec": None,  # 基准缺失
            },
            {
                "code": "SZSE.000003.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": 0.09, "ret_close_exec": None,
                "fill_status": "limit_up_unbuyable", "status": "ok",
                "excess_exec": None,
            },
            {
                "code": "SZSE.000004.SZ", "rule_id": "txt_测试规则A",
                "ret_close_sig": None, "ret_close_exec": None,
                "fill_status": "no_bar", "status": "no_bar",
                "excess_exec": None,
            },
        ]
        _write_track(cfg, sid, 20260911, rows=rows)

        r = client.get("/api/v1/bagua/track/weeks/20260911")
        assert r.status_code == 200
        body = r.json()
        assert "ui_summary" in body
        s = body["ui_summary"]
        assert s["selected_count"] == 4
        # valid_exec_count 必须排除 limit_up_unbuyable 和 no_bar
        assert s["valid_exec_count"] == 2
        # win_rate_exec: 2 只中有 1 只 > 0 → 0.5
        assert s["win_rate_exec"] == 0.5
        # mean_ret_exec: (0.05 - 0.02)/2 = 0.015
        assert s["mean_ret_exec"] == pytest.approx(0.015)
        # mean_excess_exec: 只有 1 只有有效超额 0.03
        assert s["mean_excess_exec"] == pytest.approx(0.03)
        # coverage
        assert s["return_coverage_exec"] == 0.5  # 2 / 4
        assert s["excess_coverage_exec"] == 0.25  # 1 / 4

