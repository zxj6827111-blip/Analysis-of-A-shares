# -*- coding: utf-8 -*-
"""端到端串联测试：快照 → 跟踪计算 → 三级 API → xlsx 导出。

比单元测试更强的一层证据：用**同一份合成数据**穿过 Phase 2 服务与
Phase 3 读取方/导出器，断言三处看到的数字一致（契约 §A4 验收精神：
同 snapshot_id、同规则、同范围下各读取路径必须一致）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tests.apps.astock.conftest  # noqa: F401

from wtpy.apps.astock.config import AStockConfig
from wtpy.apps.astock.service import screen_contract as sc
from wtpy.apps.astock.service import screen_tracking as st

from tests.apps.astock.test_screen_tracking import (  # noqa: E402
    BENCH, C1, C2, C3, SIGNAL_DATE, TRACK_WEEK,
    _bars, _calendar, _flat, _loader, _publish_snapshot,
)


@pytest.fixture()
def e2e(tmp_path: Path, monkeypatch):
    """隔离 storage + 合成日历 + 已发布快照 + 已结算跟踪产物。"""
    cfg = AStockConfig()
    cfg.storage_root = tmp_path / "storage"
    md_root = tmp_path / "md"
    md_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MARKET_DATA_ROOT", str(md_root))
    # 指标目录隔离到 tmp：AStockConfig() 默认指向仓库真实「指标」目录，会让
    # L0 的「当前规则目录」过滤（删除同步）拿到真实规则集，而本用例合成快照
    # 的 rule_id="r1" 不在其中 → 被当成已删除规则过滤掉。空目录 = 目录不可得
    # → 过滤停用（fail-open），用例聚焦跟踪链路本身。
    ind = tmp_path / "ind"
    ind.mkdir(parents=True, exist_ok=True)
    cfg.indicator_dir = ind
    # 规则目录同步（L0/L1 与规则中心一致）由专门用例覆盖；本用例聚焦跟踪链路，
    # 显式关闭目录注入——否则内置规则集（bagua_ohlc 等）会把合成的 rule_id="r1"
    # 当成「规则中心已删除」过滤掉（空目录仍会返回内置规则）。
    from wtpy.apps.astock.api_routes import tracking as track_routes

    monkeypatch.setattr(track_routes, "_current_rule_catalog", lambda _ctx: {})
    # 导出侧同理：_current_rule_ids 返回 None = 目录不可得 → 不过滤不归并
    from wtpy.apps.astock.service import track_export as track_export_svc

    monkeypatch.setattr(track_export_svc, "_current_rule_catalog", lambda _cfg: None)

    # 已发布快照：r1 命中 C1/C2（快照 close 故意离谱，入场价必须重读）
    _publish_snapshot(cfg, matched=[
        {"code": C1, "close": 999.0}, {"code": C2, "close": 999.0},
    ])

    # 合成行情：C1 上涨 10%（首日开盘 10 → 周末 11），C2 一字涨停买不进
    c1 = _bars({
        SIGNAL_DATE: 10.0, TRACK_WEEK[0]: 10.0, TRACK_WEEK[1]: 10.4,
        TRACK_WEEK[2]: 10.6, TRACK_WEEK[3]: 10.8, TRACK_WEEK[4]: 11.0,
    })
    c2_qfq = _bars({
        SIGNAL_DATE: 10.0, TRACK_WEEK[0]: 11.0, TRACK_WEEK[1]: 11.5,
        TRACK_WEEK[2]: 11.6, TRACK_WEEK[3]: 11.8, TRACK_WEEK[4]: 12.0,
    })
    c2_raw = _flat({
        20250109: 10.0, SIGNAL_DATE: 10.0, TRACK_WEEK[0]: 11.0,
        TRACK_WEEK[1]: 11.5, TRACK_WEEK[2]: 11.6,
        TRACK_WEEK[3]: 11.8, TRACK_WEEK[4]: 12.0,
    })
    bench = _flat({SIGNAL_DATE: 100.0, **{d: 102.0 for d in TRACK_WEEK}})
    # 用测试模块自带的合成日历（只含真实交易日；手写 range 会把周末当
    # 交易日 → 信号日不再"周内最后交易日" → fail-closed 报 anomaly）
    cal = _calendar()
    out = st.compute_weekly_tracking(
        cfg, SIGNAL_DATE,
        bar_loader=_loader({C1: c1, C2: c2_qfq}),
        raw_loader=_loader({C1: c1, C2: c2_raw}),
        benchmark_loader=_loader({BENCH: bench}),
        calendar=cal,
    )
    assert out["completion"] == sc.TRACK_COMPLETE
    return cfg, out


def _client(cfg):
    from fastapi.testclient import TestClient

    from wtpy.apps.astock.api import create_app

    return TestClient(create_app(cfg))


class TestEndToEndChain:
    def test_l2_rows_match_product(self, e2e):
        """L2 明细逐票数字 == 产物 rows（读取方不得改口径/丢精度）。"""
        cfg, out = e2e
        product = json.loads(Path(out["product_path"]).read_text(encoding="utf-8"))
        client = _client(cfg)
        try:
            r = client.get(f"/api/v1/bagua/track/weeks/{SIGNAL_DATE}")
            assert r.status_code == 200
            body = r.json()
            assert body["completion"] == sc.TRACK_COMPLETE
            assert body["backfill"] is False
            by_code = {row["code"]: row for row in body["rows"]}
            for row in product["rows"]:
                got = by_code[row["code"]]
                assert got["ret_close_sig"] == pytest.approx(row["ret_close_sig"])
                assert got["fill_status"] == row["fill_status"]
                assert got["entry_close_signal"] == pytest.approx(
                    row["entry_close_signal"]
                )
        finally:
            client.close()

    def test_l0_l1_l2_consistent(self, e2e):
        """三级视图口径一致：L0 本周入选 == L1 该周 selected == 快照命中数∩票池。"""
        cfg, out = e2e
        client = _client(cfg)
        try:
            l0 = client.get("/api/v1/bagua/track/rules").json()
            assert l0["count"] == 1
            rule = l0["rules"][0]
            assert rule["tracked_weeks"] == 1 and rule["settled_weeks"] == 1
            assert rule["latest_week"] == SIGNAL_DATE
            # 近 1 周胜率 = 该周逐票胜率（C1 涨、C2 涨 → 1.0，均为信号口径）
            assert rule["weekly_equal_win_rate_sig"] == pytest.approx(1.0)
            # 开盘口径：C2 一字涨停不可买 → 只有 C1 进分母且为正 → 1.0
            assert rule["weekly_equal_win_rate_exec"] == pytest.approx(1.0)
            assert rule["total_selected"] == 2

            l1 = client.get("/api/v1/bagua/track/rules/r1/weeks").json()
            assert l1["count"] == 1
            wk = l1["weeks"][0]
            assert wk["week_id"] == SIGNAL_DATE
            assert wk["selected_count"] == 2
            assert wk["aggregate"]["valid_sig_count"] == 2
            assert wk["aggregate"]["valid_exec_count"] == 1  # C2 不可成交剔除
            assert wk["aggregate"]["unbuyable_count"] == 1

            l2 = client.get(f"/api/v1/bagua/track/weeks/{SIGNAL_DATE}").json()
            assert len(l2["rows"]) == 2
            assert l2["pending_picks"] == []
        finally:
            client.close()

    def test_export_contains_same_numbers(self, e2e):
        """导出 xlsx 的周明细收益 == L2 明细收益（同一产物、同一口径）。"""
        from openpyxl import load_workbook

        from wtpy.apps.astock.service.track_export import export_tracking_xlsx

        cfg, out = e2e
        exp = export_tracking_xlsx(cfg, weeks=12)
        assert exp["ok"] is True
        wb = load_workbook(exp["path"])
        try:
            assert set(exp["sheets"]).issubset(set(wb.sheetnames))
            ws = wb[[n for n in wb.sheetnames if n.startswith("周明细")][0]]
            header = [c.value for c in ws[1]]
            ci_code = header.index("代码") + 1
            ci_sig = [h for h in header if h.startswith("周五收益(信号")][0]
            ci_sig_idx = header.index(ci_sig) + 1
            rows = {}
            for r in ws.iter_rows(min_row=2, values_only=True):
                if r[ci_code - 1] in (C1, C2):
                    rows[r[ci_code - 1]] = r[ci_sig_idx - 1]
            # C1：10.0 → 11.0 = +10%；导出按百分数写
            assert rows[C1] == pytest.approx(10.0, abs=0.01)
            # C2：10.0 → 12.0 = +20%
            assert rows[C2] == pytest.approx(20.0, abs=0.01)
        finally:
            wb.close()

    def test_export_download_endpoint_serves_file(self, e2e):
        """导出后经下载端点取回同一文件（白名单 + resolve 校验通过）。"""
        from wtpy.apps.astock.service.track_export import export_tracking_xlsx

        cfg, _out = e2e
        exp = export_tracking_xlsx(cfg, weeks=12)
        client = _client(cfg)
        try:
            r = client.get(
                "/api/v1/bagua/track/export/download",
                params={"file": exp["file"]},
            )
            assert r.status_code == 200
            assert r.content[:2] == b"PK"  # xlsx zip 魔数
            # 穿越尝试一律 400
            for bad in ("../secret.xlsx", "..\\secret.xlsx", "/etc/passwd",
                        "bagua_track_../x.xlsx"):
                rb = client.get(
                    "/api/v1/bagua/track/export/download", params={"file": bad}
                )
                assert rb.status_code == 400, bad
        finally:
            client.close()

    def test_pending_week_shows_unsettled(self, e2e):
        """未结算周（无产物）→ L0 计入 tracked 但不进聚合，L2 命中进 pending_picks。"""
        cfg, _out = e2e
        # 再造一个只有快照、没有跟踪产物的周
        next_week = 20250117
        _publish_snapshot(cfg, week_id=next_week, matched=[
            {"code": C3, "close": 5.0},
        ])
        client = _client(cfg)
        try:
            l0 = client.get("/api/v1/bagua/track/rules").json()
            # 两周同规则同指纹（同一公式版本）→ 聚合进同一段：tracked=2，
            # 其中只有 0910 那周有产物 → settled=1（未结算周不进聚合）
            assert l0["count"] == 1
            rule = l0["rules"][0]
            assert rule["tracked_weeks"] == 2 and rule["settled_weeks"] == 1
            assert rule["latest_week"] == next_week        # 最新周 = 未结算周
            assert rule["latest_selected"] == 1

            # L1：未结算周如实标 no_product（不冒充已结算）
            l1 = client.get("/api/v1/bagua/track/rules/r1/weeks").json()
            wks = {w["week_id"]: w for w in l1["weeks"]}
            assert wks[next_week]["completion"] == "no_product"
            assert wks[next_week]["aggregate"] is None
            assert wks[SIGNAL_DATE]["completion"] == sc.TRACK_COMPLETE

            l2 = client.get(f"/api/v1/bagua/track/weeks/{next_week}").json()
            assert l2["completion"] == "no_product"
            assert [p["code"] for p in l2["pending_picks"]] == [C3]
        finally:
            client.close()
