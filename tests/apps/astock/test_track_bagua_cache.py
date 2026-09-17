# -*- coding: utf-8 -*-
"""跟踪页周/月卦结果缓存（bagua_week_month_info）的失效与状态语义测试。

覆盖用户给出的三条边界：
- 缓存必须随行情版本与知识库版本失效，不能拿旧卦象冒充新行情；
- 「加载中 / 无数据 / 失败」三态要明确区分（ok/empty/error 不混同）；
- 计算失败不留缓存（重试要能真的重算，而不是回放同一个失败结论）。

不依赖真实行情根：价格面解析与逐票查询都被替换为确定性桩。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from wtpy.apps.astock.service import bagua_query as bq

KB_SRC = (
    Path(__file__).resolve().parents[3]
    / "wtpy" / "apps" / "astock" / "bagua" / "bagua_384.json"
)


def _week_row(name: str = "乾为天"):
    return {
        "ok": True,
        "summary": {"full_name": name, "action_signal": "持有"},
        "bagua": {"action_signal": "持有"},
    }


def _cfg(tmp_path) -> SimpleNamespace:
    """知识库复制到 tmp：测试要模拟「语料被替换」以验证指纹失效。"""
    kb = tmp_path / "kb.json"
    shutil.copyfile(KB_SRC, kb)
    return SimpleNamespace(
        bagua_json=kb,
        storage_root=tmp_path,
        market_data_root=tmp_path / "md",
    )


@pytest.fixture()
def stub_query(monkeypatch, tmp_path):
    """把「有效版本键 + 价格面 + 逐票查询」都替换为可控桩。"""
    bq.clear_bagua_info_cache()
    state = {"version": ("v1",), "calls": 0, "mode": "ok"}

    monkeypatch.setattr(
        bq, "_plane_version_keys", lambda cfg, std, amap: state["version"]
    )
    monkeypatch.setattr(bq, "_get_plane_session", lambda cfg, plane: object())

    def _query(cfg, *, code, asof, periods, adjust, session, calc, asof_map):
        state["calls"] += 1
        if state["mode"] == "raise":
            raise RuntimeError("io boom")
        if state["mode"] == "no_data":
            return {
                per: {"ok": False, "data_status": "no_data", "error_reason": "no bar"}
                for per in periods
            }
        if state["mode"] == "error_row":
            return {
                per: {"ok": False, "data_status": "error", "error_reason": "boom"}
                for per in periods
            }
        if state["mode"] == "month_fail":
            # 周卦成功、月卦查询失败（用户 2026-09-16 复核注入的场景）
            return {
                "WEEK": _week_row(),
                "MONTH": {"ok": False, "data_status": "error", "error_reason": "month boom"},
            }
        if state["mode"] == "week_fail_month_ok":
            return {
                "WEEK": {"ok": False, "data_status": "error", "error_reason": "week boom"},
                "MONTH": _week_row("坤为地"),
            }
        if state["mode"] == "month_no_data":
            # 月卦无数据（如次新股，上一自然月还没有月K）：是「无数据」不是「失败」
            return {
                "WEEK": _week_row(),
                "MONTH": {"ok": False, "data_status": "no_data", "error_reason": "no bar"},
            }
        return {
            "WEEK": _week_row(),
            "MONTH": _week_row("坤为地"),
        }

    monkeypatch.setattr(bq, "_query_bagua_periods_for_code", _query)
    yield SimpleNamespace(cfg=_cfg(tmp_path), state=state, query=_query)
    bq.clear_bagua_info_cache()


def _info(stub, code="SSE.STK.600000"):
    return bq.bagua_week_month_info(stub.cfg, code=code, asof=20260911)


def test_second_call_hits_cache(stub_query):
    first = _info(stub_query)
    assert first["state"] == "ok" and first["week_gua"] == "乾为天"
    assert first["cached"] is False
    assert stub_query.state["calls"] == 1

    second = _info(stub_query)
    assert second["cached"] is True
    assert second["week_gua"] == first["week_gua"]
    assert second["bagua"] == first["bagua"]
    assert stub_query.state["calls"] == 1, "命中缓存不得再次计算"
    stats = bq.bagua_info_cache_stats()
    assert stats["hit"] == 1 and stats["miss"] == 1


def test_market_data_version_change_invalidates(stub_query):
    assert _info(stub_query)["cached"] is False
    assert _info(stub_query)["cached"] is True
    # 行情换代（overlay delta 提交 → 虚拟 manifest id/sha 变化）后必须重算
    stub_query.state["version"] = ("v2",)
    out = _info(stub_query)
    assert out["cached"] is False
    assert stub_query.state["calls"] == 2


def test_knowledge_base_fingerprint_participates(stub_query):
    assert _info(stub_query)["cached"] is False
    assert _info(stub_query)["cached"] is True
    # 语料文件变化（大小/mtime 变）→ 旧解释不得复用；损坏语料 fail-soft 成
    # 「计算失败」而不是让接口 500（知识库在导入环节另校验）
    stub_query.cfg.bagua_json.write_text("x" * 32, encoding="utf-8")
    out = _info(stub_query)
    assert out["cached"] is False
    assert stub_query.state["calls"] == 2

def test_ok_and_empty_are_distinguished_and_cached(stub_query):
    stub_query.state["mode"] = "no_data"
    empty = _info(stub_query)
    assert empty["state"] == "empty", "数据面正常但算不出周卦 = 无数据，不是失败"
    assert empty["week_gua"] == "" and empty["bagua"] is None
    assert _info(stub_query)["cached"] is True, "无数据结论在版本不变时可复用"


def test_error_state_is_not_cached_and_can_retry(stub_query):
    stub_query.state["mode"] = "raise"
    failed = _info(stub_query)
    assert failed["state"] == "error"
    assert stub_query.state["calls"] == 2  # 两个价格口径各试一次

    # 未缓存错误 → 重试会真的重算（否则「重试」按钮只是回放失败结论）
    stub_query.state["mode"] = "ok"
    retried = _info(stub_query)
    assert retried["state"] == "ok" and retried["week_gua"] == "乾为天"
    assert stub_query.state["calls"] == 3


def test_error_row_counts_as_error_but_mixed_no_data_is_empty(stub_query):
    stub_query.state["mode"] = "error_row"
    assert _info(stub_query)["state"] == "error"
    stub_query.state["mode"] = "no_data"
    assert _info(stub_query)["state"] == "empty"


def test_version_unknown_skips_cache(stub_query, monkeypatch):
    monkeypatch.setattr(bq, "_plane_version_keys", lambda cfg, std, amap: None)
    assert _info(stub_query)["cached"] is False
    assert _info(stub_query)["cached"] is False
    assert stub_query.state["calls"] == 2, "版本不可确定时宁可多算，也不能写脏缓存"


def test_index_etf_not_cached(stub_query):
    """指数/ETF 走各自价格面（锚点钉定），不并入本缓存。"""
    assert _info(stub_query, code="SSE.IDX.000300")["cached"] is False
    assert _info(stub_query, code="SSE.IDX.000300")["cached"] is False
    assert _info(stub_query, code="SSE.ETF.510300")["cached"] is False
    assert stub_query.state["calls"] == 3


def test_cache_key_differs_per_code_and_asof(stub_query):
    _info(stub_query, code="SSE.STK.600000")
    _info(stub_query, code="SZSE.STK.000001")
    assert stub_query.state["calls"] == 2, "不同票各自一条缓存"
    _info(stub_query, code="SSE.STK.600000")
    assert stub_query.state["calls"] == 2, "同票同周必须命中"
    bq.bagua_week_month_info(stub_query.cfg, code="SSE.STK.600000", asof=20260904)
    assert stub_query.state["calls"] == 3, "同票不同周不得互相复用"


def test_month_failure_is_not_reported_as_full_success(stub_query):
    """周卦成功、月卦失败：只标月卦失败，周卦照常给出，且不得缓存成「完整成功」。"""
    stub_query.state["mode"] = "month_fail"
    first = _info(stub_query)
    assert first["state"] == "ok", "周卦确实拿到了，整体状态不能连坐"
    assert first["week_gua"] == "乾为天"
    assert first["month_state"] == "error", "月卦失败必须单独记账"
    assert first["bagua"]["week"]["combo"] == "乾为天"
    assert first["bagua"]["month"] is None
    assert stub_query.state["calls"] == 1

    # 关键：不得缓存成完整成功——否则第二次直接命中，月卦永远不会被重算
    second = _info(stub_query)
    assert second["cached"] is False
    assert stub_query.state["calls"] == 2

    # 月卦恢复后重试：一次调用即拿到完整结果并进入缓存
    stub_query.state["mode"] = "ok"
    third = _info(stub_query)
    assert third["state"] == "ok" and third["month_state"] == "ok"
    assert third["bagua"]["month"]["combo"] == "坤为地"
    assert _info(stub_query)["cached"] is True
    assert stub_query.state["calls"] == 3


def test_month_no_data_is_empty_and_still_cached(stub_query):
    """月卦「无数据」（次新股等）不是失败：标 empty，且可缓存（版本确定的事实）。"""
    stub_query.state["mode"] = "month_no_data"
    first = _info(stub_query)
    assert first["state"] == "ok"
    assert first["week_gua"] == "乾为天"
    assert first["month_state"] == "empty"
    assert first["bagua"]["month"] is None
    assert _info(stub_query)["cached"] is True, "无数据不是失败，可复用"
    assert stub_query.state["calls"] == 1


def test_week_failure_keeps_successful_month(stub_query):
    """周卦没算出来但月卦成功：月卦结果如实保留，整体按周卦的状态标。"""
    stub_query.state["mode"] = "week_fail_month_ok"
    info = _info(stub_query)
    assert info["week_state"] == "error" and info["state"] == "error"
    assert info["month_state"] == "ok"
    assert info["bagua"]["month"]["combo"] == "坤为地"
    assert info["bagua"]["week"] is None
    assert info["week_gua"] == ""
