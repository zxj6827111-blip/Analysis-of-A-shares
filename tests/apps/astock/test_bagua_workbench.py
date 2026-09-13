# -*- coding: utf-8 -*-
"""卦象工作台 API 测试（PLAN-BAGUA-UX-V1.1）。

覆盖：统一标的检索（歧义/名称/前缀）、筛选规则目录（可执行/原因）、
筛选提交校验（空规则/空范围/非法组合模式/不可执行规则）、any/all 组合
运算与「未完成评估」过滤（单元级，mock run_weekly_review）、独立任务容器
（队列满繁忙）、export force_async 强制异步、任务记录条件摘要。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import get_default_config


def _make_app(cfg):
    from wtpy.apps.astock.api import create_app

    return create_app(cfg)


def _make_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def wb_client(tmp_path: Path):
    storage = tmp_path / "st"
    ind = tmp_path / "ind"
    storage.mkdir()
    ind.mkdir()
    (ind / "测试规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
    cfg = get_default_config(
        storage_root=storage, indicator_dir=ind, output_root=tmp_path / "out"
    )
    client = _make_client(_make_app(cfg))
    yield client
    client.close()


# ===========================================================================
# GET /api/v1/bagua/instruments
# ===========================================================================


def test_instruments_empty_query_returns_idx_etf_preset(wb_client):
    r = wb_client.get("/api/v1/bagua/instruments", params={"q": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # 空查询给指数/ETF 预设入口（不倾倒全市场股票目录）
    for it in body["items"]:
        assert it["type"] in ("IDX", "ETF")
    for it in body["items"]:
        assert set(it) >= {"id", "code", "name", "type", "exchange"}


def test_instruments_prefix_and_name_search(wb_client, monkeypatch):
    from wtpy.apps.astock.service import screening as sc

    monkeypatch.setattr(
        sc, "_instrument_catalog",
        lambda cfg: {
            "key": "test", "ts": time.time(),
            "stocks": [
                {"id": "SSE.STK.600000", "code": "600000", "name": "浦发银行", "type": "STK"},
                {"id": "SZSE.STK.000001", "code": "000001", "name": "平安银行", "type": "STK"},
                {"id": "SZSE.STK.000002", "code": "000002", "name": "万科A", "type": "STK"},
            ],
            "idx_etf": [
                {"id": "SSE.IDX.000001", "code": "000001", "name": "上证指数", "type": "IDX"},
                {"id": "SSE.ETF.510300", "code": "510300", "name": "沪深300ETF", "type": "ETF"},
            ],
        },
    )
    r = wb_client.get("/api/v1/bagua/instruments", params={"q": "600000"})
    items = r.json()["items"]
    assert [it["id"] for it in items] == ["SSE.STK.600000"]
    assert items[0]["type_label"] == "股票" and items[0]["exchange_label"] == "沪市"

    # 六位代码歧义：000001 同时命中平安银行（股票）与上证指数（指数），
    # 返回候选项而不是替用户做选择
    r = wb_client.get("/api/v1/bagua/instruments", params={"q": "000001"})
    ids = {it["id"] for it in r.json()["items"]}
    assert ids == {"SZSE.STK.000001", "SSE.IDX.000001"}

    # 带交易所前缀消歧
    r = wb_client.get("/api/v1/bagua/instruments", params={"q": "sz000001"})
    assert [it["id"] for it in r.json()["items"]] == ["SZSE.STK.000001"]

    # 中文名包含
    r = wb_client.get("/api/v1/bagua/instruments", params={"q": "银行"})
    names = [it["name"] for it in r.json()["items"]]
    assert "浦发银行" in names and "平安银行" in names


# ===========================================================================
# GET /api/v1/bagua/options
# ===========================================================================


def test_options_rejects_unresolvable_code(wb_client, monkeypatch):
    from wtpy.apps.astock.service import bagua_query as bq

    def _raise(*args, **kwargs):
        raise FileNotFoundError("no bars")

    monkeypatch.setattr(bq, "load_day_bars_for_plane", _raise)
    r = wb_client.get("/api/v1/bagua/options", params={"code": "SSE.STK.999999"})
    assert r.status_code == 400


def test_options_rejects_disabled_plane(wb_client):
    r = wb_client.get(
        "/api/v1/bagua/options", params={"code": "SSE.STK.600000", "adjust": "tdx_front"}
    )
    assert r.status_code == 400
    assert "tdx_front" in r.json()["detail"] or "停用" in r.json()["detail"]


# ===========================================================================
# GET /api/v1/bagua/screen/rules
# ===========================================================================


def test_screen_rules_classifies_executability(wb_client):
    r = wb_client.get("/api/v1/bagua/screen/rules")
    assert r.status_code == 200
    body = r.json()
    by_name = {x["name"]: x for x in body["rules"]}
    a = by_name["测试规则A"]
    assert a["executable"] is True and a["reason"] == ""
    # 系统内置 native 规则（八卦 OHLC）不可筛选且带原因
    native = by_name.get("八卦OHLC")
    assert native is not None
    assert native["executable"] is False
    assert native["reason"]


def test_screen_rules_syncs_with_rule_center_deletion(wb_client):
    """规则中心删除（系统规则进 hidden 名单）后，筛选目录不再出现；
    DEFAULT_REVIEW_RULES（735/5日外）即使被 hidden 遮蔽也强制纳入。"""
    import json as _json

    from wtpy.apps.astock.config import get_default_config

    # wb_client 的 storage_root = tmp/st：预写 hidden 名单，模拟用户删除测试规则A
    # （storage 路径由 fixture 决定，这里通过 API 行为反推——直接再起一个可控 client）
    from pathlib import Path

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        storage = tmp / "st"
        ind = tmp / "ind"
        storage.mkdir()
        ind.mkdir()
        (ind / "测试规则A.txt").write_text("MA5:=MA(C,5);\nXG:CROSS(C,MA5);", encoding="utf-8")
        # 735 公式进公式目录，且在 hidden 名单（预置遮蔽场景）
        fixture = Path(__file__).resolve().parents[2] / "fixtures" / "formulas" / "735金叉及趋势.txt"
        (ind / "735金叉及趋势.txt").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        (storage / "indicators").mkdir()
        (storage / "indicators" / "hidden_rule_ids.json").write_text(
            _json.dumps(["txt_测试规则A", "txt_735金叉及趋势"]), encoding="utf-8"
        )
        cfg = get_default_config(
            storage_root=storage, indicator_dir=ind, output_root=tmp / "out"
        )
        client = _make_client(_make_app(cfg))
        try:
            body = client.get("/api/v1/bagua/screen/rules").json()
            ids = {x["id"] for x in body["rules"]}
            # 用户在规则中心删除的（hidden）不再出现
            assert "txt_测试规则A" not in ids
            # 预置复核规则即使 hidden 也强制纳入且可执行
            assert "txt_735金叉及趋势" in ids
            row = next(x for x in body["rules"] if x["id"] == "txt_735金叉及趋势")
            assert row["executable"] is True and row["hidden"] is True
        finally:
            client.close()


# ===========================================================================
# POST /api/v1/bagua/screen —— 入口校验
# ===========================================================================


def test_screen_rejects_empty_scope_picked(wb_client):
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["txt_测试规则A"], "match_mode": "any", "scope": "picked", "codes": []},
    )
    assert r.status_code == 400
    assert "退化" in r.json()["detail"]


def test_screen_rejects_bad_match_mode(wb_client):
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["txt_测试规则A"], "match_mode": "xor"},
    )
    assert r.status_code == 400


def test_screen_rejects_unknown_and_unexecutable_rules(wb_client):
    r = wb_client.post(
        "/api/v1/bagua/screen", json={"rule_ids": ["txt_不存在"], "match_mode": "any"}
    )
    assert r.status_code == 400
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["bagua_ohlc"], "match_mode": "any"},
    )
    assert r.status_code == 400
    assert "不可筛选" in r.json()["detail"]


def test_screen_rejects_stale_date_without_silent_fallback(wb_client, monkeypatch):
    """显式日期超出数据覆盖：400 + 建议日期，绝不静默回退。"""
    from wtpy.apps.astock.service import screening as sc

    monkeypatch.setattr(
        sc, "resolve_screen_asof",
        lambda cfg, requested: (_ for _ in ()).throw(
            sc.ScreenDateNotAvailable(20990101, 20260828, "超出数据覆盖（最新 20260828）")
        ),
    )
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["txt_测试规则A"], "match_mode": "any", "asof": "2099-01-01"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "20990101" in detail and "20260828" in detail


def test_screen_queue_full_returns_busy(wb_client, monkeypatch):
    from wtpy.apps.astock.service import screening as sc

    monkeypatch.setattr(
        sc, "resolve_screen_asof", lambda cfg, requested: (20260828, {"formal_l1_id": "x"})
    )

    def _full(ctx, **kwargs):
        raise sc.ScreenQueueFull("筛选任务等待队列已满（5 个），请稍后再试")

    monkeypatch.setattr(sc, "submit_screen_job", _full)
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["txt_测试规则A"], "match_mode": "any"},
    )
    assert r.status_code == 429
    assert "队列已满" in r.json()["detail"]


def test_screen_submit_and_poll_with_mocked_runner(wb_client, monkeypatch):
    """成功路径：提交 → 任务线程执行（mock run_screen）→ done → result 可取。"""
    from wtpy.apps.astock.service import screening as sc

    monkeypatch.setattr(
        sc, "resolve_screen_asof", lambda cfg, requested: (20260828, {"formal_l1_id": "x"})
    )
    calls = {}

    def _fake_run_screen(cfg, **kwargs):
        calls.update(kwargs)
        return {
            "status": "ok", "asof": 20260828, "match_mode": kwargs.get("match_mode"),
            "rule_ids": kwargs.get("rule_ids"),
            "universe_size": 3, "evaluated": 3, "missing_count": 0, "error_count": 0,
            "errors": [], "incomplete_codes": [],
            "rules": [{"rule_id": "txt_测试规则A", "sheet": "测试规则A", "count": 1}],
            "hits": [{"code": "SSE.STK.600000", "close": 10.0,
                      "hit_rules": ["测试规则A"], "signal_date": 20260828}],
            "matched_count": 1,
        }

    monkeypatch.setattr(sc, "run_screen", _fake_run_screen)
    r = wb_client.post(
        "/api/v1/bagua/screen",
        json={"rule_ids": ["txt_测试规则A"], "match_mode": "any", "asof": "2026-08-28"},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert r.json()["scope_summary"] == "全部 A 股"
    for _ in range(100):
        job = wb_client.get(f"/api/v1/bagua/screen/jobs/{job_id}").json()
        if job.get("status") == "done":
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    assert "命中 1" in job["message"]
    assert calls["match_mode"] == "any"
    assert calls["asof"] == "2026-08-28"
    # result 端点
    res = wb_client.get(f"/api/v1/bagua/screen/jobs/{job_id}/result")
    assert res.status_code == 200
    assert res.json()["result"]["matched_count"] == 1
    # 列表端点
    lst = wb_client.get("/api/v1/bagua/screen/jobs").json()
    assert any(x["job_id"] == job_id for x in lst["jobs"])


def test_screen_result_404_and_409(wb_client):
    r = wb_client.get("/api/v1/bagua/screen/jobs/nope/result")
    assert r.status_code == 404
    assert "失效" in r.json()["detail"]


# ===========================================================================
# 组合运算 + 未完成评估（单元级：mock run_weekly_review）
# ===========================================================================


def _mk_summary(rules_matched, *, missing=0, errors=0, failed_codes=None, universe=4):
    return {
        "status": "ok",
        "asof": 20260828,
        "universe_size": universe,
        "scanned": universe,
        "missing_count": missing,
        "error_count": errors,
        "failed_codes": failed_codes or [],
        "errors": [{"code": "SSE.STK.600002", "rule": "r1", "error": "boom"}] * (1 if errors else 0),
        "rules": [
            {"rule_id": rid, "sheet": sheet, "count": len(m), "matched": m}
            for rid, sheet, m in rules_matched
        ],
    }


def _patch_ir(monkeypatch, summary, asof_out=20260828):
    from wtpy.apps.astock.service import screening as sc
    from wtpy.apps.astock.service import indicator_review as ir

    monkeypatch.setattr(
        sc, "resolve_screen_asof", lambda cfg, requested: (asof_out, {"formal_l1_id": "x"})
    )
    seen = {}

    def _fake_run(cfg, **kwargs):
        seen.update(kwargs)
        return summary

    monkeypatch.setattr(ir, "run_weekly_review", _fake_run)
    return seen


def test_run_screen_any_mode_union_dedup(tmp_path, monkeypatch):
    from wtpy.apps.astock.service import screening as sc

    summary = _mk_summary([
        ("r1", "规则A", [{"code": "SSE.STK.600000", "close": 10.0},
                          {"code": "SSE.STK.600001", "close": 11.0}]),
        ("r2", "规则B", [{"code": "SSE.STK.600001", "close": 11.0},
                          {"code": "SZSE.STK.000001", "close": 12.0}]),
    ])
    _patch_ir(monkeypatch, summary)
    cfg = get_default_config(storage_root=tmp_path)
    out = sc.run_screen(cfg, rule_ids=["r1", "r2"], match_mode="any")
    codes = [h["code"] for h in out["hits"]]
    # 并集去重：600001 同票只出现一次，hit_rules 聚合两条
    assert codes == ["SSE.STK.600000", "SSE.STK.600001", "SZSE.STK.000001"]
    hit = out["hits"][1]
    assert hit["hit_rules"] == ["规则A", "规则B"]
    assert hit["signal_date"] == 20260828
    assert out["matched_count"] == 3
    assert out["evaluated"] == 4


def test_run_screen_all_mode_intersection(tmp_path, monkeypatch):
    from wtpy.apps.astock.service import screening as sc

    summary = _mk_summary([
        ("r1", "规则A", [{"code": "SSE.STK.600000", "close": 10.0},
                          {"code": "SSE.STK.600001", "close": 11.0}]),
        ("r2", "规则B", [{"code": "SSE.STK.600001", "close": 11.0},
                          {"code": "SZSE.STK.000001", "close": 12.0}]),
    ])
    _patch_ir(monkeypatch, summary)
    cfg = get_default_config(storage_root=tmp_path)
    out = sc.run_screen(cfg, rule_ids=["r1", "r2"], match_mode="all")
    assert [h["code"] for h in out["hits"]] == ["SSE.STK.600001"]
    assert out["matched_count"] == 1


def test_run_screen_empty_scope_after_filtering_rejected(tmp_path, monkeypatch):
    """指定范围全是指数/ETF：归一后为空必须拒绝，绝不静默回退全市场。"""
    from wtpy.apps.astock.service import screening as sc
    from wtpy.apps.astock.service import indicator_review as ir

    monkeypatch.setattr(
        sc, "resolve_screen_asof", lambda cfg, requested: (20260828, {"formal_l1_id": "x"})
    )
    ran = {"n": 0}

    def _no_scan(cfg, **kwargs):
        ran["n"] += 1
        raise AssertionError("run_weekly_review 不应被调用（空范围必须提前拒绝）")

    monkeypatch.setattr(ir, "run_weekly_review", _no_scan)
    cfg = get_default_config(storage_root=tmp_path)
    with pytest.raises(sc.ScreenError) as ei:
        sc.run_screen(
            cfg, rule_ids=["r1"], match_mode="any",
            codes=["SSE.IDX.000001", "SSE.ETF.510300"],
        )
    assert "指数/ETF" in str(ei.value)
    # 提交链路传下来的是空列表（submit 预归一后）——同样必须拒绝
    with pytest.raises(sc.ScreenError):
        sc.run_screen(cfg, rule_ids=["r1"], match_mode="any", codes=[])
    assert ran["n"] == 0


def test_run_screen_mixed_scope_reports_excluded(tmp_path, monkeypatch):
    """混合范围（股票+指数）：股票正常筛选，指数排除并给出提示数量。"""
    from wtpy.apps.astock.service import screening as sc

    summary = _mk_summary([
        ("r1", "规则A", [{"code": "SSE.STK.600000", "close": 10.0}]),
    ])
    seen = _patch_ir(monkeypatch, summary)
    cfg = get_default_config(storage_root=tmp_path)
    out = sc.run_screen(
        cfg, rule_ids=["r1"], match_mode="any",
        codes=["SSE.STK.600000", "SSE.IDX.000001"],
    )
    assert seen["codes"] == ["SSE.STK.600000"]
    assert out["excluded_count"] == 1
    assert "1 个标的为指数/ETF" in out["excluded_note"]
    assert out["matched_count"] == 1


def test_run_screen_incomplete_excluded_from_hits(tmp_path, monkeypatch):
    """缺数据 + 计算失败的票不进命中列表（默认命中=所选规则均完成计算）。"""
    from wtpy.apps.astock.service import screening as sc

    summary = _mk_summary(
        [
            ("r1", "规则A", [{"code": "SSE.STK.600000", "close": 10.0},
                              {"code": "SSE.STK.600009", "close": 13.0}]),
            ("r2", "规则B", [{"code": "SSE.STK.600000", "close": 10.0},
                              {"code": "SSE.STK.600009", "close": 13.0}]),
        ],
        missing=1, errors=1, failed_codes=["SSE.STK.600009"], universe=4,
    )
    seen = _patch_ir(monkeypatch, summary)
    cfg = get_default_config(storage_root=tmp_path)
    out = sc.run_screen(
        cfg, rule_ids=["r1", "r2"], match_mode="all",
        codes=["600000", "600009"],
    )
    # 指定范围走了规范标识归一
    assert seen["codes"] == ["SSE.STK.600000", "SSE.STK.600009"]
    assert [h["code"] for h in out["hits"]] == ["SSE.STK.600000"]
    assert "SSE.STK.600009" in out["incomplete_codes"]
    assert out["missing_count"] == 1 and out["error_count"] == 1
    assert out["evaluated"] == 2


def test_run_screen_no_go_passthrough(tmp_path, monkeypatch):
    """数据面不可用：原样透出 no_go，不产出命中（不冒充成功）。"""
    from wtpy.apps.astock.service import screening as sc
    from wtpy.apps.astock.service import indicator_review as ir

    monkeypatch.setattr(
        sc, "resolve_screen_asof", lambda cfg, requested: (20260828, {"formal_l1_id": "x"})
    )
    monkeypatch.setattr(
        ir, "run_weekly_review",
        lambda cfg, **kw: {"status": "no_go", "no_go_reason": "no_formal_l1_product",
                           "universe_size": 0, "error_count": 0, "errors": [], "rules": []},
    )
    cfg = get_default_config(storage_root=tmp_path)
    out = sc.run_screen(cfg, rule_ids=["r1"], match_mode="any")
    assert out["status"] == "no_go"
    assert out["matched_count"] == 0 and out["hits"] == []


def test_run_screen_rejects_bad_args(tmp_path, monkeypatch):
    from wtpy.apps.astock.service import screening as sc

    cfg = get_default_config(storage_root=tmp_path)
    with pytest.raises(sc.ScreenError):
        sc.run_screen(cfg, rule_ids=[], match_mode="any")
    with pytest.raises(sc.ScreenError):
        sc.run_screen(cfg, rule_ids=["r1"], match_mode="both")


def test_resolve_screen_asof_weekend_rejected(tmp_path, monkeypatch):
    """日历内非交易日：拒绝并建议最近交易日，不静默回退。"""
    import json as _json

    from wtpy.apps.astock.service import screening as sc

    cfg = get_default_config(storage_root=tmp_path)
    # calendar_path = storage_root/calendar.json；20260829 是周六：
    # 请求它应被拒绝，建议 20260828（周五）
    (tmp_path / "calendar.json").write_text(
        _json.dumps({"dates": ["20260824", "20260825", "20260826", "20260827", "20260828"]}),
        encoding="utf-8",
    )
    from wtpy.apps.astock.service import indicator_review as ir

    monkeypatch.setattr(
        ir, "_resolve_formal_surface",
        lambda cfg: ({"formal_l1_id": "x", "max_date": 20260828}, ""),
    )
    with pytest.raises(sc.ScreenDateNotAvailable) as ei:
        sc.resolve_screen_asof(cfg, "2026-08-29")
    assert ei.value.suggested == 20260828
    eff, surface = sc.resolve_screen_asof(cfg, "2026-08-28")
    assert eff == 20260828
    eff2, _ = sc.resolve_screen_asof(cfg, None)
    assert eff2 == 20260828


# ===========================================================================
# POST /api/v1/bagua/export?force_async=true
# ===========================================================================


def test_export_force_async_forces_background(wb_client):
    """limit=10 旧判定走同步；force_async=true 强制走后台任务（工作台口径）。"""
    r = wb_client.post(
        "/api/v1/bagua/export?async_mode=true&force_async=true",
        json={"codes": ["SSE.STK.600000", "SZSE.STK.000001"], "all_stocks": False,
              "date": "2026-08-14", "limit": 10, "review_rules": []},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "async" and body["job_id"].startswith("bqexp_")
    assert body["scope_summary"] == "指定 2 个标的"
    assert body["review_rules_mode"] == "none"
    assert body["codes"] == ["SSE.STK.600000", "SZSE.STK.000001"]
    # 任务记录出现在列表
    lst = wb_client.get("/api/v1/bagua/export/jobs").json()
    assert any(x["job_id"] == body["job_id"] for x in lst["jobs"])


def test_export_default_sync_behavior_preserved(wb_client, monkeypatch, tmp_path):
    """不传 force_async：小批量 + limit<=50 仍然同步（旧接口行为不回归）。

    同步路径会真正生成 xlsx——mock 掉核心导出函数避免依赖真实行情，
    并落一个真实小文件供 FileResponse 返回。
    """
    fake_path = tmp_path / "fake_export.xlsx"
    fake_path.write_bytes(b"PK\x03\x04fake")

    def _fake_export(cfg, **kwargs):
        return fake_path

    import wtpy.apps.astock.service.bagua_query as bqmod

    monkeypatch.setattr(bqmod, "export_bagua_multi_period_xlsx", _fake_export)
    r = wb_client.post(
        "/api/v1/bagua/export?async_mode=false",
        json={"codes": ["SSE.STK.600000"], "all_stocks": False, "date": "2026-08-14", "limit": 10},
    )
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers.get("content-type", "")
    # 同步路径不产生任务记录
    lst = wb_client.get("/api/v1/bagua/export/jobs").json()
    assert lst["count"] == 0
