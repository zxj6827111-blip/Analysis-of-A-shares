# -*- coding: utf-8 -*-
"""阶段 1c cache-first 路由级测试：POST /screen 快照短路 + /screen/latest。

验收口径（契约 §A4）：同 snapshot_id + 规则集合 + 范围 + 组合方式下，
筛选与 latest 端点结果一致；未覆盖场景落回现算不冒充。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_snapshots as ss


def _make_client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir(parents=True)
    ind.mkdir(parents=True)
    (ind / "测试规则A.txt").write_text(
        "MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8"
    )
    cfg = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    app = create_app(cfg)
    client = TestClient(app)
    yield client, cfg
    client.close()


@pytest.fixture()
def snap_client(tmp_path: Path):
    yield from _make_client(tmp_path)


def _publish_snapshot(cfg, rule_id="txt_测试规则A", asof=20260911, hit=("SZSE.000001.SZ",)):
    """合成并发布一个可用快照（绕过计算，直接构造契约结构）。

    规则指纹必须用真实注册表指纹——cache 覆盖校验会比对当前公式指纹，
    合成指纹会被判 stale 落回现算（这正是契约要求的防陈旧语义）。
    """
    from wtpy.apps.astock.service.screening import _current_rule_fingerprints

    fps = _current_rule_fingerprints(cfg, [rule_id]) or {rule_id: "fp_A"}
    codes = ["SZSE.000001.SZ", "SZSE.000002.SZ", "SZSE.000003.SZ"]
    payload = {
        "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": sc.new_snapshot_id(asof),
        "run_kind": "weekly_chain",
        "week_id": asof,
        "asof": asof,
        "generated_at": "2026-09-11 18:40:00",
        "status": "ok",
        "universe_size": len(codes),
        "universe_codes": codes,
        "universe_fingerprint": "ufp1",
        "name_snapshot_id": "ns1",
        "rule_fingerprints": fps,
        "data_version": {},
        "content_fingerprint": "cfp1",
        "scanned": 3,
        "missing_count": 0,
        "no_data_codes": [],
        "rules": [
            {
                "rule_id": rule_id, "sheet": "测试A", "status": "ok",
                "count": len(hit),
                "matched": [{"code": c, "close": 10.0} for c in hit],
                "failed_codes": [],
            }
        ],
        "duration_sec": 0.1,
    }
    return ss.write_and_publish_snapshot(cfg, payload)


def _mock_resolve_asof(monkeypatch, cfg, asof=20260911):
    """让 resolve_screen_asof 在测试环境返回固定 asof（无真实数据面）。"""
    from wtpy.apps.astock.service import screening as scr

    monkeypatch.setattr(
        scr, "resolve_screen_asof",
        lambda cfg_, requested=None: (asof, {"formal_l1_id": "l1", "max_date": asof}),
    )


class TestPostScreenCacheFirst:
    def test_cache_hit_returns_instant_done(self, snap_client, monkeypatch):
        """快照覆盖 → 立即 done 任务（不排队）；source=cache 带数据日期。"""
        client, cfg = snap_client
        pub = _publish_snapshot(cfg)
        assert pub["published"] is True
        _mock_resolve_asof(monkeypatch, cfg)

        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any"},
        )
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "done"
        assert "快照缓存" in job["message"]

        r2 = client.get(f"/api/v1/bagua/screen/jobs/{job['job_id']}/result")
        assert r2.status_code == 200
        result = r2.json()["result"] if "result" in r2.json() else r2.json()
        result = result.get("result", result)
        assert result["source"] == "cache"
        assert result["asof"] == 20260911
        assert result["matched_count"] == 1
        assert result["hits"][0]["code"] == "SZSE.000001.SZ"

    def test_cache_result_identical_to_latest_endpoint(self, snap_client, monkeypatch):
        """验收：同快照/规则/范围 → POST cache 分支与 latest 端点结果一致。"""
        client, cfg = snap_client
        _publish_snapshot(cfg)
        _mock_resolve_asof(monkeypatch, cfg)

        r_post = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any"},
        )
        job = r_post.json()
        r_res = client.get(f"/api/v1/bagua/screen/jobs/{job['job_id']}/result")
        post_hits = r_res.json()["result"]["hits"]

        r_lat = client.get("/api/v1/bagua/screen/latest")
        latest = r_lat.json()
        assert latest["available"] is True
        assert latest["source"] == "cache"
        # 名单一致（字段级：代码与命中规则）
        assert [(h["code"], h["hit_rules"]) for h in latest["hits"]] == [
            (h["code"], h["hit_rules"]) for h in post_hits
        ]

    def test_missing_rule_falls_back_to_compute(self, snap_client, monkeypatch):
        """请求快照未覆盖的规则 → 不冒充空结果，落回现算任务（queued）。

        构造：快照只含 txt_测试规则A；请求再加一条真实存在（目录里有）
        但快照没算的规则 txt_测试规则B → uncovered → 现算。
        """
        client, cfg = snap_client
        ind = cfg.indicator_dir
        (ind / "测试规则B.txt").write_text(
            "MA10:=MA(C,10);\nXG:CROSS(C,MA10);", encoding="utf-8"
        )
        _publish_snapshot(cfg)  # 只含规则 A
        _mock_resolve_asof(monkeypatch, cfg)
        from wtpy.apps.astock.service import screening as scr

        monkeypatch.setattr(
            scr, "run_screen",
            lambda cfg_, **kw: {"status": "ok", "asof": 20260911, "hits": [],
                                "matched_count": 0, "universe_size": 0,
                                "evaluated": 0, "missing_count": 0,
                                "error_count": 0, "rules": [], "duration_sec": 0},
        )

        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A", "txt_测试规则B"], "match_mode": "any"},
        )
        assert r.status_code == 200
        job = r.json()
        assert job["status"] in ("queued", "running", "done")
        assert "快照缓存" not in (job.get("message") or "")

    def test_force_recompute_bypasses_cache(self, snap_client, monkeypatch):
        """force_recompute=1 → 绕过快照走现算（手动重算语义）。"""
        client, cfg = snap_client
        _publish_snapshot(cfg)
        _mock_resolve_asof(monkeypatch, cfg)
        from wtpy.apps.astock.service import screening as scr

        monkeypatch.setattr(
            scr, "run_screen",
            lambda cfg_, **kw: {"status": "ok", "asof": 20260911, "hits": [],
                                "matched_count": 0, "universe_size": 0,
                                "evaluated": 0, "missing_count": 0,
                                "error_count": 0, "rules": [], "duration_sec": 0},
        )
        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any",
                  "force_recompute": True},
        )
        job = r.json()
        assert job["status"] in ("queued", "running", "done")
        assert "快照缓存" not in (job.get("message") or "")

    def test_no_snapshot_falls_back_to_compute(self, snap_client, monkeypatch):
        """无发布快照 → 原路径（现算任务），前端体验不变。"""
        client, cfg = snap_client
        _mock_resolve_asof(monkeypatch, cfg)
        from wtpy.apps.astock.service import screening as scr

        monkeypatch.setattr(
            scr, "run_screen",
            lambda cfg_, **kw: {"status": "ok", "asof": 20260911, "hits": [],
                                "matched_count": 0, "universe_size": 0,
                                "evaluated": 0, "missing_count": 0,
                                "error_count": 0, "rules": [], "duration_sec": 0},
        )
        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any"},
        )
        assert r.status_code == 200
        assert r.json()["status"] in ("queued", "running", "done")


class TestLatestEndpoint:
    def test_latest_without_snapshot(self, snap_client):
        """无快照：available=false + 引导文案（前端显示"首次周五链后可用"）。"""
        client, _cfg = snap_client
        r = client.get("/api/v1/bagua/screen/latest")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["available"] is False
        assert "周五链" in body["message"]

    def test_latest_with_snapshot_any_mode(self, snap_client, monkeypatch):
        client, cfg = snap_client
        _publish_snapshot(cfg)
        _mock_resolve_asof(monkeypatch, cfg)
        r = client.get("/api/v1/bagua/screen/latest")
        body = r.json()
        assert body["available"] is True
        assert body["asof"] == 20260911
        assert [h["code"] for h in body["hits"]] == ["SZSE.000001.SZ"]

    def test_latest_all_mode_single_rule(self, snap_client, monkeypatch):
        client, cfg = snap_client
        _publish_snapshot(cfg)
        _mock_resolve_asof(monkeypatch, cfg)
        r = client.get(
            "/api/v1/bagua/screen/latest",
            params={"match_mode": "all"},
        )
        body = r.json()
        assert body["available"] is True
        # 单规则 all=any（同规则命中集）
        assert [h["code"] for h in body["hits"]] == ["SZSE.000001.SZ"]


class TestReviewFixes:
    """审查修复回归锁：🔴B2 asof=latest 归一化、🔴B3 指纹 fail-closed。"""

    def test_asof_latest_string_accepted_by_cache_path(self, snap_client, monkeypatch):
        """asof="latest"（公共 API 字面量）在 cache 命中路径必须与缺省等价，
        不得因 _parse_asof 直通而 400（审查 🔴B2）。"""
        client, cfg = snap_client
        _publish_snapshot(cfg)
        _mock_resolve_asof(monkeypatch, cfg, asof=20260911)

        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any",
                  "asof": "latest"},
        )
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "done"
        res = client.get(f"/api/v1/bagua/screen/jobs/{job['job_id']}/result").json()
        result = res.get("result", res)
        assert result["source"] == "cache" and result["asof"] == 20260911

    def test_deleted_rule_makes_snapshot_stale_not_silent_reuse(self, snap_client, monkeypatch):
        """快照含一条当前已删除的规则时：该规则指纹解析为 None → stale
        → 整组落回现算，绝不用旧公式结果冒充（审查 🔴B3 fail-open 修复）。"""
        client, cfg = snap_client
        # 快照含两条规则；规则 B 稍后"删除"（从指标目录移除）
        ind = cfg.indicator_dir
        (ind / "测试规则B.txt").write_text(
            "MA10:=MA(C,10);\nXG:CROSS(C,MA10);", encoding="utf-8"
        )
        from wtpy.apps.astock.service.screening import _current_rule_fingerprints
        fps = _current_rule_fingerprints(cfg, ["txt_测试规则A", "txt_测试规则B"])
        assert "txt_测试规则B" in fps
        codes = ["SZSE.000001.SZ", "SZSE.000002.SZ"]
        from wtpy.apps.astock.service import screen_snapshots as ss
        from wtpy.apps.astock.service import screen_contract as sc
        payload = {
            "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": sc.new_snapshot_id(20260911),
            "run_kind": "weekly_chain", "week_id": 20260911, "asof": 20260911,
            "generated_at": "2026-09-11 18:40:00", "status": "ok",
            "universe_size": 2, "universe_codes": codes,
            "universe_fingerprint": "ufp1", "name_snapshot_id": "ns1",
            "rule_fingerprints": fps, "data_version": {},
            "content_fingerprint": "cfp1", "scanned": 2,
            "missing_count": 0, "no_data_codes": [],
            "rules": [
                {"rule_id": "txt_测试规则A", "sheet": "测试A", "status": "ok",
                 "count": 1, "matched": [{"code": "SZSE.000001.SZ", "close": 10.0}],
                 "failed_codes": []},
                {"rule_id": "txt_测试规则B", "sheet": "测试B", "status": "ok",
                 "count": 1, "matched": [{"code": "SZSE.000002.SZ", "close": 20.0}],
                 "failed_codes": []},
            ],
            "duration_sec": 0.1,
        }
        assert ss.write_and_publish_snapshot(cfg, payload)["published"] is True
        # 删除规则 B（当前注册表解析不到它）
        (ind / "测试规则B.txt").unlink()
        # 服务层直测：路由预检走 registry bootstrap 缓存仍能看到 B（目录
        # 扫描缓存），这里直接验证 try_screen_from_snapshot 的 stale 语义
        from wtpy.apps.astock.service import screening as scr
        res = scr.try_screen_from_snapshot(
            cfg,
            rule_ids=["txt_测试规则A", "txt_测试规则B"],
            match_mode="any", asof=None, codes=None,
        )
        # 已删除的规则使快照 stale → 覆盖不完整 → 返回 None 落回现算，
        # 不得静默用快照旧命中冒充（修复前：B 解析 KeyError → fps=None →
        # snapshot_covers 跳过全部校验 → 返回旧结果 dict）
        assert res is None

    def test_single_rule_still_cacheable_when_others_deleted(self, snap_client, monkeypatch):
        """规则 B 删除后仅请求规则 A：A 指纹完好 → cache 仍可服务（fail-closed 不误伤）。"""
        client, cfg = snap_client
        ind = cfg.indicator_dir
        (ind / "测试规则B.txt").write_text(
            "MA10:=MA(C,10);\nXG:CROSS(C,MA10);", encoding="utf-8"
        )
        from wtpy.apps.astock.service.screening import _current_rule_fingerprints
        from wtpy.apps.astock.service import screen_snapshots as ss
        from wtpy.apps.astock.service import screen_contract as sc
        fps = _current_rule_fingerprints(cfg, ["txt_测试规则A", "txt_测试规则B"])
        payload = {
            "schema_version": ss.SNAPSHOT_SCHEMA_VERSION,
            "snapshot_id": sc.new_snapshot_id(20260911),
            "run_kind": "weekly_chain", "week_id": 20260911, "asof": 20260911,
            "generated_at": "2026-09-11 18:40:00", "status": "ok",
            "universe_size": 2,
            "universe_codes": ["SZSE.000001.SZ", "SZSE.000002.SZ"],
            "universe_fingerprint": "ufp1", "name_snapshot_id": "ns1",
            "rule_fingerprints": fps, "data_version": {},
            "content_fingerprint": "cfp1", "scanned": 2,
            "missing_count": 0, "no_data_codes": [],
            "rules": [
                {"rule_id": "txt_测试规则A", "sheet": "测试A", "status": "ok",
                 "count": 1, "matched": [{"code": "SZSE.000001.SZ", "close": 10.0}],
                 "failed_codes": []},
                {"rule_id": "txt_测试规则B", "sheet": "测试B", "status": "ok",
                 "count": 0, "matched": [], "failed_codes": []},
            ],
            "duration_sec": 0.1,
        }
        ss.write_and_publish_snapshot(cfg, payload)
        (ind / "测试规则B.txt").unlink()  # B 删除，但只请求 A
        _mock_resolve_asof(monkeypatch, cfg)
        r = client.post(
            "/api/v1/bagua/screen",
            json={"rule_ids": ["txt_测试规则A"], "match_mode": "any"},
        )
        job = r.json()
        assert job["status"] == "done" and "快照缓存" in job["message"]
